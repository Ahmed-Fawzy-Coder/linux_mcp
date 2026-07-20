from __future__ import annotations

from pathlib import Path
import os
import shlex
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from .security import Settings, resolve_path, truncate

DEFAULT_SEARCH_RESULTS = 50
MAX_SEARCH_RESULTS = 200
MAX_SEARCH_LINE_CHARS = 500


def search_files(settings: Settings, pattern: str, path: str = str(Path.home()),
                 include_extensions: Optional[List[str]] = None,
                 case_sensitive: bool = False,
                 max_results: int = DEFAULT_SEARCH_RESULTS) -> Dict[str, Any]:
    """Search file contents recursively using grep on Linux."""
    if not pattern:
        return {"ok": False, "error": "pattern is required"}
    root = resolve_path(path)
    flags = ["-RIn", "--binary-files=without-match", "--exclude-dir=.git", "--exclude-dir=node_modules", "--exclude-dir=.venv", "--exclude-dir=dist", "--exclude-dir=build"]
    if not case_sensitive:
        flags.append("-i")
    cmd = ["grep"] + flags
    if include_extensions:
        for ext in include_extensions:
            cmd.extend(["--include", f"*.{ext.lstrip('.')}"])
    cmd += [pattern, str(root)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        limit = max(1, min(int(max_results or DEFAULT_SEARCH_RESULTS), MAX_SEARCH_RESULTS))
        all_lines = (proc.stdout or "").splitlines()
        selected = [truncate(line, MAX_SEARCH_LINE_CHARS)[0] for line in all_lines[:limit]]
        bounded, char_truncated = truncate("\n".join(selected), settings.max_output_chars)
        results = bounded.splitlines() if bounded else []
        return {
            "ok": proc.returncode in (0, 1),
            "match_count": len(results),
            "results": results,
            "has_more": len(all_lines) > len(results),
            "truncated": char_truncated or len(all_lines) > limit,
            "exit_code": proc.returncode,
            "_telemetry": {
                "source_chars": len(proc.stdout or ""),
                "returned_content_chars": sum(len(line) for line in results),
            },
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Search timed out after 30s"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def spotlight_search(settings: Settings, query: str, max_results: int = 50) -> Dict[str, Any]:
    """Linux replacement for macOS Spotlight. Uses fd, locate, or find to search filenames."""
    query = (query or "").strip()
    max_results = max(1, min(int(max_results or 50), 500))
    if not query:
        return {"ok": False, "error": "query is required"}

    home = Path.home()
    try:
        if shutil.which("fd"):
            cmd = ["fd", "--hidden", "--color", "never", "-i", query, str(home)]
        elif shutil.which("fdfind"):
            cmd = ["fdfind", "--hidden", "--color", "never", "-i", query, str(home)]
        elif shutil.which("locate"):
            cmd = ["locate", "-i", query]
        else:
            # find fallback: shell is used only to safely embed the escaped glob.
            glob = f"*{query}*"
            cmd = ["bash", "-lc", f"find {shlex.quote(str(home))} -iname {shlex.quote(glob)} -print 2>/dev/null"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        results = [r for r in (proc.stdout or "").splitlines() if r.strip()][:max_results]
        return {"ok": True, "count": len(results), "results": results, "backend": Path(cmd[0]).name}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Linux filename search timed out"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
