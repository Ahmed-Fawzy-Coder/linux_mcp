from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "scripts" / "codex_linux_mcp_guard.py"
SPEC = importlib.util.spec_from_file_location("codex_linux_mcp_guard", SCRIPT)
assert SPEC and SPEC.loader
GUARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GUARD)


class CodexLinuxMcpGuardTests(unittest.TestCase):
    def test_blocks_native_exec_command(self) -> None:
        reason = GUARD.decision(
            {
                "tool_name": "exec",
                "cwd": "/media/ahmed/New Volume/Coding/SAAS/GapHunter",
                "tool_input": {"code": "await tools.exec_command({cmd: 'pwd'})"},
            }
        )
        self.assertIn("native exec_command", reason or "")

    def test_allows_workspace_and_apply_patch(self) -> None:
        workspace = GUARD.decision(
            {
                "tool_name": "exec",
                "cwd": "/media/ahmed/New Volume/Coding/SAAS/GapHunter",
                "tool_input": {
                    "code": "await tools.mcp__linux_mcp__workspace({action:'read_file',arguments:{path:'/media/ahmed/New Volume/Coding/SAAS/GapHunter/app.js'}})"
                },
            }
        )
        patch_result = GUARD.decision(
            {
                "tool_name": "exec",
                "tool_input": {"code": "await tools.apply_patch('*** Begin Patch')"},
            }
        )
        self.assertIsNone(workspace)
        self.assertIsNone(patch_result)

    def test_blocks_cross_project_workspace_path(self) -> None:
        reason = GUARD.decision(
            {
                "tool_name": "exec",
                "cwd": "/media/ahmed/New Volume/Coding/SAAS/GapHunter",
                "tool_input": {
                    "code": "await tools.mcp__linux_mcp__workspace({action:'read_file',arguments:{path:'/media/ahmed/New Volume/Coding/Freelance/Haweyat/.env'}})"
                },
            }
        )
        self.assertIn("cross-project path", reason or "")

    def test_blocks_haweyat_container_from_gaphunter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            coding = Path(temporary) / "Coding"
            gaphunter = coding / "SAAS" / "GapHunter"
            haweyat = coding / "Freelance" / "Haweyat"
            gaphunter.mkdir(parents=True)
            haweyat.mkdir(parents=True)
            code = (
                "await tools.mcp__linux_mcp__workspace({action:'run_command',arguments:{"
                f"cwd:'{gaphunter}',command:'docker inspect haweyat-postgres-1'"
                "}})"
            )
            reason = GUARD.decision(
                {"tool_name": "exec", "cwd": str(gaphunter), "tool_input": {"code": code}}
            )
        self.assertIn("belongs to project haweyat", reason or "")

    def test_allows_linux_mcp_recovery_command(self) -> None:
        reason = GUARD.decision(
            {
                "tool_name": "exec",
                "tool_input": {
                    "code": "await tools.exec_command({cmd:'systemctl --user restart linux-mcp.service'})"
                },
            }
        )
        self.assertIsNone(reason)


if __name__ == "__main__":
    unittest.main()
