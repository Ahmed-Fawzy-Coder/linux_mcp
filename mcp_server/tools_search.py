from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any, Dict, List, Optional

from .security import Settings, resolve_path, truncate


def search_files(settings: Settings, pattern: str, path: str = str(Path.home()),
                 include_extensions: Optional[List[str]] = None,
                 case_sensitive: bool = False) -> Dict[str, Any]:
    """Search file contents using grep (recursive)."""
    root = resolve_path(path)
    flags = ["-rn", "--include=*"]
    if not case_sensitive:
        flags.append("-i")

    cmd = ["grep"] + flags
    if include_extensions:
        cmd = ["grep", "-rn"] + ([] if case_sensitive else ["-i"])
        for ext in include_extensions:
            cmd.extend(["--include", f"*.{ext.lstrip('.')}"])

    cmd += [pattern, str(root)]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        stdout, truncated = truncate(proc.stdout, 50_000)
        lines = stdout.splitlines()
        return {
            "ok": True,
            "match_count": len(lines),
            "results": stdout,
            "truncated": truncated,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Search timed out after 30s"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def spotlight_search(settings: Settings, query: str, max_results: int = 50) -> Dict[str, Any]:
    """Search files using macOS Spotlight (mdfind) — much faster for filenames."""
    try:
        proc = subprocess.run(
            ["mdfind", "-name", query],
            capture_output=True, text=True, timeout=10
        )
        results = [r for r in proc.stdout.splitlines() if r.strip()][:max_results]
        return {"ok": True, "count": len(results), "results": results}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Spotlight search timed out"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
