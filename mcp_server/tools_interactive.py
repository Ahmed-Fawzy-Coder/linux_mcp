from __future__ import annotations

import subprocess
from typing import Any, Dict, Optional

from .security import Settings


def ask_user(
    settings: Settings,
    question: str,
    sender: str = "AI",
    timeout_s: int = 60,
) -> Dict[str, Any]:
    """
    Allows the AI to ask the local user an interactive question during an autonomous task.

    Opens a native macOS dialog:
    - Sender label at the top, for example ChatGPT
    - The AI question or message in the body
    - A text input for the user's response
    - Send / Skip buttons
    - Automatically closes on timeout

    Parameters
    ----------
    question   : Question or message shown to the user
    sender     : Name shown in the dialog title, for example ChatGPT or Claude
    timeout_s  : How many seconds to wait, default 60

    Returns
    -------
    {
      "ok"       : bool,
      "response" : str | None,
      "skipped"  : bool,
      "timed_out": bool
    }
    """
    # Escape text for AppleScript string literals
    def _as_escape(text: str) -> str:
        return text.replace("\\", "\\\\").replace('"', '\\"')

    q_display = _as_escape(question[:800] + ("…" if len(question) > 800 else ""))
    s_display = _as_escape(sender[:50])

    # Two-step dialog:
    # 1. Show the question; continue or skip
    # 2. Cevap input'u al
    script = f"""
set q_text to "{q_display}"
set s_name to "{s_display}"

-- Step 1: show the question
set step1 to display dialog q_text ¬
    with title "🤖 " & s_name & " — Question" ¬
    buttons {{"Atla", "Cevapla →"}} ¬
    default button "Cevapla →" ¬
    giving up after {timeout_s}

if gave up of step1 then
    return "__TIMEOUT__"
end if
if button returned of step1 is "Atla" then
    return "__SKIP__"
end if

-- Step 2: get the answer
set step2 to display dialog "Write your answer:" ¬
    with title "🤖 " & s_name & " — Cevap" ¬
    default answer "" ¬
    buttons {{"Cancel", "Send ➤"}} ¬
    default button "Send ➤" ¬
    giving up after 300

if gave up of step2 then
    return "__TIMEOUT__"
end if
if button returned of step2 is "Cancel" then
    return "__SKIP__"
end if

return text returned of step2
"""

    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout_s + 320,
        )

        output = (proc.stdout or "").strip()

        if not output or output == "__TIMEOUT__":
            return {"ok": True, "response": None, "timed_out": True,  "skipped": False}
        if output == "__SKIP__":
            return {"ok": True, "response": None, "timed_out": False, "skipped": True}

        return {"ok": True, "response": output, "timed_out": False, "skipped": False}

    except subprocess.TimeoutExpired:
        return {"ok": True, "response": None, "timed_out": True, "skipped": False}
    except Exception as e:
        return {"ok": False, "error": str(e)}
