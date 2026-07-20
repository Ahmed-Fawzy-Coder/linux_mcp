from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
import os
import re
import shlex
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from .security import Settings, truncate


def _which_any(names: List[str]) -> Optional[str]:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def _run(args: List[str], *, input_text: Optional[str] = None, timeout: int = 10) -> Dict[str, Any]:
    try:
        proc = subprocess.run(args, input=input_text, capture_output=True, text=True, timeout=timeout)
        stdout, out_truncated = truncate(proc.stdout or "", 50_000)
        stderr, err_truncated = truncate(proc.stderr or "", 50_000)
        return {
            "ok": proc.returncode == 0,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": out_truncated,
            "stderr_truncated": err_truncated,
            "exit_code": proc.returncode,
            "command": args,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Command timed out after {timeout}s", "command": args}
    except Exception as e:
        return {"ok": False, "error": str(e), "command": args}


def run_applescript(settings: Settings, script: str, timeout_s: int = 30) -> Dict[str, Any]:
    """AppleScript is macOS-only. On Linux this tool intentionally returns an unsupported error."""
    return {
        "ok": False,
        "error": "AppleScript/osascript is not available on Linux. Use run_command for shell automation or the Linux desktop tools instead.",
        "platform": "linux",
    }


def send_notification(settings: Settings, title: str, message: str, sound: str = "Pop") -> Dict[str, Any]:
    """Send a Linux desktop notification using notify-send when available."""
    notify = _which_any(["notify-send"])
    if not notify:
        return {"ok": False, "error": "notify-send not found. Install it with: sudo apt install libnotify-bin"}
    return _run([notify, str(title or "Notification"), str(message or "")], timeout=5)


def clipboard_get(settings: Settings) -> Dict[str, Any]:
    """Read the clipboard on Wayland or X11."""
    if _which_any(["wl-paste"]):
        res = _run(["wl-paste", "--no-newline"], timeout=5)
    elif _which_any(["xclip"]):
        res = _run(["xclip", "-selection", "clipboard", "-o"], timeout=5)
    elif _which_any(["xsel"]):
        res = _run(["xsel", "--clipboard", "--output"], timeout=5)
    else:
        return {"ok": False, "error": "No clipboard tool found. Install wl-clipboard or xclip: sudo apt install wl-clipboard xclip"}
    if not res.get("ok"):
        return res
    content = res.get("stdout", "")
    return {"ok": True, "content": content, "length": len(content)}


def clipboard_set(settings: Settings, content: str) -> Dict[str, Any]:
    """Write text to the clipboard on Wayland or X11."""
    content = content or ""
    if _which_any(["wl-copy"]):
        res = _run(["wl-copy"], input_text=content, timeout=5)
    elif _which_any(["xclip"]):
        res = _run(["xclip", "-selection", "clipboard"], input_text=content, timeout=5)
    elif _which_any(["xsel"]):
        res = _run(["xsel", "--clipboard", "--input"], input_text=content, timeout=5)
    else:
        return {"ok": False, "error": "No clipboard tool found. Install wl-clipboard or xclip: sudo apt install wl-clipboard xclip"}
    return {"ok": bool(res.get("ok")), "chars_copied": len(content), "details": res}


def open_app(settings: Settings, app_name: str) -> Dict[str, Any]:
    """Open a Linux application by desktop id or executable name."""
    if not app_name:
        return {"ok": False, "error": "app_name is required"}
    gtk_launch = _which_any(["gtk-launch"])
    if gtk_launch:
        # Works with desktop ids such as org.gnome.Nautilus or code.
        res = _run([gtk_launch, app_name], timeout=5)
        if res.get("ok"):
            return {"ok": True, "app": app_name, "method": "gtk-launch"}
    parts = shlex.split(app_name)
    executable = shutil.which(parts[0]) if parts else None
    if not executable:
        return {"ok": False, "error": f"Application not found: {app_name}. Try the exact executable or .desktop id."}
    try:
        subprocess.Popen([executable] + parts[1:], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True)
        return {"ok": True, "app": app_name, "method": "exec"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def open_url(settings: Settings, url: str) -> Dict[str, Any]:
    """Open a URL or local file in the default Linux handler."""
    opener = _which_any(["xdg-open", "gio"])
    if not opener:
        return {"ok": False, "error": "xdg-open/gio not found. Install xdg-utils: sudo apt install xdg-utils"}
    args = [opener, "open", url] if Path(opener).name == "gio" else [opener, url]
    try:
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True)
        return {"ok": True, "url": url}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def set_volume(settings: Settings, level: int) -> Dict[str, Any]:
    """Set Linux system volume to 0-100 using PulseAudio/PipeWire or ALSA."""
    level = max(0, min(100, int(level)))
    if _which_any(["pactl"]):
        return _run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{level}%"], timeout=5)
    if _which_any(["amixer"]):
        return _run(["amixer", "set", "Master", f"{level}%"], timeout=5)
    return {"ok": False, "error": "No volume tool found. Install pulseaudio-utils or alsa-utils."}


def get_volume(settings: Settings) -> Dict[str, Any]:
    """Get current Linux system volume."""
    if _which_any(["pactl"]):
        res = _run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"], timeout=5)
        text = res.get("stdout", "")
        m = re.search(r"(\d+)%", text)
        return {"ok": res.get("ok", False), "volume": int(m.group(1)) if m else None, "raw": text}
    if _which_any(["amixer"]):
        res = _run(["amixer", "get", "Master"], timeout=5)
        text = res.get("stdout", "")
        m = re.search(r"\[(\d+)%\]", text)
        return {"ok": res.get("ok", False), "volume": int(m.group(1)) if m else None, "raw": text}
    return {"ok": False, "error": "No volume tool found. Install pulseaudio-utils or alsa-utils."}


def set_brightness(settings: Settings, level: int) -> Dict[str, Any]:
    """Set brightness to 0-100. Prefers brightnessctl; falls back to xrandr display gamma brightness."""
    level = max(1, min(100, int(level)))
    if _which_any(["brightnessctl"]):
        return _run(["brightnessctl", "set", f"{level}%"], timeout=8)
    if _which_any(["xrandr"]):
        try:
            out = subprocess.check_output("xrandr --current | awk '/ connected/{print $1; exit}'", shell=True, text=True, timeout=5).strip()
            if out:
                return _run(["xrandr", "--output", out, "--brightness", str(level / 100.0)], timeout=5)
        except Exception as e:
            return {"ok": False, "error": str(e)}
    return {"ok": False, "error": "No brightness tool found. Install brightnessctl: sudo apt install brightnessctl"}


def screenshot(settings: Settings, path: str = str(Path.home() / "Pictures" / "screenshot.png"), window: bool = False) -> Dict[str, Any]:
    """Take a Linux screenshot using gnome-screenshot, grim, scrot, or ImageMagick import."""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    if _which_any(["gnome-screenshot"]):
        args = ["gnome-screenshot", "-f", str(target)]
        if window:
            args.insert(1, "-w")
        res = _run(args, timeout=30)
    elif _which_any(["grim"]):
        res = _run(["grim", str(target)], timeout=30)
    elif _which_any(["scrot"]):
        args = ["scrot", str(target)] if not window else ["scrot", "-u", str(target)]
        res = _run(args, timeout=30)
    elif _which_any(["import"]):
        res = _run(["import", "-window", "root", str(target)], timeout=30)
    else:
        return {"ok": False, "error": "No screenshot tool found. Install gnome-screenshot, grim, or scrot."}
    res["path"] = str(target)
    return res


def _parse_due_date(due_date: str) -> Optional[datetime]:
    for fmt in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M %p"):
        try:
            return datetime.strptime(due_date.strip(), fmt)
        except ValueError:
            continue
    return None


def set_reminder(settings: Settings, title: str, notes: str = "", due_date: Optional[str] = None) -> Dict[str, Any]:
    """Schedule a Linux desktop reminder using systemd-run --user or at, when available."""
    title = title or "Reminder"
    notes = notes or ""
    reminders_file = Path.home() / ".mac-mcp" / "reminders.jsonl"
    reminders_file.parent.mkdir(parents=True, exist_ok=True)
    record = {"title": title, "notes": notes, "due_date": due_date}
    with reminders_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    if not due_date:
        return {"ok": True, "stored": str(reminders_file), "scheduled": False, "message": "Stored without due_date; no notification scheduled."}
    parsed = _parse_due_date(due_date)
    if not parsed:
        return {"ok": False, "stored": str(reminders_file), "error": "Could not parse due_date. Use MM/DD/YYYY HH:MM AM/PM or YYYY-MM-DD HH:MM"}

    notify = _which_any(["notify-send"])
    if not notify:
        return {"ok": True, "stored": str(reminders_file), "scheduled": False, "warning": "notify-send not found; reminder was stored only."}

    when = parsed.strftime("%Y-%m-%d %H:%M:%S")
    if _which_any(["systemd-run"]):
        args = ["systemd-run", "--user", f"--on-calendar={when}", notify, title, notes]
        res = _run(args, timeout=10)
        return {"ok": bool(res.get("ok")), "stored": str(reminders_file), "scheduled": bool(res.get("ok")), "details": res}
    if _which_any(["at"]):
        cmd = f'{shlex.quote(notify)} {shlex.quote(title)} {shlex.quote(notes)}\n'
        res = _run(["at", parsed.strftime("%H:%M %Y-%m-%d")], input_text=cmd, timeout=10)
        return {"ok": bool(res.get("ok")), "stored": str(reminders_file), "scheduled": bool(res.get("ok")), "details": res}
    return {"ok": True, "stored": str(reminders_file), "scheduled": False, "warning": "Install systemd-run or at to schedule notifications."}


def get_running_apps(settings: Settings) -> Dict[str, Any]:
    """Get visible/running Linux desktop apps where possible."""
    if _which_any(["wmctrl"]):
        res = _run(["wmctrl", "-lx"], timeout=5)
        apps = []
        for line in res.get("stdout", "").splitlines():
            parts = line.split(None, 4)
            if len(parts) >= 3:
                apps.append(parts[2])
        return {"ok": res.get("ok", False), "apps": sorted(set(apps)), "raw": res.get("stdout", "")}
    res = _run(["ps", "-eo", "comm="], timeout=5)
    apps = sorted(set(x.strip() for x in res.get("stdout", "").splitlines() if x.strip()))[:500]
    return {"ok": res.get("ok", False), "apps": apps, "note": "Install wmctrl for visible window app names."}
