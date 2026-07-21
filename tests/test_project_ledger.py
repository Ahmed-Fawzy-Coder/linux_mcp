import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

from mcp_server.project_ledger import Ledger
from mcp_server.tools_workspace import workspace


class ProjectLedgerAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = tempfile.TemporaryDirectory()
        self.old = os.environ.get("LINUX_MCP_HOME")
        os.environ["LINUX_MCP_HOME"] = self.home.name
        self.root = Path(self.tmp.name)
        (self.root / "input.txt").write_text("stable", encoding="utf-8")

    def tearDown(self):
        if self.old is None:
            os.environ.pop("LINUX_MCP_HOME", None)
        else:
            os.environ["LINUX_MCP_HOME"] = self.old
        self.home.cleanup(); self.tmp.cleanup()

    def test_restart_recovery_and_checkpoint(self):
        first = Ledger(str(self.root)); first.task("t1", "c1", "ship feature")
        first.decision("t1", "c1", "use sqlite", "local durable state")
        first.checkpoint("t1", "c1", {"remaining": ["tests"]})
        recovered = Ledger(str(self.root)).state("t1", "c1")
        self.assertEqual(recovered["task"]["goal"], "ship feature")
        self.assertEqual(recovered["decisions"][0]["decision"], "use sqlite")
        self.assertIn("tests", recovered["checkpoint"]["snapshot"])

    def test_stale_invalidation_and_read_only_hit(self):
        ledger = Ledger(str(self.root), "v1")
        deps = {"input.txt": "hash-a"}
        key = ledger.cache_key("read_file", {"path": str(self.root / "input.txt")}, deps, "read-only")
        ledger.cache_put(key, "read_file", {"content": "stable"}, deps, "read-only")
        self.assertEqual(ledger.cache_get(key)["content"], "stable")
        stale_key = ledger.cache_key("read_file", {"path": str(self.root / "input.txt")}, {"input.txt": "hash-b"}, "read-only")
        self.assertIsNone(ledger.cache_get(stale_key))

    def test_redaction_and_no_raw_content_in_facts(self):
        ledger = Ledger(str(self.root)); ledger.fact("t", "c", "read_file", {"content": "API_KEY=super-secret", "path": "x"})
        with sqlite3.connect(ledger.db) as db:
            raw = db.execute("select payload from facts").fetchone()[0]
        self.assertNotIn("super-secret", raw); self.assertIn("sha256", raw)

    def test_cross_project_and_cross_conversation_denied(self):
        other = Path(self.tmp.name).parent / (self.root.name + "-other"); other.mkdir()
        try:
            Ledger(str(self.root)).task("t", "c1", "goal")
            with self.assertRaises(PermissionError): Ledger(str(self.root)).state("t", "c2")
            with self.assertRaises(PermissionError): Ledger(str(other)).state("t", "c1")
        finally:
            other.rmdir()

    def test_mutations_and_network_policy_are_not_cacheable(self):
        ledger = Ledger(str(self.root))
        self.assertIsNone(ledger.cache_key("edit_file", {"path": "x"}, {}, "read-only"))
        self.assertIsNone(ledger.cache_key("run_command", {"command": "date"}, {}, "read-only"))

    def test_workspace_read_only_work_is_not_repeated(self):
        settings = SimpleNamespace(workdir=self.root, max_output_chars=12000)
        args = {"path": str(self.root / "input.txt"), "task_id": "t", "conversation_id": "c", "_context": False}
        first = workspace(settings, "read_file", args)
        second = workspace(settings, "read_file", args)
        self.assertNotIn("exact-hit", first)
        self.assertIn("exact-hit", second)


if __name__ == "__main__":
    unittest.main()
