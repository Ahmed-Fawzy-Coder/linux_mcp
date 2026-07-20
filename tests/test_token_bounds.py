from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import telemetry, tools_jobs
from mcp_server.main import WORKSPACE_DESCRIPTION
from mcp_server.tools_files import read_file, read_multiple_files
from mcp_server.tools_search import search_files
from mcp_server.tools_terminal import run_command
from mcp_server.tools_workspace import workspace


def settings(workdir: Path, max_output_chars: int = 12_000):
    return SimpleNamespace(
        allow_shell=True,
        default_command_timeout_s=5,
        max_command_timeout_s=10,
        max_output_chars=max_output_chars,
        workdir=workdir,
    )


class TokenBoundsTests(unittest.TestCase):
    def test_read_file_defaults_to_160_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "large.txt"
            target.write_text("".join(f"line-{i}\n" for i in range(1_000)), encoding="utf-8")

            result = read_file(settings(root), str(target))

            self.assertEqual(result["returned_lines"], 160)
            self.assertEqual(result["total_lines"], 1_000)
            self.assertTrue(result["has_more"])
            self.assertNotIn("line-999", result["content"])

    def test_read_multiple_files_rejects_more_than_eight(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = []
            for index in range(9):
                target = root / f"{index}.txt"
                target.write_text("ok\n", encoding="utf-8")
                paths.append(str(target))

            with self.assertRaises(HTTPException):
                read_multiple_files(settings(root), paths)

    def test_search_files_respects_result_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "matches.txt"
            target.write_text("".join(f"needle-{i}\n" for i in range(100)), encoding="utf-8")

            result = search_files(settings(root), "needle", str(root), max_results=7)

            self.assertTrue(result["ok"])
            self.assertEqual(result["match_count"], 7)
            self.assertTrue(result["has_more"])

    def test_run_command_uses_one_combined_output_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_command(
                settings(root, max_output_chars=200),
                "python3 -c \"import sys; print('o'*1000); print('e'*1000, file=sys.stderr)\"",
                tail_lines=100,
            )

            self.assertLessEqual(len(result["stdout"]) + len(result["stderr"]), 200)
            self.assertTrue(result["stdout_truncated"] or result["stderr_truncated"])
            self.assertNotIn("command", result)

    def test_run_command_truncation_preserves_latest_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_command(
                settings(root, max_output_chars=200),
                "python3 -c \"[print(f'line-{i:04d}-' + 'x'*30) for i in range(500)]\"",
            )

            self.assertTrue(result["stdout_truncated"])
            self.assertIn("line-0499", result["stdout"])
            self.assertNotIn("line-0400", result["stdout"])

    def test_run_command_honors_explicit_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            result = run_command(settings(Path.home()), "pwd", cwd=str(root))

            self.assertEqual(result["stdout"], str(root))

    def test_job_output_defaults_to_last_100_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = root / "jobs"
            job = jobs / "job123"
            job.mkdir(parents=True)
            (job / "meta.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")
            (job / "stdout.log").write_text(
                "".join(f"line-{i}\n" for i in range(300)), encoding="utf-8"
            )
            (job / "stderr.log").write_text("", encoding="utf-8")

            with patch.object(tools_jobs, "JOBS_DIR", jobs):
                result = tools_jobs.get_job_output(settings(root), "job123", stream="stdout")

            self.assertNotIn("line-0\n", result["stdout"])
            self.assertIn("line-299", result["stdout"])
            self.assertEqual(len(result["stdout"].splitlines()), 100)

    def test_job_output_char_limit_preserves_latest_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = root / "jobs"
            job = jobs / "job123"
            job.mkdir(parents=True)
            (job / "meta.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")
            (job / "stdout.log").write_text(
                "".join(f"line-{i:04d}-{'x' * 30}\n" for i in range(300)), encoding="utf-8"
            )
            (job / "stderr.log").write_text("", encoding="utf-8")

            with patch.object(tools_jobs, "JOBS_DIR", jobs):
                result = tools_jobs.get_job_output(
                    settings(root, max_output_chars=200), "job123", stream="stdout"
                )

            self.assertTrue(result["stdout_truncated"])
            self.assertIn("line-0299", result["stdout"])

    def test_workspace_returns_single_compact_json_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "large.txt"
            target.write_text("".join(f"line-{i}\n" for i in range(1_000)), encoding="utf-8")

            raw = workspace(settings(root), "read_file", {"path": str(target), "length": 20})
            result = json.loads(raw)

            self.assertIsInstance(raw, str)
            self.assertEqual(result["returned_lines"], 20)
            self.assertTrue(result["has_more"])
            self.assertNotIn("_telemetry", raw)
            self.assertGreater(raw.internal_discarded_chars, 0)
            self.assertGreater(raw.estimated_savable_chars, 0)
            self.assertLessEqual(raw.estimated_savable_chars, 40_000)

    def test_workspace_accepts_max_output_lines_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = workspace(
                settings(Path(tmp)),
                "run_command",
                {
                    "command": "printf 'one\\ntwo\\nthree\\n'",
                    "cwd": tmp,
                    "max_output_lines": 2,
                    "max_output_chars": 200,
                },
            )
            result = json.loads(raw)

            self.assertEqual(result["stdout"], "two\nthree")
            self.assertEqual(result["exit_code"], 0)

    def test_workspace_description_distinguishes_path_contracts(self):
        self.assertIn("search_files uses path as the absolute project-root directory", WORKSPACE_DESCRIPTION)
        self.assertIn("read_file uses path as the absolute full file path", WORKSPACE_DESCRIPTION)
        self.assertIn("never split it into path plus a file field", WORKSPACE_DESCRIPTION)
        self.assertIn("offset is zero-based", WORKSPACE_DESCRIPTION)
        self.assertIn("run_command uses cwd as the absolute project-root directory", WORKSPACE_DESCRIPTION)

    def test_workspace_read_file_rejects_split_path_with_retry_guidance(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(HTTPException) as raised:
                workspace(
                    settings(Path(tmp)),
                    "read_file",
                    {"path": tmp, "file": "package.json"},
                )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("path must be the absolute full file path", raised.exception.detail)
        self.assertIn("do not split it into path plus a file field", raised.exception.detail)

    def test_live_metrics_ignore_legacy_unmeasured_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = Path(tmp) / "audit.log"
            audit.write_text(
                "2026-07-20 03:00:00 | {\"tool\":\"workspace:read_file\",\"outcome\":\"ok\"}\n"
                "2026-07-20 03:01:00 | {\"tool\":\"workspace:read_file\",\"payload_chars\":12000,"
                "\"internal_discarded_chars\":1714246537,"
                "\"estimated_savable_chars\":28000,"
                "\"measured_segments\":1,\"truncated\":true}\n",
                encoding="utf-8",
            )
            with patch.object(telemetry, "AUDIT_LOG", audit):
                result = telemetry.summarize_audit_metrics("all")

            self.assertEqual(result["calls"], 2)
            self.assertEqual(result["measuredCalls"], 1)
            self.assertEqual(result["returnedTokensEstimate"], 3_000)
            self.assertEqual(result["internalDiscardedChars"], 1_714_246_537)
            self.assertEqual(result["estimatedBaselineChars"], 40_000)
            self.assertEqual(result["estimatedSavedChars"], 28_000)
            self.assertEqual(result["estimatedSavedTokens"], 7_000)
            self.assertAlmostEqual(result["estimatedSavingsRatio"], 0.7)
            self.assertEqual(result["boundedCalls"], 1)
            self.assertIn("caps the native-equivalent output", result["method"])

    def test_workspace_rejects_unknown_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(HTTPException):
                workspace(settings(Path(tmp)), "not_allowed", {})


if __name__ == "__main__":
    unittest.main()
