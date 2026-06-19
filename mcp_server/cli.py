from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

APP_MODULE = "mcp_server.main:app"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = "8000"
STATE_DIR = Path.home() / ".mac-mcp"
PID_FILE = STATE_DIR / "mac-mcp.pid"
LOG_FILE = STATE_DIR / "mac-mcp.log"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text().strip())
    except Exception:
        return None


def _remove_stale_pid() -> None:
    pid = _read_pid()
    if pid is None or not _pid_alive(pid):
        PID_FILE.unlink(missing_ok=True)


def start(args: argparse.Namespace) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _remove_stale_pid()
    pid = _read_pid()
    if pid and _pid_alive(pid):
        print(f"mac-mcp is already running (pid {pid}).")
        return 0

    env = os.environ.copy()
    env.setdefault("MAC_MCP_HOST", args.host)
    env.setdefault("MAC_MCP_PORT", str(args.port))
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        APP_MODULE,
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    if args.reload:
        cmd.append("--reload")

    log = LOG_FILE.open("a", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    PID_FILE.write_text(str(proc.pid))
    time.sleep(0.5)
    if proc.poll() is not None:
        print(f"mac-mcp failed to start. See log: {LOG_FILE}")
        PID_FILE.unlink(missing_ok=True)
        return proc.returncode or 1
    print(f"mac-mcp started on http://{args.host}:{args.port} (pid {proc.pid}).")
    print(f"Log: {LOG_FILE}")
    return 0


def stop(args: argparse.Namespace) -> int:
    pid = _read_pid()
    if not pid or not _pid_alive(pid):
        PID_FILE.unlink(missing_ok=True)
        print("mac-mcp is not running.")
        return 0
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            PID_FILE.unlink(missing_ok=True)
            print("mac-mcp stopped.")
            return 0
        time.sleep(0.2)
    if args.force:
        os.kill(pid, signal.SIGKILL)
        PID_FILE.unlink(missing_ok=True)
        print("mac-mcp force-stopped.")
        return 0
    print(f"mac-mcp did not stop within {args.timeout}s. Run: mac-mcp stop --force")
    return 1


def status(args: argparse.Namespace) -> int:
    pid = _read_pid()
    if pid and _pid_alive(pid):
        print(f"mac-mcp is running (pid {pid}).")
        print(f"Log: {LOG_FILE}")
        return 0
    PID_FILE.unlink(missing_ok=True)
    print("mac-mcp is not running.")
    return 1


def restart(args: argparse.Namespace) -> int:
    stop_args = argparse.Namespace(timeout=args.timeout, force=True)
    stop(stop_args)
    return start(args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage the Mac MCP local server.")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_start_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--host", default=os.getenv("MAC_MCP_HOST", DEFAULT_HOST))
        p.add_argument("--port", default=int(os.getenv("MAC_MCP_PORT", DEFAULT_PORT)), type=int)
        p.add_argument("--reload", action="store_true", help="Enable uvicorn reload mode for development.")

    p_start = sub.add_parser("start", help="Start the local server.")
    add_start_flags(p_start)
    p_start.set_defaults(func=start)

    p_stop = sub.add_parser("stop", help="Stop the local server.")
    p_stop.add_argument("--timeout", type=float, default=5)
    p_stop.add_argument("--force", action="store_true")
    p_stop.set_defaults(func=stop)

    p_restart = sub.add_parser("restart", help="Restart the local server.")
    add_start_flags(p_restart)
    p_restart.add_argument("--timeout", type=float, default=5)
    p_restart.set_defaults(func=restart)

    p_status = sub.add_parser("status", help="Show server status.")
    p_status.set_defaults(func=status)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
