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


def _tail(text: str, tail_lines: int) -> str:
    count = max(1, min(int(tail_lines or 100), 500))
    return "\n".join(text.splitlines()[-count:])


def _truncate_tail(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    marker = "... [earlier output truncated]\n"
    available = max(0, limit - len(marker))
    tail = text[-available:] if available else ""
    first_newline = tail.find("\n")
    if first_newline >= 0:
        tail = tail[first_newline + 1:]
    return marker + tail, True


def run_command(settings: Settings, command: str, cwd: Optional[str] = None,
                timeout_s: Optional[int] = None,
                tail_lines: int = 100,
                max_output_chars: Optional[int] = None) -> Dict[str, Any]:
    """Run any shell command through the user's Linux shell."""
    timeout = _timeout(settings, timeout_s)
    argv = [_shell(), "-lc", command] if settings.allow_shell else command.split()
    workdir = Path(cwd).expanduser().resolve() if cwd else (
        settings.workdir if settings.workdir.exists() else Path.home()
    )
    if not workdir.exists() or not workdir.is_dir():
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"cwd does not exist or is not a directory: {workdir}")
    start = time.perf_counter()
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=_base_env(),
            cwd=str(workdir),
        )
        duration_ms = int((time.perf_counter() - start) * 1000)
    except subprocess.TimeoutExpired as e:
        raise HTTPException(status.HTTP_408_REQUEST_TIMEOUT,
                            f"Command timed out after {timeout}s") from e

    limit = min(settings.max_output_chars, max(512, int(max_output_chars or settings.max_output_chars)))
    stdout_text = _tail(proc.stdout or "", tail_lines)
    stderr_text = _tail(proc.stderr or "", tail_lines)
    first_name, first_text, second_name, second_text = (
        ("stderr", stderr_text, "stdout", stdout_text)
        if proc.returncode != 0 else
        ("stdout", stdout_text, "stderr", stderr_text)
    )
    first, first_truncated = _truncate_tail(first_text, limit)
    remaining = max(0, limit - len(first))
    if remaining:
        second, second_truncated = _truncate_tail(second_text, remaining)
    else:
        second, second_truncated = "", bool(second_text)
    streams = {first_name: first, second_name: second}
    truncation = {first_name: first_truncated, second_name: second_truncated}
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": streams["stdout"],
        "stderr": streams["stderr"],
        "stdout_truncated": truncation["stdout"],
        "stderr_truncated": truncation["stderr"],
        "duration_ms": duration_ms,
        "_telemetry": {
            "source_chars": len(proc.stdout or "") + len(proc.stderr or ""),
            "returned_content_chars": len(streams["stdout"]) + len(streams["stderr"]),
        },
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
