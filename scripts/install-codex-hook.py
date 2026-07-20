#!/usr/bin/env python3
"""Install the global Linux MCP enforcement hook for Codex."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path


HOOK_MATCHER = "exec|Bash|exec_command|shell_command|read_file|grep|glob|mcp__linux_mcp__workspace"


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    guard = repo_root / "scripts" / "codex_linux_mcp_guard.py"
    codex_dir = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    hooks_path = codex_dir / "hooks.json"
    codex_dir.mkdir(parents=True, exist_ok=True)

    data: dict[str, object] = {}
    if hooks_path.exists():
        data = json.loads(hooks_path.read_text(encoding="utf-8"))
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        shutil.copy2(hooks_path, hooks_path.with_name(f"hooks.json.before-linux-mcp-{stamp}"))

    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks.json contains a non-object 'hooks' value")
    existing = hooks.setdefault("PreToolUse", [])
    if not isinstance(existing, list):
        raise ValueError("hooks.json contains a non-array PreToolUse value")

    existing[:] = [
        item
        for item in existing
        if "graphify hook-check" not in json.dumps(item)
        and "codex_linux_mcp_guard.py" not in json.dumps(item)
    ]
    existing.append(
        {
            "matcher": HOOK_MATCHER,
            "hooks": [
                {
                    "type": "command",
                    "command": str(guard),
                    "timeout": 5,
                }
            ],
        }
    )

    temporary = hooks_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(hooks_path)
    guard.chmod(0o755)

    print(f"Installed Linux MCP guard in {hooks_path}")
    print("Restart Codex and approve the new global PreToolUse hook once when prompted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
