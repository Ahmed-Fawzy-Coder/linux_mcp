#!/usr/bin/env python3
"""Block native Codex workspace operations that bypass Linux MCP."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any


BLOCKED_DIRECT_TOOLS = {
    "bash",
    "exec_command",
    "shell_command",
    "read_file",
    "grep",
    "glob",
}
NATIVE_EXEC_MARKERS = ("tools.exec_command(", "tools.write_stdin(")
WORKSPACE_MARKER = "tools.mcp__linux_mcp__workspace("
RECOVERY_PATTERN = re.compile(
    r"(?:systemctl\s+--user\s+(?:is-active|status|start|restart)\s+[^\n;]*(?:linux-mcp\.service|linux-mcp\.socket)"
    r"|curl\s+[^\n;]*127\.0\.0\.1:8000(?:/mcp|/health|/metrics)?)",
    re.IGNORECASE,
)
ABSOLUTE_PROJECT_PATH = re.compile(
    r"(?P<path>/media/[^\"'\n]+?/Coding/[^\"'\n]+)",
    re.IGNORECASE,
)
DOCKER_TARGET = re.compile(
    r"\bdocker\s+(?:container\s+)?(?:inspect|logs|exec|start|stop|restart|rm|kill)\s+"
    r"(?:--?[\w.-]+(?:=[^\s]+)?\s+)*(?P<target>[A-Za-z0-9][A-Za-z0-9_.-]*)",
    re.IGNORECASE,
)


def _payload_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _coding_root(path: Path) -> Path | None:
    parts = path.parts
    try:
        index = parts.index("Coding")
    except ValueError:
        return None
    return Path(*parts[: index + 1])


def _known_project_names(cwd: Path) -> set[str]:
    coding_root = _coding_root(cwd)
    if coding_root is None or not coding_root.is_dir():
        return set()

    names: set[str] = set()
    try:
        for category in coding_root.iterdir():
            if not category.is_dir():
                continue
            for project in category.iterdir():
                if project.is_dir():
                    names.add(project.name.casefold())
    except OSError:
        return set()
    return names


def _cross_project_reason(text: str, cwd_text: str) -> str | None:
    if not cwd_text:
        return None
    cwd = Path(cwd_text).resolve(strict=False)
    coding_root = _coding_root(cwd)
    if coding_root is None:
        return None

    for match in ABSOLUTE_PROJECT_PATH.finditer(text):
        candidate = Path(match.group("path")).resolve(strict=False)
        if _is_within(candidate, coding_root) and not _is_within(candidate, cwd):
            return f"cross-project path is outside the task root: {candidate} (task root: {cwd})"

    docker_match = DOCKER_TARGET.search(text)
    if docker_match:
        target = docker_match.group("target").casefold()
        current_name = cwd.name.casefold()
        for project_name in _known_project_names(cwd):
            if project_name != current_name and re.match(
                rf"^{re.escape(project_name)}(?:[-_.]|$)", target
            ):
                return (
                    f"Docker target {docker_match.group('target')} belongs to project "
                    f"{project_name}, not the current task root {cwd}"
                )
    return None


def decision(payload: dict[str, Any]) -> str | None:
    """Return a blocking reason, or None when the tool call is allowed."""

    tool_name = str(payload.get("tool_name") or payload.get("toolName") or "").casefold()
    tool_input = payload.get("tool_input", payload.get("toolInput", {}))
    text = _payload_text(tool_input)
    cwd = str(payload.get("cwd") or payload.get("working_directory") or "")

    if tool_name in BLOCKED_DIRECT_TOOLS:
        return f"native tool {tool_name} is disabled; use mcp__linux_mcp__workspace"

    if tool_name == "exec":
        if any(marker in text for marker in NATIVE_EXEC_MARKERS):
            if RECOVERY_PATTERN.search(text) and "linux-mcp" in text.casefold():
                return None
            return "native exec_command is disabled; call mcp__linux_mcp__workspace instead"

        if WORKSPACE_MARKER in text:
            return _cross_project_reason(text, cwd)

    if tool_name in {"mcp__linux_mcp__workspace", "linux_mcp.workspace"}:
        return _cross_project_reason(text, cwd)

    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Linux MCP guard could not parse hook input: {exc}", file=sys.stderr)
        return 2

    reason = decision(payload)
    if reason is None:
        return 0

    print(
        "LINUX_MCP_REQUIRED: "
        f"{reason}. Retry with the workspace gateway and the task's exact cwd/path.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
