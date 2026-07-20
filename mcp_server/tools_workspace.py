from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional

from fastapi import HTTPException, status

from .security import Settings
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


class WorkspaceResult(str):
    """String result carrying server-side telemetry that is not sent to the model."""

    payload_chars: int
    estimated_unbounded_chars: int
    avoided_chars: int
    measured_segments: int
    truncated: bool


def _strip_telemetry(value: Any) -> tuple[Any, Dict[str, Any]]:
    totals: Dict[str, Any] = {
        "source_chars": 0,
        "returned_content_chars": 0,
        "measured_segments": 0,
        "truncated": False,
    }

    def visit(item: Any) -> Any:
        if isinstance(item, dict):
            clean = {}
            metric = item.get("_telemetry")
            if isinstance(metric, dict):
                totals["source_chars"] += max(0, int(metric.get("source_chars", 0)))
                totals["returned_content_chars"] += max(0, int(metric.get("returned_content_chars", 0)))
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
    handler = handlers.get(operation)
    if handler is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unknown action: {operation}. Allowed: {', '.join(handlers)}",
        )
    payload = dict(arguments or {})
    try:
        result = handler(**payload)
    except (KeyError, TypeError) as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Invalid arguments for {operation}: {exc}",
        ) from exc
    clean_result, telemetry = _strip_telemetry(result)
    payload = json.dumps(clean_result, ensure_ascii=False, separators=(",", ":"))
    returned_content = telemetry["returned_content_chars"]
    avoided = max(0, telemetry["source_chars"] - returned_content)
    measured = WorkspaceResult(payload)
    measured.payload_chars = len(payload)
    measured.estimated_unbounded_chars = len(payload) + avoided
    measured.avoided_chars = avoided
    measured.measured_segments = telemetry["measured_segments"]
    measured.truncated = telemetry["truncated"]
    return measured
