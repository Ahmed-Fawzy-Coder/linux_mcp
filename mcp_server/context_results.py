from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import HTTPException, status


DEFAULT_TTL_S = 3_600
DEFAULT_MAX_ENTRIES = 128
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
DEFAULT_RETRIEVAL_CHARS = 12_000
MAX_RETRIEVAL_CHARS = 40_000
DEFAULT_REDUCED_CHARS = 4_000
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_-]{32}$")
_LOCK = threading.RLock()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def normalize_etag(value: Any) -> str:
    validator = str(value or "").strip()
    if validator.startswith("W/"):
        validator = validator[2:].strip()
    return validator.strip('"')


def _positive(value: Any, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def _store_dir(settings: Any) -> Path:
    configured = getattr(settings, "context_results_dir", None)
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path(getattr(settings, "workdir", Path.home())) / ".linux-mcp-context-results").resolve()


def _limits(settings: Any) -> tuple[int, int, int, int]:
    return (
        _positive(getattr(settings, "context_result_ttl_s", DEFAULT_TTL_S), DEFAULT_TTL_S),
        _positive(getattr(settings, "context_result_max_entries", DEFAULT_MAX_ENTRIES), DEFAULT_MAX_ENTRIES),
        _positive(getattr(settings, "context_result_max_bytes", DEFAULT_MAX_BYTES), DEFAULT_MAX_BYTES),
        min(
            MAX_RETRIEVAL_CHARS,
            _positive(
                getattr(settings, "context_result_max_retrieval_chars", DEFAULT_RETRIEVAL_CHARS),
                DEFAULT_RETRIEVAL_CHARS,
            ),
        ),
    )


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Context result storage is not a private directory.")
    path.chmod(0o700)


def _record_paths(path: Path) -> list[Path]:
    return [item for item in path.glob("*.json") if item.is_file() and not item.is_symlink()]


def _read_record(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if path.is_symlink() or (path.stat().st_mode & 0o077):
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("content"), str):
            return None
        return value
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _evict(directory: Path, *, now: float, max_entries: int, max_bytes: int) -> None:
    live: list[tuple[float, str, Path, int]] = []
    for path in _record_paths(directory):
        record = _read_record(path)
        if record is None:
            _unlink(path)
            continue
        expires_at = float(record.get("expires_at", 0))
        if expires_at <= now:
            _unlink(path)
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        live.append((float(record.get("created_at", 0)), path.name, path, size))
    live.sort()
    total = sum(item[3] for item in live)
    while live and (len(live) > max_entries or total > max_bytes):
        _, _, path, size = live.pop(0)
        _unlink(path)
        total -= size


def store_context_result(
    settings: Any,
    content: str,
    *,
    source_complete: bool,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    current = time.time() if now is None else float(now)
    ttl_s, max_entries, max_bytes, _ = _limits(settings)
    encoded_bytes = len(content.encode("utf-8"))
    if encoded_bytes > max_bytes:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Context result exceeds the configured store size.")
    directory = _store_dir(settings)
    try:
        with _LOCK:
            _ensure_private_dir(directory)
            _evict(directory, now=current, max_entries=max_entries - 1, max_bytes=max(0, max_bytes - encoded_bytes))
            while True:
                handle = secrets.token_urlsafe(24)
                if not _HANDLE_RE.fullmatch(handle):  # pragma: no cover - token_urlsafe(24) is exactly 32 URL-safe chars
                    raise RuntimeError("Failed to generate a context handle.")
                target = directory / f"{handle}.json"
                if not target.exists():
                    break
            digest = content_sha256(content)
            record = canonical_json({
                "content": content,
                "created_at": current,
                "expires_at": current + ttl_s,
                "sha256": digest,
                "snapshot_complete": True,
                "source_complete": bool(source_complete),
            })
            fd, temporary_name = tempfile.mkstemp(prefix=".context-", dir=str(directory))
            temporary = Path(temporary_name)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as output:
                    output.write(record)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, target)
                target.chmod(0o600)
                directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                _unlink(temporary)
                raise
            _evict(directory, now=current, max_entries=max_entries, max_bytes=max_bytes)
            if not target.exists():
                raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Context result could not fit in the configured store.")
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Context result storage failed.") from exc
    return {
        "id": handle,
        "sha256": digest,
        "etag": digest,
        "stored_chars": len(content),
        "stored_bytes": encoded_bytes,
        "expires_at": int((current + ttl_s) * 1000),
        "snapshot_complete": True,
        "source_complete": bool(source_complete),
    }


def get_context_result(
    settings: Any,
    context_id: str,
    *,
    offset: int = 0,
    length: Optional[int] = None,
    if_none_match: Optional[str] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    if not isinstance(context_id, str) or not _HANDLE_RE.fullmatch(context_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Context result not found.")
    try:
        start = max(0, int(offset))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "offset must be an integer.") from exc
    current = time.time() if now is None else float(now)
    ttl_s, max_entries, max_bytes, max_retrieval = _limits(settings)
    requested = max_retrieval if length is None else _positive(length, max_retrieval)
    requested = min(requested, max_retrieval)
    directory = _store_dir(settings)
    try:
        with _LOCK:
            if not directory.exists():
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Context result not found.")
            _ensure_private_dir(directory)
            _evict(directory, now=current, max_entries=max_entries, max_bytes=max_bytes)
            path = directory / f"{context_id}.json"
            record = _read_record(path)
            if record is None or float(record.get("expires_at", 0)) <= current:
                _unlink(path)
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Context result not found.")
            content = record["content"]
            digest = str(record.get("sha256", ""))
            if digest != content_sha256(content):
                _unlink(path)
                raise HTTPException(status.HTTP_410_GONE, "Context result failed integrity verification.")
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Context result retrieval failed.") from exc
    validator = normalize_etag(if_none_match)
    base = {
        "ok": True,
        "context_id": context_id,
        "sha256": digest,
        "etag": digest,
        "total_chars": len(content),
        "snapshot_complete": bool(record.get("snapshot_complete") is True),
        "source_complete": bool(record.get("source_complete") is True),
    }
    if validator and secrets.compare_digest(validator, digest):
        return {**base, "not_modified": True, "offset": start, "returned_chars": 0, "has_more": False}
    chunk = content[start:start + requested]
    return {
        **base,
        "not_modified": False,
        "content": chunk,
        "offset": start,
        "returned_chars": len(chunk),
        "has_more": start + len(chunk) < len(content),
    }


def _shorten_text(value: str, budget: int, *, prefer_tail: bool = False) -> str:
    if len(value) <= budget:
        return value
    marker = "\n... [context reduced; retrieve stored snapshot] ...\n"
    available = max(0, budget - len(marker))
    if prefer_tail:
        head = available // 4
    else:
        head = available * 2 // 3
    return value[:head] + marker + value[-(available - head):]


def reduce_context_result(action: str, value: Any, *, intent: str = "", target_chars: int = DEFAULT_REDUCED_CHARS) -> Any:
    """Deterministically reduce only known, potentially large workspace result fields."""
    if not isinstance(value, dict) or "_context_result" in value:
        return value
    target = max(512, int(target_chars))
    operation = (action or "").strip()
    reduced = json.loads(canonical_json(value))
    if operation == "read_file" and isinstance(reduced.get("content"), str):
        reduced["content"] = _shorten_text(reduced["content"], target)
    elif operation == "read_multiple_files" and isinstance(reduced.get("files"), list):
        per_file = max(256, target // max(1, len(reduced["files"])))
        for item in reduced["files"]:
            if isinstance(item, dict) and isinstance(item.get("content"), str):
                item["content"] = _shorten_text(item["content"], per_file)
    elif operation == "search_files" and isinstance(reduced.get("results"), list):
        kept = []
        used = 0
        for result in reduced["results"]:
            rendered = str(result)
            if kept and used + len(rendered) > target:
                break
            kept.append(result)
            used += len(rendered)
        reduced["results"] = kept
        if len(kept) < len(value.get("results", [])):
            reduced["context_results_omitted"] = len(value["results"]) - len(kept)
    elif operation in {"run_command", "get_job_output"}:
        prefer_stderr = "error" in intent.lower() or "debug" in intent.lower()
        for key in ("stderr", "stdout"):
            if isinstance(reduced.get(key), str):
                budget = target * (2 if (key == "stderr") == prefer_stderr else 1) // 3
                reduced[key] = _shorten_text(reduced[key], max(256, budget), prefer_tail=True)
    elif operation in {"run_commands_parallel", "wait_jobs"} and isinstance(reduced.get("jobs"), list):
        per_job = max(256, target // max(1, len(reduced["jobs"])))
        for job in reduced["jobs"]:
            if not isinstance(job, dict):
                continue
            for key in ("stderr", "stdout", "output"):
                if isinstance(job.get(key), str):
                    job[key] = _shorten_text(job[key], per_job, prefer_tail=True)
    return reduced
