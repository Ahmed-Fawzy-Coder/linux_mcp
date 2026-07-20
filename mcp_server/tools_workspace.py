from __future__ import annotations

import json
import secrets
from typing import Any, Callable, Dict, Optional

from fastapi import HTTPException, status

from .security import Settings
from .context_results import (
    canonical_json,
    content_sha256,
    get_context_result,
    normalize_etag,
    reduce_context_result,
    store_context_result,
)
from .tools_files import edit_file, read_file, read_multiple_files, write_file, write_files_batch
from .tools_jobs import (
    get_job_output,
    get_job_status,
    run_commands_parallel,
    start_background_job,
    stop_job,
    wait_jobs,
)
from .tools_search import search_files
from .tools_terminal import run_command


ESTIMATED_NATIVE_OUTPUT_CAP_CHARS = 40_000


class WorkspaceResult(str):
    """String result carrying server-side telemetry that is not sent to the model."""

    payload_chars: int
    internal_discarded_chars: int
    estimated_savable_chars: int
    measured_segments: int
    truncated: bool
    context_stored_chars: int
    context_original_chars: int
    context_reduced_chars: int
    context_returned_chars: int
    context_saved_chars: int
    context_retrieval_chars: int
    context_stored: int
    context_reduced: int
    context_retrieval: int
    context_not_modified: int
    context_source_incomplete: int


def _strip_telemetry(value: Any) -> tuple[Any, Dict[str, Any]]:
    totals: Dict[str, Any] = {
        "source_chars": 0,
        "returned_content_chars": 0,
        "estimated_savable_chars": 0,
        "measured_segments": 0,
        "truncated": False,
    }

    def visit(item: Any) -> Any:
        if isinstance(item, dict):
            clean = {}
            metric = item.get("_telemetry")
            if isinstance(metric, dict):
                source_chars = max(0, int(metric.get("source_chars", 0)))
                returned_chars = max(0, int(metric.get("returned_content_chars", 0)))
                totals["source_chars"] += source_chars
                totals["returned_content_chars"] += returned_chars
                totals["estimated_savable_chars"] += max(
                    0,
                    min(source_chars, ESTIMATED_NATIVE_OUTPUT_CAP_CHARS) - returned_chars,
                )
                totals["measured_segments"] += 1
            for key, child in item.items():
                if key == "_telemetry":
                    continue
                if key == "truncated" and child is True:
                    totals["truncated"] = True
                if key == "has_more" and child is True:
                    totals["truncated"] = True
                if key.endswith("_truncated") and child is True:
                    totals["truncated"] = True
                clean[key] = visit(child)
            return clean
        if isinstance(item, list):
            return [visit(child) for child in item]
        return item

    return visit(value), totals


def _measured_result(payload: str, telemetry: Dict[str, Any], **context: int) -> WorkspaceResult:
    measured = WorkspaceResult(payload)
    returned_content = telemetry["returned_content_chars"]
    measured.payload_chars = len(payload)
    measured.internal_discarded_chars = max(0, telemetry["source_chars"] - returned_content)
    measured.estimated_savable_chars = telemetry["estimated_savable_chars"]
    measured.measured_segments = telemetry["measured_segments"]
    measured.truncated = telemetry["truncated"]
    for name in (
        "context_stored_chars", "context_original_chars", "context_reduced_chars",
        "context_returned_chars", "context_saved_chars", "context_retrieval_chars",
        "context_stored", "context_reduced", "context_retrieval",
        "context_not_modified", "context_source_incomplete",
    ):
        setattr(measured, name, max(0, int(context.get(name, 0))))
    return measured


def _context_control(value: Any) -> Dict[str, Any]:
    if value is None or value is False:
        return {"mode": "off", "intent": "", "if_none_match": ""}
    if value is True:
        value = {}
    if not isinstance(value, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "_context must be an object, true, or false.")
    mode = str(value.get("mode", "auto")).strip().lower()
    aliases = {"ultimate": "auto", "reduce": "auto", "preserve": "store", "none": "off"}
    mode = aliases.get(mode, mode)
    if mode not in {"off", "auto", "store", "full"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "_context.mode must be off, auto, store, or full.")
    intent = str(value.get("intent", ""))[:200]
    validator = normalize_etag(value.get("if_none_match", ""))[:200]
    return {"mode": mode, "intent": intent, "if_none_match": validator}


def _retrieval(settings: Settings, arguments: Dict[str, Any]) -> WorkspaceResult:
    payload = dict(arguments)
    context_id = payload.pop("context_id", payload.pop("id", ""))
    result = get_context_result(settings, context_id, **payload)
    serialized = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    returned_chars = max(0, int(result.get("returned_chars", 0)))
    return _measured_result(
        serialized,
        {
            "source_chars": int(result.get("total_chars", 0)),
            "returned_content_chars": returned_chars,
            "estimated_savable_chars": 0,
            "measured_segments": 1,
            "truncated": bool(result.get("has_more")),
        },
        context_retrieval_chars=returned_chars,
        context_retrieval=1,
        context_not_modified=int(result.get("not_modified") is True),
        context_source_incomplete=int(result.get("source_complete") is False),
    )


def workspace(settings: Settings, action: str,
              arguments: Optional[Dict[str, Any]] = None) -> str:
    """Dispatch a bounded workspace operation and return one compact JSON payload."""
    handlers: Dict[str, Callable[..., Dict[str, Any]]] = {
        "search_files": lambda **kwargs: search_files(settings, **kwargs),
        "read_file": lambda **kwargs: read_file(settings, **kwargs),
        "read_multiple_files": lambda **kwargs: read_multiple_files(settings, **kwargs),
        "edit_file": lambda **kwargs: edit_file(settings, **kwargs),
        "write_file": lambda **kwargs: write_file(settings, **kwargs),
        "write_files_batch": lambda **kwargs: write_files_batch(settings, **kwargs),
        "run_command": lambda **kwargs: run_command(settings, **kwargs),
        "run_commands_parallel": lambda **kwargs: run_commands_parallel(settings, **kwargs),
        "start_background_job": lambda **kwargs: start_background_job(settings, **kwargs),
        "get_job_status": lambda **kwargs: get_job_status(settings, **kwargs),
        "get_job_output": lambda **kwargs: get_job_output(settings, **kwargs),
        "wait_jobs": lambda **kwargs: wait_jobs(settings, **kwargs),
        "stop_job": lambda **kwargs: stop_job(
            settings,
            job_id=kwargs["job_id"],
            signal_name=kwargs.get("signal", kwargs.get("signal_name", "TERM")),
        ),
    }
    operation = (action or "").strip()
    supplied_arguments = dict(arguments or {})
    if operation == "run_command" and "max_output_lines" in supplied_arguments:
        # Codex agents commonly use this intuitive alias even though the compact
        # workspace schema calls the bound `tail_lines`. Accept it so a harmless
        # naming mismatch does not cost a failed tool round trip.
        max_output_lines = supplied_arguments.pop("max_output_lines")
        supplied_arguments.setdefault("tail_lines", max_output_lines)
    if operation == "get_context_result":
        try:
            return _retrieval(settings, supplied_arguments)
        except TypeError as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Invalid arguments for {operation}: {exc}",
            ) from exc
    handler = handlers.get(operation)
    if handler is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unknown action: {operation}. Allowed: {', '.join([*handlers, 'get_context_result'])}",
        )
    context = _context_control(supplied_arguments.pop("_context", None))
    try:
        result = handler(**supplied_arguments)
    except (KeyError, TypeError) as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Invalid arguments for {operation}: {exc}",
        ) from exc
    clean_result, telemetry = _strip_telemetry(result)
    if context["mode"] == "off":
        serialized = json.dumps(clean_result, ensure_ascii=False, separators=(",", ":"))
        return _measured_result(serialized, telemetry)

    snapshot = canonical_json(clean_result)
    digest = content_sha256(snapshot)
    source_complete = not telemetry["truncated"]
    if context["if_none_match"] and secrets.compare_digest(context["if_none_match"], digest):
        response = {
            "ok": True,
            "not_modified": True,
            "etag": digest,
            "snapshot_complete": True,
            "source_complete": source_complete,
        }
        serialized = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
        return _measured_result(
            serialized,
            telemetry,
            context_original_chars=len(snapshot),
            context_returned_chars=len(serialized),
            context_saved_chars=max(0, len(snapshot) - len(serialized)),
            context_not_modified=1,
            context_source_incomplete=int(not source_complete),
        )

    stored: Optional[Dict[str, Any]] = None
    output = clean_result
    was_reduced = False
    if context["mode"] in {"auto", "store"}:
        stored = store_context_result(settings, snapshot, source_complete=source_complete)
    if context["mode"] == "auto":
        target_chars = max(512, int(getattr(settings, "context_result_reduce_chars", 4_000)))
        candidate = reduce_context_result(
            operation, clean_result, intent=context["intent"], target_chars=target_chars,
        )
        was_reduced = len(canonical_json(candidate)) < len(snapshot)
        if was_reduced:
            output = candidate
    reduced_body_chars = len(canonical_json(output)) if was_reduced else 0
    metadata: Dict[str, Any] = {
        "etag": digest,
        "sha256": digest,
        "snapshot_chars": len(snapshot),
        "snapshot_complete": True,
        "source_complete": source_complete,
        "reduced": was_reduced,
    }
    if stored is not None:
        metadata.update({
            "id": stored["id"],
            "expires_at": stored["expires_at"],
            "stored_chars": stored["stored_chars"],
        })
    output = dict(output)
    output["_context_result"] = metadata
    serialized = json.dumps(output, ensure_ascii=False, separators=(",", ":"))
    return _measured_result(
        serialized,
        telemetry,
        context_stored_chars=int(stored["stored_chars"]) if stored else 0,
        context_original_chars=len(snapshot),
        context_reduced_chars=reduced_body_chars,
        context_returned_chars=len(serialized),
        context_saved_chars=max(0, len(snapshot) - len(serialized)),
        context_stored=int(stored is not None),
        context_reduced=int(was_reduced),
        context_source_incomplete=int(not source_complete),
    )
