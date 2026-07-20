#!/usr/bin/env bash
set -euo pipefail

if [ ! -d "mcp_server" ] || [ ! -f "pyproject.toml" ]; then
  echo "Run this script from the root of a cloned bulutarkan/mac-mcp repository." >&2
  exit 1
fi

backup_dir=".linux-port-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup_dir/mcp_server"
for f in pyproject.toml mcp_server/tools_terminal.py mcp_server/tools_jobs.py mcp_server/tools_macos.py mcp_server/tools_search.py mcp_server/tools_browser.py mcp_server/tools_interactive.py mcp_server/main.py; do
  if [ -f "$f" ]; then
    mkdir -p "$backup_dir/$(dirname "$f")"
    cp "$f" "$backup_dir/$f"
  fi
done

echo "Backup written to: $backup_dir"

cat > mcp_server/tools_terminal.py <<'PY'
from __future__ import annotations

from pathlib import Path
import os
import subprocess
import time
from typing import Any, Dict, Optional

from fastapi import HTTPException, status

from .security import Settings, truncate


def _timeout(settings: Settings, timeout_s: Optional[int]) -> int:
    if timeout_s is None:
        return settings.default_command_timeout_s
    return min(max(1, int(timeout_s)), settings.max_command_timeout_s)


def _shell() -> str:
    return os.getenv("SHELL") or "/bin/bash"


def _base_env() -> Dict[str, str]:
    env = os.environ.copy()
    env.update({
        "HOME": str(Path.home()),
        "USER": os.getenv("USER", Path.home().name),
        "LOGNAME": os.getenv("LOGNAME", os.getenv("USER", Path.home().name)),
        "PATH": f"{os.environ.get('PATH', '')}:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/snap/bin",
        "LANG": os.getenv("LANG", "C.UTF-8"),
        "LC_ALL": os.getenv("LC_ALL", os.getenv("LANG", "C.UTF-8")),
    })
    return env


def run_command(settings: Settings, command: str, timeout_s: Optional[int] = None) -> Dict[str, Any]:
    """Run any shell command through the user's Linux shell."""
    timeout = _timeout(settings, timeout_s)
    argv = [_shell(), "-lc", command] if settings.allow_shell else command.split()
    start = time.perf_counter()
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=_base_env(),
            cwd=str(settings.workdir if settings.workdir.exists() else Path.home()),
        )
        duration_ms = int((time.perf_counter() - start) * 1000)
    except subprocess.TimeoutExpired as e:
        raise HTTPException(status.HTTP_408_REQUEST_TIMEOUT,
                            f"Command timed out after {timeout}s") from e

    stdout, out_truncated = truncate(proc.stdout or "", settings.max_output_chars)
    stderr, err_truncated = truncate(proc.stderr or "", settings.max_output_chars)
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": out_truncated,
        "stderr_truncated": err_truncated,
        "duration_ms": duration_ms,
        "command": command,
        "shell": argv[0] if argv else None,
        "cwd": str(settings.workdir if settings.workdir.exists() else Path.home()),
    }


def process_list(settings: Settings, filter: Optional[str] = None) -> Dict[str, Any]:
    """List running processes, optionally filtered by name."""
    cmd = "ps aux"
    result = run_command(settings, cmd)
    if filter and result["ok"]:
        lines = result["stdout"].splitlines()
        header = lines[0] if lines else ""
        matched = [l for l in lines[1:] if filter.lower() in l.lower()]
        result["stdout"] = "\n".join([header] + matched)
        result["matched_count"] = len(matched)
    return result


def kill_process(settings: Settings, pid: int, signal: str = "TERM") -> Dict[str, Any]:
    """Kill a process by PID. Signal: TERM, KILL, HUP, INT, or QUIT."""
    allowed_signals = {"TERM", "KILL", "HUP", "INT", "QUIT"}
    sig = signal.upper()
    if sig not in allowed_signals:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"Signal must be one of: {', '.join(sorted(allowed_signals))}")
    return run_command(settings, f"kill -{sig} {int(pid)}")


def get_system_info(settings: Settings) -> Dict[str, Any]:
    """Get Linux system info: OS, kernel, CPU, memory, disk, battery, network, uptime."""
    script = r'''
echo "=== OS ==="
if [ -r /etc/os-release ]; then . /etc/os-release; echo "${PRETTY_NAME:-Linux}"; else uname -s; fi
echo "=== KERNEL ==="
uname -a
echo "=== HOSTNAME ==="
hostname
echo "=== UPTIME ==="
uptime
echo "=== CPU ==="
if command -v lscpu >/dev/null 2>&1; then
  lscpu | sed -n 's/^Model name:[[:space:]]*//p; s/^CPU(s):[[:space:]]*/CPU(s): /p' | head -5
else
  grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2- || echo unknown
fi
echo "=== MEMORY ==="
if command -v free >/dev/null 2>&1; then free -h; else cat /proc/meminfo | head; fi
echo "=== DISK ==="
df -h / | tail -1
echo "=== BATTERY ==="
if command -v upower >/dev/null 2>&1; then
  upower -i $(upower -e | grep -m1 BAT) 2>/dev/null | egrep 'state|percentage|time to' || echo "no battery info"
elif ls /sys/class/power_supply/BAT* >/dev/null 2>&1; then
  for b in /sys/class/power_supply/BAT*; do echo "$(basename "$b"): $(cat "$b/status" 2>/dev/null) $(cat "$b/capacity" 2>/dev/null)%"; done
else
  echo "no battery info"
fi
echo "=== NETWORK ==="
if command -v ip >/dev/null 2>&1; then ip -brief addr show scope global; else hostname -I; fi
'''
    return run_command(settings, script)
PY

cat > mcp_server/tools_macos.py <<'PY'
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
PY

cat > mcp_server/tools_search.py <<'PY'
from __future__ import annotations

from pathlib import Path
import os
import shlex
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from .security import Settings, resolve_path, truncate


def search_files(settings: Settings, pattern: str, path: str = str(Path.home()),
                 include_extensions: Optional[List[str]] = None,
                 case_sensitive: bool = False) -> Dict[str, Any]:
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
        stdout, truncated = truncate(proc.stdout or "", 50_000)
        lines = stdout.splitlines()
        return {"ok": proc.returncode in (0, 1), "match_count": len(lines), "results": stdout, "truncated": truncated, "exit_code": proc.returncode}
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
PY

cat > mcp_server/tools_interactive.py <<'PY'
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
PY

cat > mcp_server/tools_browser.py <<'PY'
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status

from .security import Settings, truncate

CDP_HOST = os.getenv("BROWSER_CDP_HOST", "127.0.0.1")
CDP_PORT = int(os.getenv("BROWSER_CDP_PORT", "9222"))
CDP_BASE = f"http://{CDP_HOST}:{CDP_PORT}"

BROWSERS = {
    "chrome": ["google-chrome", "chrome", "chromium", "chromium-browser"],
    "google chrome": ["google-chrome", "chrome", "chromium", "chromium-browser"],
    "chromium": ["chromium", "chromium-browser", "google-chrome"],
    "brave": ["brave-browser", "brave", "google-chrome", "chromium"],
    "edge": ["microsoft-edge", "microsoft-edge-stable", "google-chrome", "chromium"],
}


def validate_url(settings: Settings, url: str) -> None:
    from urllib.parse import urlparse
    import ipaddress
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "URL must include scheme and host.")
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if scheme not in {"http", "https"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only http/https URLs are allowed.")
    if settings.browser_https_only and scheme != "https":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only HTTPS URLs are allowed.")
    allowlist = settings.browser_allowlist
    if "*" not in allowlist:
        host = hostname.rstrip(".")
        if not any(host == a.rstrip(".") or host.endswith("." + a.rstrip(".")) for a in allowlist):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Hostname not in BROWSER_ALLOWLIST.")
    try:
        ipaddress.ip_address(hostname)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Direct IP URLs are not allowed.")
    except ValueError:
        pass


def _norm_browser(browser: str) -> str:
    key = (browser or "chrome").strip().lower()
    if key == "safari":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Safari is macOS-only. On Linux use 'chrome', 'chromium', 'brave', or 'edge'.")
    if key in BROWSERS:
        return key
    raise HTTPException(status.HTTP_400_BAD_REQUEST, "browser must be 'chrome', 'chromium', 'brave', or 'edge' on Linux.")


def _request_text(path: str, method: str = "GET", timeout: int = 5) -> str:
    req = urllib.request.Request(CDP_BASE + path, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _json(path: str, method: str = "GET", timeout: int = 5) -> Any:
    return json.loads(_request_text(path, method=method, timeout=timeout) or "null")


def _find_browser_binary(browser_key: str) -> Optional[str]:
    for candidate in BROWSERS[browser_key]:
        path = shutil.which(candidate)
        if path:
            return path
    return None


def _ensure_cdp(browser: str) -> None:
    key = _norm_browser(browser)
    try:
        _json("/json/version", timeout=2)
        return
    except Exception:
        pass

    binary = _find_browser_binary(key)
    if not binary:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "No Chrome/Chromium-compatible browser found. Install chromium-browser or google-chrome.")

    user_data = Path.home() / ".cache" / "mac-mcp" / "chrome-cdp"
    user_data.mkdir(parents=True, exist_ok=True)
    cmd = [
        binary,
        f"--remote-debugging-address={CDP_HOST}",
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={user_data}",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True)
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            _json("/json/version", timeout=1)
            return
        except Exception:
            time.sleep(0.25)
    raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not start browser with Chrome DevTools Protocol on port 9222.")


def _pages(browser: str) -> List[Dict[str, Any]]:
    _ensure_cdp(browser)
    pages = _json("/json/list", timeout=5)
    return [p for p in pages if p.get("type") == "page"]


def _new_page(browser: str, url: str = "about:blank") -> Dict[str, Any]:
    _ensure_cdp(browser)
    encoded = urllib.parse.quote(url, safe="")
    last_error: Optional[Exception] = None
    for method in ("PUT", "GET"):
        try:
            page = _json(f"/json/new?{encoded}", method=method, timeout=5)
            if isinstance(page, dict):
                return page
        except Exception as e:
            last_error = e
    raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Could not create browser tab: {last_error}")


def _target(browser: str, tab_index: Optional[int] = None) -> Dict[str, Any]:
    pages = _pages(browser)
    if not pages:
        return _new_page(browser)
    if tab_index is not None:
        if tab_index < 1 or tab_index > len(pages):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"tab_index must be between 1 and {len(pages)}")
        return pages[tab_index - 1]
    return pages[0]


def _activate(page: Dict[str, Any]) -> None:
    tid = page.get("id")
    if tid:
        try:
            _request_text(f"/json/activate/{tid}", timeout=3)
        except Exception:
            pass


def _close(page: Dict[str, Any]) -> None:
    tid = page.get("id")
    if not tid:
        return
    _request_text(f"/json/close/{tid}", timeout=3)


def _cdp(page: Dict[str, Any], method: str, params: Optional[Dict[str, Any]] = None, timeout: int = 10) -> Dict[str, Any]:
    try:
        import websocket  # type: ignore
    except Exception as e:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Missing websocket-client dependency. Run: pip install websocket-client") from e

    ws_url = page.get("webSocketDebuggerUrl")
    if not ws_url:
        # Refresh target details; /json/new sometimes returns a partial object.
        for p in _pages("chrome"):
            if p.get("id") == page.get("id"):
                ws_url = p.get("webSocketDebuggerUrl")
                break
    if not ws_url:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Target has no webSocketDebuggerUrl.")

    ws = websocket.create_connection(ws_url, timeout=timeout)
    try:
        msg_id = int(time.time() * 1000) % 1_000_000_000
        ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            raw = ws.recv()
            msg = json.loads(raw)
            if msg.get("id") != msg_id:
                continue
            if "error" in msg:
                raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, json.dumps(msg["error"]))
            return msg.get("result", {})
    finally:
        ws.close()


def _result_to_text(remote: Dict[str, Any]) -> str:
    if "value" in remote:
        value = remote.get("value")
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False)
    return str(remote.get("description") or remote.get("unserializableValue") or "")


def browser_open_url(settings: Settings, browser: str, url: str, new_tab: bool = True, activate: bool = True) -> Dict[str, Any]:
    b = _norm_browser(browser)
    validate_url(settings, url)
    if new_tab:
        page = _new_page(b, url)
    else:
        page = _target(b)
        _cdp(page, "Page.navigate", {"url": url}, timeout=10)
    if activate:
        _activate(page)
    return {"ok": True, "browser": b, "url": url, "target_id": page.get("id")}


def browser_list_tabs(settings: Settings, browser: str) -> Dict[str, Any]:
    b = _norm_browser(browser)
    tabs = []
    for i, p in enumerate(_pages(b), 1):
        tabs.append({"window_index": 1, "tab_index": i, "active": i == 1, "title": p.get("title", ""), "url": p.get("url", ""), "id": p.get("id")})
    return {"ok": True, "browser": b, "tabs": tabs}


def browser_activate_tab(settings: Settings, browser: str, window_index: int = 1, tab_index: int = 1) -> Dict[str, Any]:
    b = _norm_browser(browser)
    page = _target(b, tab_index)
    _activate(page)
    return {"ok": True, "browser": b, "window_index": 1, "tab_index": tab_index}


def browser_close_tab(settings: Settings, browser: str, window_index: int = 1, tab_index: int = 1) -> Dict[str, Any]:
    b = _norm_browser(browser)
    page = _target(b, tab_index)
    _close(page)
    return {"ok": True, "browser": b, "window_index": 1, "tab_index": tab_index}


def browser_execute_js(settings: Settings, browser: str, js: str, window_index: int = 1, tab_index: Optional[int] = None) -> Dict[str, Any]:
    b = _norm_browser(browser)
    page = _target(b, tab_index)
    res = _cdp(page, "Runtime.evaluate", {"expression": js or "undefined", "awaitPromise": True, "returnByValue": True}, timeout=min(60, settings.max_wait_s))
    if res.get("exceptionDetails"):
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, json.dumps(res["exceptionDetails"], ensure_ascii=False)[:2000])
    raw = _result_to_text(res.get("result", {}))
    raw, truncated = truncate(raw, settings.max_js_result_chars)
    return {"ok": True, "browser": b, "result": raw, "truncated": truncated}


def browser_click_selector(settings: Settings, browser: str, css_selector: str, window_index: int = 1, tab_index: Optional[int] = None) -> Dict[str, Any]:
    sel = json.dumps(css_selector)
    js = f"(function(){{var el=document.querySelector({sel}); if(!el) return 'NOT_FOUND'; el.click(); return 'OK';}})()"
    return browser_execute_js(settings, browser, js, window_index=window_index, tab_index=tab_index)


def browser_type_selector(settings: Settings, browser: str, css_selector: str, text: str, clear: bool = True, window_index: int = 1, tab_index: Optional[int] = None) -> Dict[str, Any]:
    sel = json.dumps(css_selector)
    txt = json.dumps(text or "")
    clr = "true" if clear else "false"
    js = (
        "(function(){"
        f"var el=document.querySelector({sel}); if(!el) return 'NOT_FOUND';"
        "try{el.focus();}catch(e){}"
        f"if({clr}) el.value='';"
        f"el.value = {txt};"
        "el.dispatchEvent(new Event('input', {bubbles:true}));"
        "el.dispatchEvent(new Event('change', {bubbles:true}));"
        "return 'OK';"
        "})()"
    )
    return browser_execute_js(settings, browser, js, window_index=window_index, tab_index=tab_index)


def browser_wait_for_selector(settings: Settings, browser: str, css_selector: str, timeout_s: int = 20, poll_ms: int = 250, window_index: int = 1, tab_index: Optional[int] = None) -> Dict[str, Any]:
    timeout_s = max(1, min(int(timeout_s or 20), settings.max_wait_s))
    poll_ms = max(50, int(poll_ms or 250))
    sel = json.dumps(css_selector)
    start = time.time()
    while True:
        res = browser_execute_js(settings, browser, f"(function(){{return !!document.querySelector({sel});}})()", window_index=window_index, tab_index=tab_index)
        ok = str(res.get("result", "")).strip().lower() in {"true", "1", "ok"}
        if ok:
            return {"ok": True, "found": True, "elapsed_s": round(time.time() - start, 3)}
        if time.time() - start >= timeout_s:
            return {"ok": True, "found": False, "elapsed_s": round(time.time() - start, 3)}
        time.sleep(poll_ms / 1000.0)


def browser_get_html(settings: Settings, browser: str, max_chars: Optional[int] = None, window_index: int = 1, tab_index: Optional[int] = None) -> Dict[str, Any]:
    lim = settings.max_html_chars if max_chars is None else max(1, min(max_chars, 2_000_000))
    res = browser_execute_js(settings, browser, "document.documentElement.outerHTML", window_index=window_index, tab_index=tab_index)
    html = res.get("result", "")
    html, truncated = truncate(html, lim)
    return {"ok": True, "html": html, "truncated": truncated}


def browser_wait_for_download(settings: Settings, filename_contains: Optional[str] = None, timeout_s: int = 60) -> Dict[str, Any]:
    timeout_s = max(1, min(timeout_s, settings.max_wait_s))
    needle = (filename_contains or "").strip().lower()
    dl = settings.download_dir
    if not dl.exists() or not dl.is_dir():
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Download dir not found: {dl}")
    start = time.time()
    before = {p.name: p.stat().st_mtime for p in dl.iterdir() if p.is_file()}
    while True:
        candidates = []
        for p in dl.iterdir():
            if not p.is_file() or p.name.endswith((".download", ".crdownload")):
                continue
            if needle and needle not in p.name.lower():
                continue
            m = p.stat().st_mtime
            if p.name not in before or m > before.get(p.name, 0):
                candidates.append((m, p))
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            newest = candidates[0][1]
            return {"ok": True, "path": str(newest), "filename": newest.name}
        if time.time() - start >= timeout_s:
            return {"ok": True, "path": None, "filename": None}
        time.sleep(0.25)


def browser_screenshot(settings: Settings, browser: str, path: Optional[str] = None, window_index: int = 1, return_base64: bool = True) -> Dict[str, Any]:
    b = _norm_browser(browser)
    page = _target(b)
    _cdp(page, "Page.enable", {}, timeout=5)
    res = _cdp(page, "Page.captureScreenshot", {"format": "png", "fromSurface": True}, timeout=20)
    data = res.get("data", "")
    result: Dict[str, Any] = {"ok": True, "mime_type": "image/png"}
    if path:
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(data))
        result["path"] = str(target)
    else:
        result["path"] = None
    if return_base64:
        result["base64"] = data
    return result


def browser_scroll(settings: Settings, browser: str, dx: int = 0, dy: int = 300, selector: Optional[str] = None, window_index: int = 1, tab_index: Optional[int] = None) -> Dict[str, Any]:
    if selector:
        sel = json.dumps(selector)
        js = f"(function(){{var el=document.querySelector({sel}); if(!el) return 'NOT_FOUND'; el.scrollIntoView({{behavior:'instant',block:'center',inline:'nearest'}}); return 'OK';}})()"
    else:
        js = f"(function(){{window.scrollBy({int(dx)},{int(dy)});return 'OK';}})()"
    return browser_execute_js(settings, browser, js, window_index=window_index, tab_index=tab_index)


_MOD_BITS = {"alt": 1, "option": 1, "opt": 1, "ctrl": 2, "control": 2, "cmd": 4, "command": 4, "meta": 4, "shift": 8}
_KEY_MAP = {
    "return": "Enter", "enter": "Enter", "escape": "Escape", "esc": "Escape", "tab": "Tab", "space": " ",
    "delete": "Delete", "backspace": "Backspace", "left": "ArrowLeft", "right": "ArrowRight", "down": "ArrowDown", "up": "ArrowUp",
    "pageup": "PageUp", "pagedown": "PageDown", "home": "Home", "end": "End", "f5": "F5",
}


def browser_press_key(settings: Settings, browser: str, key: str, modifiers: Optional[List[str]] = None, window_index: int = 1) -> Dict[str, Any]:
    b = _norm_browser(browser)
    page = _target(b)
    mods = 0
    for m in modifiers or []:
        mods |= _MOD_BITS.get(m.lower(), 0)
    k = _KEY_MAP.get((key or "").lower(), key or "")
    if len(k) == 1 and mods == 0:
        _cdp(page, "Input.insertText", {"text": k}, timeout=5)
    else:
        params = {"type": "keyDown", "key": k, "modifiers": mods}
        _cdp(page, "Input.dispatchKeyEvent", params, timeout=5)
        params["type"] = "keyUp"
        _cdp(page, "Input.dispatchKeyEvent", params, timeout=5)
    return {"ok": True, "key": key, "modifiers": modifiers or []}


def browser_coordinate_click(settings: Settings, browser: str, x: int, y: int, double_click: bool = False, window_index: int = 1) -> Dict[str, Any]:
    b = _norm_browser(browser)
    page = _target(b)
    scroll_raw = browser_execute_js(settings, b, "JSON.stringify({x: window.scrollX, y: window.scrollY})").get("result", "{}")
    try:
        scroll = json.loads(scroll_raw)
    except Exception:
        scroll = {"x": 0, "y": 0}
    vx = int(x) - int(scroll.get("x", 0))
    vy = int(y) - int(scroll.get("y", 0))
    count = 2 if double_click else 1
    _cdp(page, "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": vx, "y": vy}, timeout=5)
    _cdp(page, "Input.dispatchMouseEvent", {"type": "mousePressed", "x": vx, "y": vy, "button": "left", "clickCount": count}, timeout=5)
    _cdp(page, "Input.dispatchMouseEvent", {"type": "mouseReleased", "x": vx, "y": vy, "button": "left", "clickCount": count}, timeout=5)
    return {"ok": True, "x": x, "y": y, "viewport_x": vx, "viewport_y": vy, "double_click": double_click}


_SNAPSHOT_JS = r"""
(function(maxDepth, maxChildren) {
    var scrollX = window.scrollX, scrollY = window.scrollY;
    var winH = window.innerHeight, winW = window.innerWidth;
    function isVisible(el) {
        var s = window.getComputedStyle(el);
        if (s.display === 'none' || s.visibility === 'hidden') return false;
        if (parseFloat(s.opacity) === 0) return false;
        var r = el.getBoundingClientRect();
        if (r.width === 0 && r.height === 0) return false;
        return true;
    }
    function buildNode(el, depth) {
        if (depth > maxDepth) return null;
        if (!isVisible(el)) return null;
        var tag = el.tagName.toLowerCase();
        var r = el.getBoundingClientRect();
        var node = { tag: tag, rect: { x: Math.round(r.left + scrollX), y: Math.round(r.top + scrollY), w: Math.round(r.width), h: Math.round(r.height), vx: Math.round(r.left), vy: Math.round(r.top) } };
        if (el.id) node.id = el.id;
        var cls = el.className;
        if (cls && typeof cls === 'string' && cls.trim()) node.cls = cls.trim().substring(0, 80);
        var aria = el.getAttribute('aria-label'); if (aria) node.aria = aria.substring(0, 120);
        var role = el.getAttribute('role'); if (role) node.role = role;
        var ph = el.getAttribute('placeholder'); if (ph) node.placeholder = ph.substring(0, 80);
        var title = el.getAttribute('title'); if (title) node.title = title.substring(0, 80);
        if (tag === 'a' && el.href) node.href = el.href.substring(0, 200);
        if (['input','textarea','select'].includes(tag)) { node.value = (el.value || '').substring(0, 150); if (tag === 'input') node.type = el.type; node.name = el.name || ''; }
        if (tag === 'button' || el.getAttribute('role') === 'button') node.isButton = true;
        var text = Array.from(el.childNodes).filter(function(n){ return n.nodeType === 3; }).map(function(n){ return n.textContent.trim(); }).join(' ').trim();
        if (text) node.text = text.substring(0, 200);
        var kids = Array.from(el.children).slice(0, maxChildren).map(function(c){ return buildNode(c, depth + 1); }).filter(Boolean);
        if (kids.length) node.children = kids;
        return node;
    }
    return JSON.stringify({ url: location.href, title: document.title, scroll: {x: scrollX, y: scrollY}, viewport: {w: winW, h: winH}, tree: buildNode(document.body, 0) });
})(MAX_DEPTH, MAX_CHILDREN)
"""


def browser_get_snapshot(settings: Settings, browser: str, window_index: int = 1, tab_index: Optional[int] = None, max_depth: int = 6, max_children: int = 25) -> Dict[str, Any]:
    js = _SNAPSHOT_JS.replace("MAX_DEPTH", str(max_depth)).replace("MAX_CHILDREN", str(max_children))
    raw = browser_execute_js(settings, browser, js, window_index=window_index, tab_index=tab_index)
    result_str = raw.get("result", "")
    if not result_str:
        return {"ok": False, "error": "Empty snapshot"}
    try:
        data = json.loads(result_str)
        data["ok"] = True
        return data
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"JSON parse error: {e}", "raw": result_str[:500]}
PY

python3 - <<'PY'
from pathlib import Path

# Patch tools_jobs.py: zsh/homebrew assumptions -> Linux shell/PATH.
p = Path("mcp_server/tools_jobs.py")
txt = p.read_text(encoding="utf-8")
txt = txt.replace('"PATH": f"{os.environ.get(\'PATH\', \'\')}:/usr/local/bin:/opt/homebrew/bin:/opt/homebrew/sbin",', '"PATH": f"{os.environ.get(\'PATH\', \'\')}:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/snap/bin",')
txt = txt.replace('argv = ["/bin/zsh", "-lc", command] if settings.allow_shell else command.split()', 'argv = [os.getenv("SHELL", "/bin/bash"), "-lc", command] if settings.allow_shell else command.split()')
txt = txt.replace('"HOMEBREW_NO_AUTO_UPDATE": "1",', '"HOMEBREW_NO_AUTO_UPDATE": "1",  # harmless on Linux; kept for compatibility')
p.write_text(txt, encoding="utf-8")

# Patch pyproject.toml dependency for CDP websocket.
p = Path("pyproject.toml")
txt = p.read_text(encoding="utf-8")
if 'websocket-client' not in txt:
    txt = txt.replace('  "mcp[cli]==1.27.2"\n]', '  "mcp[cli]==1.27.2",\n  "websocket-client==1.8.0"\n]')
# Keep package/command name unchanged for compatibility, but update description.
txt = txt.replace('description = "Local macOS control server for MCP clients and Custom GPT Actions"', 'description = "Local Linux desktop control server for MCP clients and Custom GPT Actions"')
p.write_text(txt, encoding="utf-8")

# Patch main.py user-facing tool descriptions/instructions. Imports remain compatible via tools_macos.py.
p = Path("mcp_server/main.py")
txt = p.read_text(encoding="utf-8")
txt = txt.replace('name="mac-mcp"', 'name="linux-mcp"')
txt = txt.replace("You are connected to the user's local Mac through Mac MCP.", "You are connected to the user's local Linux desktop through Linux MCP.")
txt = txt.replace("macOS kontrolu icin run_applescript veya ozel macOS toollarini kullan.", "Linux desktop control is available through notification, clipboard, app/url, screenshot, volume, brightness, browser, file, and shell tools.")
txt = txt.replace("Run any shell command in zsh on the local Mac. Full access.", "Run any shell command through the user's Linux shell. Full access.")
txt = txt.replace("Get Mac system info: CPU, memory, disk, battery, network, uptime.", "Get Linux system info: OS, kernel, CPU, memory, disk, battery, network, uptime.")
txt = txt.replace("# ── macOS tools", "# ── Linux desktop compatibility tools")
txt = txt.replace("Run AppleScript on macOS. Control apps, system settings, UI automation.", "AppleScript compatibility placeholder. On Linux, use shell/browser/desktop tools instead.")
txt = txt.replace("Send a macOS notification banner. sound: Pop, Glass, Basso, etc.", "Send a Linux desktop notification banner. sound is ignored.")
txt = txt.replace("Read the current Mac clipboard contents.", "Read the current Linux clipboard contents.")
txt = txt.replace("Write text to the Mac clipboard.", "Write text to the Linux clipboard.")
txt = txt.replace("Open a macOS application by name. e.g. 'Safari', 'Finder', 'Terminal'.", "Open a Linux application by executable or .desktop id.")
txt = txt.replace("Requires 'brew install brightness'.", "Requires brightnessctl or xrandr.")
txt = txt.replace("Add a reminder to macOS Reminders. due_date format: 'month/day/year HH:MM AM/PM'.", "Add a Linux desktop reminder when systemd-run/at is available. due_date format: 'month/day/year HH:MM AM/PM'.")
txt = txt.replace("Get list of currently running macOS applications (visible apps only).", "Get list of currently running Linux applications/windows when possible.")
txt = txt.replace("Search files by name using macOS Spotlight (mdfind) — very fast.", "Search files by name on Linux using fd, locate, or find.")
txt = txt.replace("Open a URL in Safari or Google Chrome. browser: 'Safari' or 'Google Chrome'.", "Open a URL in a Chrome/Chromium-compatible Linux browser via Chrome DevTools Protocol. browser: chrome|chromium|brave|edge.")
txt = txt.replace("List all open tabs in Safari or Google Chrome.", "List tabs in the Chrome/Chromium-compatible Linux CDP browser session.")
txt = txt.replace("Example: key='a', modifiers=['cmd'] sends Cmd+A.", "Example: key='a', modifiers=['ctrl'] sends Ctrl+A.")
txt = txt.replace('{"ok": True, "server": "mac-mcp", "workdir": str(settings.workdir)}', '{"ok": True, "server": "linux-mcp", "workdir": str(settings.workdir)}')
p.write_text(txt, encoding="utf-8")

# Patch CLI wording only; keep command binary mac-mcp for compatibility.
p = Path("mcp_server/cli.py")
if p.exists():
    txt = p.read_text(encoding="utf-8")
    txt = txt.replace("Install it with Homebrew or set NGROK_BIN", "Install ngrok or set NGROK_BIN")
    txt = txt.replace("Manage the Mac MCP local server.", "Manage the Linux MCP local server.")
    p.write_text(txt, encoding="utf-8")
PY

cat > LINUX_PORT_NOTES.md <<'MD'
# Linux port notes for bulutarkan/mac-mcp

This patch keeps the original `mac-mcp` command name for compatibility, but changes the runtime behavior to Linux.

## What was changed

- Terminal commands now use `$SHELL` or `/bin/bash` instead of `/bin/zsh`.
- System info now uses Linux commands: `/etc/os-release`, `uname`, `lscpu`, `free`, `df`, `upower`/`/sys/class/power_supply`, and `ip`.
- macOS desktop functions were replaced by Linux equivalents:
  - notifications: `notify-send`
  - clipboard: `wl-copy`/`wl-paste`, `xclip`, or `xsel`
  - open URL/app: `xdg-open`, `gio`, `gtk-launch`, or executable launch
  - volume: `pactl` or `amixer`
  - brightness: `brightnessctl` or `xrandr`
  - screenshots: `gnome-screenshot`, `grim`, `scrot`, or ImageMagick `import`
  - interactive prompt: `zenity` or `kdialog`
- Spotlight search was replaced by `fd`/`fdfind`, `locate`, or `find`.
- Browser automation was replaced by Chrome DevTools Protocol over `127.0.0.1:9222`.

## Recommended Ubuntu packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git \
  libnotify-bin xdg-utils wl-clipboard xclip zenity \
  wmctrl xdotool brightnessctl gnome-screenshot ripgrep fd-find chromium-browser
```

On some Ubuntu versions Chromium is a snap package. Google Chrome also works.

## Install after patch

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp -n mcp_server/.env.example mcp_server/.env
```

Edit `mcp_server/.env` and use a strong API key:

```env
MCP_API_KEY=replace-with-a-long-random-token
MCP_ALLOW_NO_AUTH=false
MCP_ALLOW_SHELL=true
RATE_LIMIT_PER_MINUTE=120
WORKDIR=/home/YOUR_USER
```

Start:

```bash
mac-mcp start --host 127.0.0.1 --port 8000
curl http://127.0.0.1:8000/health
```

## Browser automation note

This Linux port controls a Chrome/Chromium-compatible browser through Chrome DevTools Protocol. It will start a separate browser profile at:

```text
~/.cache/mac-mcp/chrome-cdp
```

This is safer and more reliable than trying to control your existing browser windows.

## Codex MCP config example

```bash
codex mcp add linux-mcp --url http://127.0.0.1:8000/mcp --bearer-token "$MCP_API_KEY"
```

If your Codex CLI version does not accept `--url`, add it manually to `~/.codex/config.toml` according to your Codex MCP format.
MD

echo
cat <<'MSG'
Linux port files written.

Next steps:
  python3 -m venv .venv
  source .venv/bin/activate
  pip install -e .
  cp -n mcp_server/.env.example mcp_server/.env
  nano mcp_server/.env
  mac-mcp start --host 127.0.0.1 --port 8000

Read LINUX_PORT_NOTES.md for Ubuntu packages and browser/CDP notes.
MSG
