from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class CompressionPolicy:
    risk: str; content_class: str; mode: str; target_bytes: int; preserve: tuple[str, ...]; auto_retrieve: bool; reason_codes: tuple[str, ...]; shadow: bool

SAFE = {"logs", "search", "json"}
EXACT = {"auth", "security", "database", "migrations", "config", "diff"}

def decide(*, content_class: str, mutating: bool = False, source_complete: bool = True, size: int = 0, shadow: bool = True, target_bytes: int = 4000) -> CompressionPolicy:
    if mutating or content_class in EXACT or not source_complete:
        return CompressionPolicy("exact" if content_class in EXACT else "high", content_class, "store", max(size, target_bytes), ("exact", "line_numbers"), False, ("risk_exact_or_incomplete",), shadow)
    if content_class in SAFE and size > target_bytes:
        return CompressionPolicy("low", content_class, "compact", target_bytes, ("errors", "frames", "line_numbers"), True, ("safe_allowlist",), shadow)
    return CompressionPolicy("medium", content_class, "off", size, ("full",), False, ("unknown_fail_open",), shadow)
