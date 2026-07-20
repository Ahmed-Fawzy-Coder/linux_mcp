#!/usr/bin/env python3
"""Deterministic ten-round benchmark for reversible Linux MCP context results."""

from __future__ import annotations

import hashlib
import json
import statistics
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from mcp_server.tools_workspace import workspace


ROUNDS = 10


def settings(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        allow_shell=True,
        default_command_timeout_s=5,
        max_command_timeout_s=10,
        max_output_chars=12_000,
        workdir=root,
        context_results_dir=root / "context-results",
        context_result_ttl_s=60,
        context_result_max_entries=32,
        context_result_max_bytes=4_000_000,
        context_result_max_retrieval_chars=1_000,
        context_result_reduce_chars=600,
    )


def main() -> None:
    rows: list[dict[str, int | float | bool]] = []
    with tempfile.TemporaryDirectory(prefix="linux-mcp-context-benchmark-") as tmp:
        root = Path(tmp)
        config = settings(root)
        target = root / "large.txt"

        for round_number in range(1, ROUNDS + 1):
            prefix = f"round={round_number} "
            target.write_text(prefix + ("x" * (10_145 - len(prefix))), encoding="utf-8")
            arguments = {
                "path": str(target),
                "length": 1,
                "_context": {"mode": "auto", "intent": "summary"},
            }

            started = time.perf_counter()
            transformed = workspace(config, "read_file", arguments)
            latency_ms = (time.perf_counter() - started) * 1_000
            payload = json.loads(transformed)
            metadata = payload["_context_result"]

            chunks: list[str] = []
            offset = 0
            while True:
                chunk = json.loads(workspace(config, "get_context_result", {
                    "context_id": metadata["id"],
                    "offset": offset,
                    "length": 1_000,
                }))
                chunks.append(chunk["content"])
                offset += chunk["returned_chars"]
                if not chunk["has_more"]:
                    break
            snapshot = "".join(chunks)
            hash_ok = hashlib.sha256(snapshot.encode()).hexdigest() == metadata["sha256"]

            retry = json.loads(workspace(config, "read_file", arguments))
            retry_metadata = retry["_context_result"]
            retry_deterministic = (
                retry.get("content") == payload.get("content")
                and all(retry_metadata.get(key) == metadata.get(key)
                        for key in ("sha256", "etag"))
            )
            cached = json.loads(workspace(config, "read_file", {
                **arguments,
                "_context": {
                    "mode": "auto",
                    "intent": "summary",
                    "if_none_match": metadata["etag"],
                },
            }))
            row = {
                "round": round_number,
                "beforeChars": transformed.context_original_chars,
                "returnedChars": transformed.payload_chars,
                "savedChars": transformed.context_saved_chars,
                "latencyMs": round(latency_ms, 3),
                "retrievalHashOk": hash_ok,
                "retryDeterministic": retry_deterministic,
                "cacheNotModified": cached.get("not_modified") is True,
            }
            rows.append(row)
            print(json.dumps({"type": "round", **row}, separators=(",", ":")))

    latencies = sorted(float(row["latencyMs"]) for row in rows)
    aggregate = {
        "rounds": len(rows),
        "beforeChars": sum(int(row["beforeChars"]) for row in rows),
        "returnedChars": sum(int(row["returnedChars"]) for row in rows),
        "savedChars": sum(int(row["savedChars"]) for row in rows),
        "medianLatencyMs": round(statistics.median(latencies), 3),
        "worstLatencyMs": max(latencies),
        "retrievalHashSuccesses": sum(bool(row["retrievalHashOk"]) for row in rows),
        "deterministicRetries": sum(bool(row["retryDeterministic"]) for row in rows),
        "cacheNotModified": sum(bool(row["cacheNotModified"]) for row in rows),
    }
    print(json.dumps({"type": "aggregate", **aggregate}, separators=(",", ":")))


if __name__ == "__main__":
    main()
