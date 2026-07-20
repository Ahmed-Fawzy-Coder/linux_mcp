from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server.context_results import get_context_result
from mcp_server.tools_files import read_file
from mcp_server.tools_workspace import workspace


def settings(root: Path):
    return SimpleNamespace(
        max_output_chars=12_000,
        context_results_dir=root / "context-results",
        context_result_ttl_s=60,
        context_result_max_entries=8,
        context_result_max_bytes=1_000_000,
        context_result_max_retrieval_chars=1_000,
        context_result_reduce_chars=600,
    )


class FileReadFreshnessTests(unittest.TestCase):
    def test_atomic_replace_during_open_retries_new_inode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "tracked.ts"
            replacement = root / ".tracked.ts.new"
            target.write_text("old-generation\n", encoding="utf-8")
            replacement.write_text("new-generation\n", encoding="utf-8")

            from mcp_server import tools_files

            real_open = tools_files._open_read_fd
            replaced = False

            def open_then_replace(path: Path) -> int:
                nonlocal replaced
                fd = real_open(path)
                if not replaced:
                    replaced = True
                    os.replace(replacement, target)
                return fd

            with patch.object(tools_files, "_open_read_fd", side_effect=open_then_replace):
                result = read_file(settings(root), str(target))

            self.assertTrue(replaced)
            self.assertEqual(result["content"], "new-generation\n")

    def test_conditional_offset_read_invalidates_when_unread_bytes_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "tracked.ts"
            target.write_text("unchanged-header\nold-tail\n", encoding="utf-8")
            config = settings(root)

            initial = json.loads(workspace(config, "read_file", {
                "path": str(target),
                "offset": 0,
                "length": 1,
                "_context": {"mode": "store"},
            }))
            old_etag = initial["_context_result"]["etag"]
            old_stat = target.stat()

            target.write_text("unchanged-header\nnew-tail\n", encoding="utf-8")
            os.utime(target, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
            self.assertEqual(target.stat().st_ino, old_stat.st_ino)

            current = json.loads(workspace(config, "read_file", {
                "path": str(target),
                "offset": 0,
                "length": 1,
                "_context": {"mode": "store", "if_none_match": old_etag},
            }))
            self.assertNotIn("not_modified", current)
            self.assertEqual(current["content"], "unchanged-header\n")
            metadata = current["_context_result"]
            self.assertNotEqual(metadata["etag"], old_etag)
            cached_snapshot = get_context_result(config, metadata["id"], if_none_match=metadata["etag"])
            self.assertTrue(cached_snapshot["not_modified"])

    def test_offset_read_observes_same_size_same_mtime_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "tracked.ts"
            target.write_text("header\nold-tail\n", encoding="utf-8")
            old_stat = target.stat()
            self.assertEqual(read_file(settings(root), str(target), offset=1, length=1)["content"], "old-tail\n")

            replacement = root / ".tracked.ts.new"
            replacement.write_text("header\nnew-tail\n", encoding="utf-8")
            self.assertEqual(replacement.stat().st_size, old_stat.st_size)
            os.utime(replacement, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
            os.replace(replacement, target)

            current = read_file(settings(root), str(target), offset=1, length=1)
            self.assertEqual(current["content"], "new-tail\n")


if __name__ == "__main__":
    unittest.main()
