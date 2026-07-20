from __future__ import annotations

import hashlib
import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import telemetry
from mcp_server.main import _log
from mcp_server.context_results import (
    canonical_json,
    get_context_result,
    reduce_context_result,
    store_context_result,
)
from mcp_server.tools_workspace import workspace


def settings(root: Path, **overrides):
    values = {
        "allow_shell": True,
        "default_command_timeout_s": 5,
        "max_command_timeout_s": 10,
        "max_output_chars": 12_000,
        "workdir": root,
        "context_results_dir": root / "context-results",
        "context_result_ttl_s": 60,
        "context_result_max_entries": 8,
        "context_result_max_bytes": 1_000_000,
        "context_result_max_retrieval_chars": 1_000,
        "context_result_reduce_chars": 600,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class ContextResultStoreTests(unittest.TestCase):
    def test_store_is_private_and_retrieval_is_bounded_and_hash_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = settings(root, context_result_max_retrieval_chars=7)
            stored = store_context_result(config, "abcdefghijk", source_complete=True, now=10)
            directory = config.context_results_dir
            record = directory / f"{stored['id']}.json"

            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(record.stat().st_mode), 0o600)
            self.assertRegex(stored["id"], r"^[A-Za-z0-9_-]{32}$")
            self.assertEqual(stored["sha256"], hashlib.sha256(b"abcdefghijk").hexdigest())

            first = get_context_result(config, stored["id"], offset=2, length=50, now=11)
            self.assertEqual(first["content"], "cdefghi")
            self.assertEqual(first["returned_chars"], 7)
            self.assertTrue(first["has_more"])
            self.assertTrue(first["snapshot_complete"])
            self.assertTrue(first["source_complete"])

            cached = get_context_result(
                config, stored["id"], if_none_match=f'"{stored["sha256"]}"', now=11
            )
            self.assertTrue(cached["not_modified"])
            self.assertNotIn("content", cached)

    def test_ttl_and_invalid_handles_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = settings(Path(tmp), context_result_ttl_s=1)
            stored = store_context_result(config, "short lived", source_complete=False, now=20)

            with self.assertRaises(HTTPException) as invalid:
                get_context_result(config, "../not-a-handle")
            self.assertEqual(invalid.exception.status_code, 404)
            with self.assertRaises(HTTPException) as expired:
                get_context_result(config, stored["id"], now=22)
            self.assertEqual(expired.exception.status_code, 404)

    def test_entry_and_byte_limits_evict_oldest_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = settings(root, context_result_max_entries=2)
            first = store_context_result(config, "first", source_complete=True, now=1)
            store_context_result(config, "second", source_complete=True, now=2)
            store_context_result(config, "third", source_complete=True, now=3)
            with self.assertRaises(HTTPException):
                get_context_result(config, first["id"], now=4)
            self.assertEqual(len(list(config.context_results_dir.glob("*.json"))), 2)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = settings(root, context_result_max_entries=20, context_result_max_bytes=1_200)
            first = store_context_result(config, "a" * 100, source_complete=True, now=1)
            for index in range(2, 7):
                store_context_result(config, str(index) * 100, source_complete=True, now=index)
            records = list(config.context_results_dir.glob("*.json"))
            self.assertLessEqual(sum(path.stat().st_size for path in records), 1_200)
            with self.assertRaises(HTTPException):
                get_context_result(config, first["id"], now=7)

    def test_integrity_failure_removes_tampered_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = settings(Path(tmp))
            stored = store_context_result(config, "trusted", source_complete=True, now=10)
            record_path = config.context_results_dir / f"{stored['id']}.json"
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record["content"] = "tampered"
            record_path.write_text(json.dumps(record), encoding="utf-8")
            record_path.chmod(0o600)

            with self.assertRaises(HTTPException) as failure:
                get_context_result(config, stored["id"], now=11)
            self.assertEqual(failure.exception.status_code, 410)
            self.assertFalse(record_path.exists())


class ContextReductionTests(unittest.TestCase):
    def test_reducer_is_deterministic_and_idempotent(self):
        value = {"ok": True, "content": "0123456789" * 1_000, "path": "/tmp/example"}
        first = reduce_context_result("read_file", value, intent="summary", target_chars=600)
        second = reduce_context_result("read_file", value, intent="summary", target_chars=600)
        self.assertEqual(first, second)
        self.assertLess(len(first["content"]), len(value["content"]))

        already_transformed = {**first, "_context_result": {"reduced": True}}
        self.assertIs(reduce_context_result("read_file", already_transformed), already_transformed)

    def test_unknown_and_mutating_actions_are_conservative(self):
        value = {"ok": True, "content": "x" * 20_000}
        self.assertEqual(reduce_context_result("write_file", value), value)
        self.assertEqual(reduce_context_result("future_action", value), value)


class WorkspaceContextTests(unittest.TestCase):
    def test_auto_mode_reduces_and_retrieves_exact_bounded_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "large.txt"
            target.write_text("z" * 8_000, encoding="utf-8")
            config = settings(root)

            raw = workspace(config, "read_file", {
                "path": str(target), "length": 1,
                "_context": {"mode": "auto", "intent": "summary"},
            })
            result = json.loads(raw)
            metadata = result["_context_result"]
            self.assertTrue(metadata["reduced"])
            self.assertTrue(metadata["snapshot_complete"])
            self.assertTrue(metadata["source_complete"])
            self.assertEqual(raw.context_stored, 1)
            self.assertEqual(raw.context_reduced, 1)
            self.assertGreater(raw.context_stored_chars, len(result["content"]))

            chunks = []
            offset = 0
            while True:
                retrieved = json.loads(workspace(config, "get_context_result", {
                    "context_id": metadata["id"], "offset": offset, "length": 1_000,
                }))
                chunks.append(retrieved["content"])
                offset += retrieved["returned_chars"]
                if not retrieved["has_more"]:
                    break
            snapshot = "".join(chunks)
            self.assertEqual(hashlib.sha256(snapshot.encode()).hexdigest(), metadata["sha256"])
            self.assertEqual(snapshot, canonical_json(json.loads(snapshot)))

    def test_etag_and_source_incomplete_are_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "lines.txt"
            target.write_text("".join(f"line-{i}\n" for i in range(100)), encoding="utf-8")
            config = settings(root)
            initial = json.loads(workspace(config, "read_file", {
                "path": str(target), "length": 2, "_context": {"mode": "full"},
            }))
            metadata = initial["_context_result"]
            self.assertFalse(metadata["source_complete"])

            cached_raw = workspace(config, "read_file", {
                "path": str(target), "length": 2,
                "_context": {"mode": "auto", "if_none_match": metadata["etag"]},
            })
            cached = json.loads(cached_raw)
            self.assertTrue(cached["not_modified"])
            self.assertFalse(cached["source_complete"])
            self.assertEqual(cached_raw.context_not_modified, 1)
            self.assertEqual(cached_raw.context_source_incomplete, 1)

    def test_absent_context_control_preserves_legacy_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "small.txt"
            target.write_text("hello\n", encoding="utf-8")
            raw = workspace(settings(root), "read_file", {"path": str(target)})
            self.assertNotIn("_context_result", json.loads(raw))
            self.assertEqual(raw.context_stored, 0)

    def test_live_telemetry_aggregates_context_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = Path(tmp) / "audit.log"
            audit.write_text(
                "2026-07-20 03:01:00 | "
                '{"tool":"workspace:read_file","payload_chars":500,'
                '"context_stored_chars":5000,"context_original_chars":5000,'
                '"context_reduced_chars":400,"context_returned_chars":500,'
                '"context_saved_chars":4500,"context_retrieval_chars":0,'
                '"context_stored":1,"context_reduced":1,"context_retrieval":0,'
                '"context_not_modified":0,"context_source_incomplete":1}\n'
                "2026-07-20 03:02:00 | "
                '{"tool":"workspace:get_context_result","payload_chars":220,'
                '"context_retrieval_chars":100,'
                '"context_retrieval":1,"context_not_modified":1}\n',
                encoding="utf-8",
            )
            with patch.object(telemetry, "AUDIT_LOG", audit):
                result = telemetry.summarize_audit_metrics("all")
            self.assertEqual(result["contextStored"], 1)
            self.assertEqual(result["contextReduced"], 1)
            self.assertEqual(result["contextRetrievals"], 1)
            self.assertEqual(result["contextNotModified"], 1)
            self.assertEqual(result["contextSourceIncomplete"], 1)
            self.assertEqual(result["contextStoredChars"], 5_000)
            self.assertEqual(result["contextOriginalChars"], 5_000)
            self.assertEqual(result["contextReducedChars"], 400)
            self.assertEqual(result["contextReturnedChars"], 500)
            self.assertEqual(result["contextSavedChars"], 4_500)
            self.assertEqual(result["contextRetrievalChars"], 100)

    def test_audit_event_contains_only_numeric_context_metadata(self):
        class Logger:
            event = ""

            def info(self, value):
                self.event = value

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "audit.txt"
            target.write_text("sensitive" * 1_000, encoding="utf-8")
            logger = Logger()
            _log(logger, "workspace:read_file", lambda: workspace(settings(root), "read_file", {
                "path": str(target), "length": 1, "_context": {"mode": "auto"},
            }))
            event = json.loads(logger.event)
            context_keys = [key for key in event if key.startswith("context_")]
            self.assertTrue(context_keys)
            self.assertTrue(all(type(event[key]) is int for key in context_keys))
            serialized = json.dumps(event)
            self.assertNotIn("sensitive", serialized)
            self.assertNotIn("context-results", serialized)
            self.assertNotIn("sha256", serialized)
            self.assertNotIn("etag", serialized)


if __name__ == "__main__":
    unittest.main()
