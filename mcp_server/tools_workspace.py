from __future__ import annotations

import json
import hashlib
import secrets
from pathlib import Path
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
from .project_ledger import Ledger
from .semantic_cache import SemanticCache
from .adaptive_compression import decide


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
    model_requested_retrievals: int
    automatic_retrievals: int
    retrieval_no_progress: int
    answers_with_incomplete_source: int


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
    aliases = {
        "ultimate": "auto",
        "reduce": "auto",
        "enforce": "auto",
        "preserve": "store",
        "none": "off",
    }
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
        model_requested_retrievals=1,
        automatic_retrievals=0,
        retrieval_no_progress=int(result.get("not_modified") is True or result.get("has_more") is False),
        answers_with_incomplete_source=int(result.get("source_complete") is False),
    )


def _normalize_read_file_alias(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize the common {path: project_root, file: relative_path} alias."""
    if "file" not in arguments:
        return arguments

    project_path = arguments.get("path")
    relative_file = arguments.get("file")
    if not isinstance(project_path, str) or not project_path:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Invalid read_file alias: path must be an absolute project directory.",
        )
    if not isinstance(relative_file, str) or not relative_file:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Invalid read_file alias: file must be a non-empty relative file path.",
        )

    project_root = Path(project_path).expanduser()
    file_path = Path(relative_file)
    if not project_root.is_absolute():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Invalid read_file alias: path must be an absolute project directory.",
        )
    if file_path.is_absolute():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Invalid read_file alias: file must be relative; pass an absolute full file path "
            "as path without the file field instead.",
        )
    if ".." in file_path.parts:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Invalid read_file alias: file must not contain '..' or escape the project path.",
        )

    resolved_root = project_root.resolve()
    resolved_file = (resolved_root / file_path).resolve()
    try:
        resolved_file.relative_to(resolved_root)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Invalid read_file alias: file must not escape the project path.",
        ) from exc

    normalized = dict(arguments)
    normalized["path"] = str(resolved_file)
    normalized.pop("file")
    return normalized


def _normalize_workspace_arguments(operation: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Accept bounded, unambiguous aliases commonly emitted by Codex clients."""
    normalized = dict(arguments)

    def alias(target: str, *sources: str) -> None:
        for source in sources:
            if source not in normalized:
                continue
            value = normalized.pop(source)
            normalized.setdefault(target, value)

    def timeout_aliases() -> None:
        alias("timeout_s", "timeout_seconds")
        if "timeout_ms" not in normalized:
            return
        milliseconds = normalized.pop("timeout_ms")
        if "timeout_s" in normalized:
            return
        try:
            normalized["timeout_s"] = max(1, (int(milliseconds) + 999) // 1000)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "timeout_ms must be an integer number of milliseconds.",
            ) from exc

    def output_line_aliases() -> None:
        alias("tail_lines", "max_output_lines", "max_lines", "lines")

    if operation == "read_file":
        normalized = _normalize_read_file_alias(normalized)
    elif operation == "read_multiple_files":
        alias("paths", "files")
    elif operation == "search_files":
        alias("pattern", "query")
        include_values = normalized.pop("include", None)
        file_pattern = normalized.pop("file_pattern", None)
        if "include_extensions" not in normalized:
            raw_values: list[Any] = []
            for value in (include_values, file_pattern):
                if isinstance(value, (list, tuple, set)):
                    raw_values.extend(value)
                elif value is not None:
                    raw_values.append(value)
            extensions = []
            for raw in raw_values:
                candidate = str(raw).strip()
                if not candidate:
                    continue
                if "*." in candidate:
                    candidate = candidate.rsplit("*.", 1)[1]
                candidate = candidate.lstrip(".")
                if candidate and "/" not in candidate and "*" not in candidate:
                    extensions.append(candidate)
            if extensions:
                normalized["include_extensions"] = list(dict.fromkeys(extensions))
    elif operation == "run_command":
        timeout_aliases()
        output_line_aliases()
        alias("max_output_chars", "max_chars")
    elif operation == "run_commands_parallel":
        timeout_aliases()
        output_line_aliases()
        alias("max_output_chars", "max_chars")
        commands = normalized.get("commands")
        if isinstance(commands, list):
            flattened = []
            inferred_cwds = set()
            for item in commands:
                if isinstance(item, str):
                    command = item
                elif isinstance(item, dict):
                    command = item.get("command", item.get("cmd"))
                    if item.get("cwd"):
                        inferred_cwds.add(str(item["cwd"]))
                else:
                    command = None
                if not isinstance(command, str) or not command.strip():
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST,
                        "Each commands item must be a command string or an object containing command/cmd.",
                    )
                flattened.append(command)
            if len(inferred_cwds) > 1:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "Per-command cwd values must be identical; split calls when commands need different directories.",
                )
            if inferred_cwds:
                normalized.setdefault("cwd", inferred_cwds.pop())
            normalized["commands"] = flattened
    elif operation == "start_background_job":
        timeout_aliases()
        alias("no_output_timeout_s", "no_output_timeout_seconds")
        for ignored in ("max_output_lines", "max_lines", "tail_lines", "max_output_chars", "max_chars"):
            normalized.pop(ignored, None)
    elif operation == "wait_jobs":
        timeout_aliases()
        output_line_aliases()
        alias("max_output_chars", "max_chars")
        if "job_ids" not in normalized and "job_id" in normalized:
            normalized["job_ids"] = [normalized.pop("job_id")]
        normalized.pop("cwd", None)
    elif operation == "get_job_output":
        output_line_aliases()
        alias("max_output_chars", "max_chars")
        normalized.pop("cwd", None)
    elif operation == "get_job_status":
        normalized.pop("cwd", None)

    return normalized


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
    supplied_arguments = _normalize_workspace_arguments(operation, dict(arguments or {}))
    ledger = Ledger(str(supplied_arguments.get("project_root", settings.workdir)))
    semantic = SemanticCache(ledger.db, ttl_s=int(getattr(settings, "semantic_cache_ttl_s", 3600)), enabled=bool(getattr(settings, "semantic_cache_enabled", True)))
    task_id = str(supplied_arguments.pop("task_id", "default")); conversation_id = str(supplied_arguments.pop("conversation_id", "default"))
    project_root = supplied_arguments.pop("project_root", str(settings.workdir))
    if action == "begin_task":
        ledger.task(task_id, conversation_id, str(supplied_arguments.get("goal", ""))); return json.dumps({"ok":True,"project_id":ledger.project_id,"task_id":task_id})
    if action == "get_project_state":
        try: return json.dumps(ledger.state(task_id, conversation_id),ensure_ascii=False)
        except PermissionError as exc: raise HTTPException(status.HTTP_403_FORBIDDEN,str(exc))
    if action == "record_decision":
        ledger.decision(task_id, conversation_id, str(supplied_arguments["decision"]), str(supplied_arguments.get("reason", ""))); return json.dumps({"ok":True})
    if action == "checkpoint_task":
        ledger.checkpoint(task_id, conversation_id, dict(supplied_arguments.get("snapshot", {}))); return json.dumps({"ok":True})
    if action == "complete_task":
        ledger.task(task_id, conversation_id, str(supplied_arguments.get("goal", "")), "completed"); return json.dumps({"ok":True})
    policy = "read-only"
    cache_key = None
    cache_deps = {}
    cacheable_request = supplied_arguments.get("_context") in (None, False)
    if cacheable_request and action in {"read_file", "read_multiple_files", "search_files", "get_job_status", "get_job_output"}:
        deps = {}
        for p in [supplied_arguments.get("path"), *(supplied_arguments.get("paths", []) if isinstance(supplied_arguments.get("paths"), list) else [])]:
            try: deps[str(p)] = hashlib.sha256(Path(str(p)).read_bytes()).hexdigest()
            except Exception: pass
        cache_deps = deps
        cache_key = ledger.cache_key(action, supplied_arguments, deps, policy)
        if cache_key:
            cached = ledger.cache_get(cache_key)
            if cached is not None: return json.dumps({**cached,"cache":"exact-hit"},ensure_ascii=False)
        semantic_candidate = semantic.lookup(ledger.project_id, action, canonical_json(supplied_arguments), cache_deps, ledger.tool_version)
        if semantic_candidate is not None:
            return json.dumps(semantic_candidate, ensure_ascii=False)
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
    except HTTPException as exc:
        if operation == "read_file" and exc.status_code == status.HTTP_404_NOT_FOUND:
            requested = Path(str(supplied_arguments.get("path", "")))
            root = Path(str(project_root)).expanduser().resolve()
            suggestions = []
            if requested.name and root.is_dir():
                try:
                    suggestions = [str(candidate) for candidate in root.rglob(requested.name) if candidate.is_file()][:5]
                except OSError:
                    suggestions = []
            result = {
                "ok": False,
                "status": "not_found",
                "path": str(requested),
                "suggestions": suggestions,
                "message": "File not found. Use a suggested path or search_files before retrying.",
            }
        else:
            raise
    except (KeyError, TypeError) as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Invalid arguments for {operation}: {exc}",
        ) from exc
    clean_result, telemetry = _strip_telemetry(result)
    ledger.fact(task_id, conversation_id, action, {"arguments": supplied_arguments, "result": clean_result})
    if cache_key and cacheable_request:
        ledger.cache_put(cache_key, action, clean_result, {}, policy)
        semantic.put(ledger.project_id, action, canonical_json(supplied_arguments), cache_deps, clean_result, ledger.tool_version)
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
        content_class = "search" if operation == "search_files" else ("logs" if operation in {"run_command", "get_job_output"} else "json")
        active = str(getattr(settings, "adaptive_compression_mode", "active")) == "active"
        compression = decide(content_class=content_class, source_complete=source_complete, size=len(snapshot), shadow=not active, target_bytes=int(getattr(settings, "context_result_reduce_chars", 4_000)))
        target_chars = max(512, compression.target_bytes)
        candidate = reduce_context_result(
            operation, clean_result, intent=context["intent"], target_chars=target_chars,
        )
        was_reduced = active and compression.mode == "compact" and len(canonical_json(candidate)) < len(snapshot)
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
        "manifest": {
            "handle": stored["id"] if stored is not None else None,
            "etag": digest,
            "sha256": digest,
            "source_complete": source_complete,
            "omitted": ["full_output", "unbounded_nested_values"],
            "suggested_offset": 0,
            "suggested_length": max(1, int(getattr(settings, "context_result_max_retrieval_chars", 12_000))),
            "reason": "source_incomplete_or_truncated" if not source_complete else "details_available_on_demand",
        },
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
