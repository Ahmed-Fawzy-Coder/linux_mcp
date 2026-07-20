from __future__ import annotations

import shutil
import subprocess
from typing import Any, Dict

from .security import Settings


def ask_user(
    settings: Settings,
    question: str,
    sender: str = "AI",
    timeout_s: int = 60,
) -> Dict[str, Any]:
    """Ask the local Linux user a question using zenity/kdialog, if available."""
    q = (question or "")[:1200]
    s = (sender or "AI")[:80]
    timeout_s = max(1, int(timeout_s or 60))

    try:
        if shutil.which("zenity"):
            proc = subprocess.run(
                ["zenity", "--entry", f"--title=🤖 {s} — Question", f"--text={q}", f"--timeout={timeout_s}"],
                capture_output=True,
                text=True,
                timeout=timeout_s + 5,
            )
            if proc.returncode == 0:
                return {"ok": True, "response": (proc.stdout or "").rstrip("\n"), "timed_out": False, "skipped": False}
            # zenity uses 5 for timeout and 1 for cancel in common builds.
            return {"ok": True, "response": None, "timed_out": proc.returncode == 5, "skipped": proc.returncode != 5}

        if shutil.which("kdialog"):
            proc = subprocess.run(
                ["kdialog", "--title", f"🤖 {s} — Question", "--inputbox", q],
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
            if proc.returncode == 0:
                return {"ok": True, "response": (proc.stdout or "").rstrip("\n"), "timed_out": False, "skipped": False}
            return {"ok": True, "response": None, "timed_out": False, "skipped": True}

        return {"ok": False, "error": "No native dialog tool found. Install zenity or kdialog: sudo apt install zenity"}
    except subprocess.TimeoutExpired:
        return {"ok": True, "response": None, "timed_out": True, "skipped": False}
    except Exception as e:
        return {"ok": False, "error": str(e)}
