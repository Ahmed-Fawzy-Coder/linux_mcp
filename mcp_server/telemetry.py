from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from .security import BASE_DIR


AUDIT_LOG = BASE_DIR / "audit.log"
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def _since_for_range(value: str, now: datetime) -> Optional[datetime]:
    if value == "7d":
        return now - timedelta(days=7)
    if value == "all":
        return None
    return now - timedelta(days=30)


def _tokens(chars: int) -> int:
    # Model tokenizers differ. Four characters per token is deliberately
    # exposed as an estimate rather than reported as provider token usage.
    return math.ceil(max(0, chars) / 4)


def summarize_audit_metrics(range_value: str = "30d", now: Optional[datetime] = None) -> Dict[str, Any]:
    current = now or datetime.now()
    selected_range = range_value if range_value in {"7d", "30d", "all"} else "30d"
    since = _since_for_range(selected_range, current)
    calls = measured_calls = measured_segments = bounded_calls = 0
    returned_chars = internal_discarded_chars = estimated_saved_chars = 0
    context_stored_chars = context_original_chars = context_reduced_chars = 0
    context_returned_chars = context_saved_chars = context_retrieval_chars = 0
    context_stored = context_reduced = context_retrieval = 0
    context_not_modified = context_source_incomplete = 0
    started_at: Optional[datetime] = None

    if AUDIT_LOG.exists():
        with AUDIT_LOG.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    stamp, raw = line.rstrip("\n").split(" | ", 1)
                    timestamp = datetime.strptime(stamp, TIMESTAMP_FORMAT)
                    event = json.loads(raw)
                except (ValueError, json.JSONDecodeError):
                    continue
                if since is not None and timestamp < since:
                    continue
                if not str(event.get("tool", "")).startswith("workspace:"):
                    continue
                calls += 1
                if not isinstance(event.get("payload_chars"), int):
                    continue
                measured_calls += 1
                measured_segments += max(0, int(event.get("measured_segments", 0)))
                returned_chars += max(0, int(event["payload_chars"]))
                internal_discarded_chars += max(0, int(event.get("internal_discarded_chars", 0)))
                estimated_saved_chars += max(0, int(event.get("estimated_savable_chars", 0)))
                bounded_calls += int(event.get("truncated") is True)
                context_stored_chars += max(0, int(event.get("context_stored_chars", 0)))
                context_original_chars += max(0, int(event.get("context_original_chars", 0)))
                context_reduced_chars += max(0, int(event.get("context_reduced_chars", 0)))
                context_returned_chars += max(0, int(event.get("context_returned_chars", 0)))
                context_saved_chars += max(0, int(event.get("context_saved_chars", 0)))
                context_retrieval_chars += max(0, int(event.get("context_retrieval_chars", 0)))
                context_stored += max(0, int(event.get("context_stored", 0)))
                context_reduced += max(0, int(event.get("context_reduced", 0)))
                context_retrieval += max(0, int(event.get("context_retrieval", 0)))
                context_not_modified += max(0, int(event.get("context_not_modified", 0)))
                context_source_incomplete += max(0, int(event.get("context_source_incomplete", 0)))
                if started_at is None or timestamp < started_at:
                    started_at = timestamp

    returned_tokens = _tokens(returned_chars)
    estimated_baseline_chars = returned_chars + estimated_saved_chars
    return {
        "version": 1,
        "range": selected_range,
        "generatedAt": int(current.timestamp() * 1000),
        "startedAt": int(started_at.timestamp() * 1000) if started_at else None,
        "calls": calls,
        "measuredCalls": measured_calls,
        "measuredSegments": measured_segments,
        "boundedCalls": bounded_calls,
        "returnedChars": returned_chars,
        "internalDiscardedChars": internal_discarded_chars,
        "estimatedBaselineChars": estimated_baseline_chars,
        "estimatedSavedChars": estimated_saved_chars,
        "returnedTokensEstimate": returned_tokens,
        "estimatedSavedTokens": _tokens(estimated_saved_chars),
        "estimatedSavingsRatio": (
            estimated_saved_chars / estimated_baseline_chars
            if estimated_baseline_chars else 0
        ),
        "contextStored": context_stored,
        "contextReduced": context_reduced,
        "contextRetrievals": context_retrieval,
        "contextNotModified": context_not_modified,
        "contextSourceIncomplete": context_source_incomplete,
        "contextStoredChars": context_stored_chars,
        "contextOriginalChars": context_original_chars,
        "contextReducedChars": context_reduced_chars,
        "contextReturnedChars": context_returned_chars,
        "contextSavedChars": context_saved_chars,
        "contextRetrievalChars": context_retrieval_chars,
        "method": (
            "live estimate; each measured operation caps the native-equivalent output at "
            "40000 characters; token estimates use 4 chars/token"
        ),
    }
