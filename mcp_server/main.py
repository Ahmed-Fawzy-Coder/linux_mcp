from __future__ import annotations

from pathlib import Path
import json
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, Mount

from mcp.server.transport_security import TransportSecuritySettings
from .security import BASE_DIR, RateLimiter, Settings, authenticate, client_ip, load_settings, rate_limit, setup_audit_logger
from .tools_terminal import run_command, process_list, kill_process, get_system_info
from .tools_jobs import (
    start_background_job, get_job_status, get_job_output,
    stop_job, list_jobs, wait_jobs, run_commands_parallel,
)
from .tools_files import (
    write_file, write_files_batch, read_file, read_multiple_files,
    edit_file, move_file, copy_file, delete_path,
    list_directory, directory_tree, create_directory, get_file_info, find_files,
)
from .tools_macos import (
    run_applescript, send_notification, clipboard_get, clipboard_set,
    open_app, open_url, set_volume, get_volume, set_brightness,
    screenshot, set_reminder, get_running_apps,
)
from .tools_search import search_files, spotlight_search
from .tools_http import http_request
from .tools_browser import (
    browser_open_url, browser_list_tabs, browser_activate_tab, browser_close_tab,
    browser_execute_js, browser_click_selector, browser_type_selector,
    browser_wait_for_selector, browser_get_html, browser_wait_for_download,
    browser_screenshot, browser_scroll, browser_press_key,
    browser_coordinate_click, browser_get_snapshot,
)
from .tools_interactive import ask_user
from .tools_workspace import workspace


WORKSPACE_DESCRIPTION = (
    "One compact gateway returning JSON text once. Pass action plus arguments. "
    "Path contract: search_files uses path as the absolute project-root directory; "
    "read_file uses path as the absolute full file path, including the filename "
    "(never split it into path plus a file field), and offset is zero-based; "
    "run_command uses cwd as the absolute project-root directory. "
    "Examples: search_files {pattern:'TODO',path:'/project'}; "
    "read_file {path:'/project/package.json',offset:0,length:160}; "
    "run_command {command:'npm test',cwd:'/project'}. "
    "Actions: search_files(pattern,path,include_extensions,max_results); "
    "read_file(path,offset,length); read_multiple_files(paths,offset,length); "
    "edit_file(path,old_string,new_string,expected_replacements); "
    "write_file(path,content); write_files_batch(files,atomic); "
    "run_command(command,cwd,timeout_s,tail_lines,max_output_chars); "
    "run_commands_parallel(commands,cwd,timeout_s,return_output); "
    "start_background_job(command,cwd,env,timeout_s,no_output_timeout_s); "
    "get_job_status(job_id); get_job_output(job_id,tail_lines,since_offset,stream); "
    "wait_jobs(job_ids,timeout_s,return_output); stop_job(job_id,signal); "
    "get_context_result(context_id,offset,length,if_none_match). "
    "Optional arguments._context accepts mode (off|auto|store|full), intent, and if_none_match. "
    "auto stores the complete bounded action snapshot before deterministic reduction; "
    "snapshot_complete and source_complete are reported separately. "
    "Bounded defaults: read 160 lines, search 50 results, command/job output 100 lines and 12000 chars."
)


def _log(audit_logger, tool: str, fn):
    start = time.perf_counter()
    outcome = "ok"
    result = None
    try:
        result = fn()
        return result
    except HTTPException as exc:
        outcome = f"error:{exc.status_code}:{exc.detail}"
        raise
    except Exception as exc:
        outcome = f"error:500:{exc}"
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Server error: {exc}") from exc
    finally:
        ms = int((time.perf_counter() - start) * 1000)
        event = {"tool": tool, "outcome": outcome, "duration_ms": ms}
        if result is not None and hasattr(result, "payload_chars"):
            event.update({
                "payload_chars": result.payload_chars,
                "internal_discarded_chars": result.internal_discarded_chars,
                "estimated_savable_chars": result.estimated_savable_chars,
                "measured_segments": result.measured_segments,
                "truncated": result.truncated,
            })
            for field in (
                "context_stored_chars", "context_original_chars", "context_reduced_chars",
                "context_returned_chars", "context_saved_chars", "context_retrieval_chars",
                "context_stored", "context_reduced", "context_retrieval",
                "context_not_modified", "context_source_incomplete",
            ):
                value = getattr(result, field, 0)
                event[field] = max(0, int(value))
        audit_logger.info(json.dumps(event))


def create_app():
    settings = load_settings()
    limiter = RateLimiter(settings.rate_limit_per_minute)
    audit_logger = setup_audit_logger()

    mcp = FastMCP(
        name="linux-mcp",
        instructions=(
            "Bounded local Linux workspace operations with compact output. Workspace actions may opt in "
            "to reversible Ultimate Context via arguments._context; retrieve stored snapshots with "
            "get_context_result. Stored snapshots never imply recovery of source content already truncated "
            "by an underlying action."
        ),
        streamable_http_path="/mcp",
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    class SecurityMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            if request.method == "HEAD" and request.url.path == "/mcp":
                return Response(status_code=200, headers={
                    "content-type": "text/event-stream; charset=utf-8",
                    "mcp-session-id": uuid.uuid4().hex,
                })
            if request.url.path.startswith("/mcp"):
                try:
                    token = authenticate(settings, request.headers.get("authorization"))
                    ip = client_ip(request)
                    rate_limit(limiter, token, ip)
                except HTTPException as exc:
                    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            return await call_next(request)

    # ── Compact workspace gateway ──────────────────────────────────────────
    @mcp.tool(
        name="workspace",
        structured_output=False,
        description=WORKSPACE_DESCRIPTION,
    )
    def _workspace(action: str, arguments: Optional[Dict[str, Any]] = None) -> str:
        return _log(audit_logger, f"workspace:{action}",
                    lambda: workspace(settings, action=action, arguments=arguments))

    # ── Terminal tools ──────────────────────────────────────────────────────
    @mcp.tool(name="run_command",
              description="Run one shell command and return at most the requested tail within a strict output budget.")
    def _run_command(command: str, cwd: Optional[str] = None,
                     timeout_s: Optional[int] = None,
                     tail_lines: int = 100,
                     max_output_chars: Optional[int] = None) -> Dict[str, Any]:
        return _log(audit_logger, "run_command",
                    lambda: run_command(settings, command=command, cwd=cwd, timeout_s=timeout_s,
                                        tail_lines=tail_lines, max_output_chars=max_output_chars))

    @mcp.tool(name="process_list",
              description="List running processes. Optional filter by name substring.")
    def _process_list(filter: Optional[str] = None) -> Dict[str, Any]:
        return _log(audit_logger, "process_list",
                    lambda: process_list(settings, filter=filter))

    @mcp.tool(name="kill_process",
              description="Kill a process by PID. signal: TERM (graceful) or KILL (force).")
    def _kill_process(pid: int, signal: str = "TERM") -> Dict[str, Any]:
        return _log(audit_logger, "kill_process",
                    lambda: kill_process(settings, pid=pid, signal=signal))

    @mcp.tool(name="get_system_info",
              description="Get Linux system info: OS, kernel, CPU, memory, disk, battery, network, uptime.")
    def _get_system_info() -> Dict[str, Any]:
        return _log(audit_logger, "get_system_info", lambda: get_system_info(settings))

    @mcp.tool(name="start_background_job",
              description="Start a long-running shell command and return immediately with job_id. Use for npm install, builds, downloads, dev servers, docker, tests.")
    def _start_background_job(command: str, cwd: Optional[str] = None,
                              env: Optional[Dict[str, str]] = None,
                              timeout_s: Optional[int] = None,
                              no_output_timeout_s: Optional[int] = None) -> Dict[str, Any]:
        return _log(audit_logger, "start_background_job",
                    lambda: start_background_job(settings, command=command, cwd=cwd, env=env,
                                                 timeout_s=timeout_s, no_output_timeout_s=no_output_timeout_s))

    @mcp.tool(name="get_job_status",
              description="Get status for a background job by job_id.")
    def _get_job_status(job_id: str) -> Dict[str, Any]:
        return _log(audit_logger, "get_job_status",
                    lambda: get_job_status(settings, job_id=job_id))

    @mcp.tool(name="get_job_output",
              description="Read bounded stdout/stderr for a background job. Defaults to the last 100 lines.")
    def _get_job_output(job_id: str, tail_lines: Optional[int] = 100,
                        since_offset: Optional[int] = None,
                        stream: str = "both") -> Dict[str, Any]:
        return _log(audit_logger, "get_job_output",
                    lambda: get_job_output(settings, job_id=job_id, tail_lines=tail_lines,
                                           since_offset=since_offset, stream=stream))

    @mcp.tool(name="stop_job",
              description="Stop a background job by job_id. signal: TERM, KILL, INT, HUP.")
    def _stop_job(job_id: str, signal: str = "TERM") -> Dict[str, Any]:
        return _log(audit_logger, "stop_job",
                    lambda: stop_job(settings, job_id=job_id, signal_name=signal))

    @mcp.tool(name="list_jobs",
              description="List background jobs. status_filter can be running, stalled, completed, failed, timeout, killed.")
    def _list_jobs(status_filter: Optional[str] = None) -> Dict[str, Any]:
        return _log(audit_logger, "list_jobs",
                    lambda: list_jobs(settings, status_filter=status_filter))

    @mcp.tool(name="wait_jobs",
              description="Wait for background jobs to finish, optionally returning output.")
    def _wait_jobs(job_ids: List[str], timeout_s: Optional[int] = None,
                   return_output: bool = False) -> Dict[str, Any]:
        return _log(audit_logger, "wait_jobs",
                    lambda: wait_jobs(settings, job_ids=job_ids, timeout_s=timeout_s,
                                      return_output=return_output))

    @mcp.tool(name="run_commands_parallel",
              description="Run independent commands in parallel. Output is omitted by default; request it only when needed.")
    def _run_commands_parallel(commands: List[str], cwd: Optional[str] = None,
                               timeout_s: Optional[int] = None,
                               return_output: bool = False) -> Dict[str, Any]:
        return _log(audit_logger, "run_commands_parallel",
                    lambda: run_commands_parallel(settings, commands=commands, cwd=cwd,
                                                  timeout_s=timeout_s, return_output=return_output))

    # ── File tools ──────────────────────────────────────────────────────────
    @mcp.tool(name="write_file",
              description="Write content to a file. Creates parent directories if needed.")
    def _write_file(path: str, content: str) -> Dict[str, Any]:
        return _log(audit_logger, "write_file",
                    lambda: write_file(settings, path=path, content=content))

    @mcp.tool(name="write_files_batch",
              description="Write multiple files in one call. Pass a list of objects, each with 'path' and 'content' string fields.")
    def _write_files_batch(files: List[Dict[str, str]], atomic: bool = True) -> Dict[str, Any]:
        return _log(audit_logger, "write_files_batch",
                    lambda: write_files_batch(settings, files=files, atomic=atomic))

    @mcp.tool(name="read_file",
              description="Read a bounded file segment. Defaults to 160 lines; maximum 500 lines.")
    def _read_file(path: str, offset: int = 0, length: Optional[int] = 160) -> Dict[str, Any]:
        return _log(audit_logger, "read_file",
                    lambda: read_file(settings, path=path, offset=offset, length=length))

    @mcp.tool(name="read_multiple_files",
              description="Read the same bounded line range from up to 8 known files in one call.")
    def _read_multiple_files(paths: List[str], offset: int = 0,
                             length: Optional[int] = 120) -> Dict[str, Any]:
        return _log(audit_logger, "read_multiple_files",
                    lambda: read_multiple_files(settings, paths=paths,
                                                offset=offset, length=length))

    @mcp.tool(name="edit_file",
              description="Find-and-replace in a file. Fails if occurrence count != expected_replacements.")
    def _edit_file(path: str, old_string: str, new_string: str,
                   expected_replacements: int = 1) -> Dict[str, Any]:
        return _log(audit_logger, "edit_file",
                    lambda: edit_file(settings, path=path, old_string=old_string,
                                      new_string=new_string, expected_replacements=expected_replacements))

    @mcp.tool(name="move_file", description="Move or rename a file/directory.")
    def _move_file(source: str, destination: str) -> Dict[str, Any]:
        return _log(audit_logger, "move_file",
                    lambda: move_file(settings, source=source, destination=destination))

    @mcp.tool(name="copy_file", description="Copy a file or directory.")
    def _copy_file(source: str, destination: str) -> Dict[str, Any]:
        return _log(audit_logger, "copy_file",
                    lambda: copy_file(settings, source=source, destination=destination))

    @mcp.tool(name="delete_path",
              description="Delete a file or directory. Set recursive=true for directories.")
    def _delete_path(path: str, recursive: bool = False) -> Dict[str, Any]:
        return _log(audit_logger, "delete_path",
                    lambda: delete_path(settings, path=path, recursive=recursive))

    @mcp.tool(name="list_directory", description="List files and directories in a path.")
    def _list_directory(path: str) -> Dict[str, Any]:
        return _log(audit_logger, "list_directory",
                    lambda: list_directory(settings, path=path))

    @mcp.tool(name="directory_tree",
              description="Show directory structure as a tree. depth controls how deep.")
    def _directory_tree(path: str, depth: int = 3) -> Dict[str, Any]:
        return _log(audit_logger, "directory_tree",
                    lambda: directory_tree(settings, path=path, depth=depth))

    @mcp.tool(name="create_directory", description="Create a directory (and parents).")
    def _create_directory(path: str) -> Dict[str, Any]:
        return _log(audit_logger, "create_directory",
                    lambda: create_directory(settings, path=path))

    @mcp.tool(name="get_file_info",
              description="Get file/directory metadata: size, dates, type, permissions.")
    def _get_file_info(path: str) -> Dict[str, Any]:
        return _log(audit_logger, "get_file_info",
                    lambda: get_file_info(settings, path=path))

    @mcp.tool(name="find_files",
              description="Find files by name glob pattern (e.g. '*.py', 'report*'). file_type: file|dir|any.")
    def _find_files(pattern: str, path: str = str(Path.home()),
                    file_type: str = "any") -> Dict[str, Any]:
        return _log(audit_logger, "find_files",
                    lambda: find_files(settings, pattern=pattern, path=path, file_type=file_type))

    # ── Linux desktop compatibility tools ─────────────────────────────────────────────────────────
    @mcp.tool(name="run_applescript",
              description="AppleScript compatibility placeholder. On Linux, use shell/browser/desktop tools instead.")
    def _run_applescript(script: str, timeout_s: int = 30) -> Dict[str, Any]:
        return _log(audit_logger, "run_applescript",
                    lambda: run_applescript(settings, script=script, timeout_s=timeout_s))

    @mcp.tool(name="send_notification",
              description="Send a Linux desktop notification banner. sound is ignored.")
    def _send_notification(title: str, message: str, sound: str = "Pop") -> Dict[str, Any]:
        return _log(audit_logger, "send_notification",
                    lambda: send_notification(settings, title=title, message=message, sound=sound))

    @mcp.tool(name="clipboard_get", description="Read the current Linux clipboard contents.")
    def _clipboard_get() -> Dict[str, Any]:
        return _log(audit_logger, "clipboard_get", lambda: clipboard_get(settings))

    @mcp.tool(name="clipboard_set", description="Write text to the Linux clipboard.")
    def _clipboard_set(content: str) -> Dict[str, Any]:
        return _log(audit_logger, "clipboard_set",
                    lambda: clipboard_set(settings, content=content))

    @mcp.tool(name="open_app",
              description="Open a Linux application by executable or .desktop id.")
    def _open_app(app_name: str) -> Dict[str, Any]:
        return _log(audit_logger, "open_app",
                    lambda: open_app(settings, app_name=app_name))

    @mcp.tool(name="open_url", description="Open a URL in the default browser.")
    def _open_url(url: str) -> Dict[str, Any]:
        return _log(audit_logger, "open_url", lambda: open_url(settings, url=url))

    @mcp.tool(name="set_volume", description="Set system volume (0-100).")
    def _set_volume(level: int) -> Dict[str, Any]:
        return _log(audit_logger, "set_volume",
                    lambda: set_volume(settings, level=level))

    @mcp.tool(name="get_volume", description="Get current system volume level.")
    def _get_volume() -> Dict[str, Any]:
        return _log(audit_logger, "get_volume", lambda: get_volume(settings))

    @mcp.tool(name="set_brightness",
              description="Set screen brightness (0-100). Requires brightnessctl or xrandr.")
    def _set_brightness(level: int) -> Dict[str, Any]:
        return _log(audit_logger, "set_brightness",
                    lambda: set_brightness(settings, level=level))

    @mcp.tool(name="screenshot",
              description="Take a screenshot. path: save location. window=true for interactive window select.")
    def _screenshot(path: str = str(Path.home() / "Desktop" / "screenshot.png"),
                    window: bool = False) -> Dict[str, Any]:
        return _log(audit_logger, "screenshot",
                    lambda: screenshot(settings, path=path, window=window))

    @mcp.tool(name="set_reminder",
              description="Add a Linux desktop reminder when systemd-run/at is available. due_date format: 'month/day/year HH:MM AM/PM'.")
    def _set_reminder(title: str, notes: str = "",
                      due_date: Optional[str] = None) -> Dict[str, Any]:
        return _log(audit_logger, "set_reminder",
                    lambda: set_reminder(settings, title=title, notes=notes, due_date=due_date))

    @mcp.tool(name="get_running_apps",
              description="Get list of currently running Linux applications/windows when possible.")
    def _get_running_apps() -> Dict[str, Any]:
        return _log(audit_logger, "get_running_apps", lambda: get_running_apps(settings))

    # ── Search tools ────────────────────────────────────────────────────────
    @mcp.tool(name="search_files",
              description="Search file contents and return at most max_results concise matching lines.")
    def _search_files(pattern: str, path: str = str(Path.home()),
                      include_extensions: Optional[List[str]] = None,
                      case_sensitive: bool = False,
                      max_results: int = 50) -> Dict[str, Any]:
        return _log(audit_logger, "search_files",
                    lambda: search_files(settings, pattern=pattern, path=path,
                                         include_extensions=include_extensions,
                                         case_sensitive=case_sensitive,
                                         max_results=max_results))

    @mcp.tool(name="spotlight_search",
              description="Search files by name on Linux using fd, locate, or find.")
    def _spotlight_search(query: str, max_results: int = 50) -> Dict[str, Any]:
        return _log(audit_logger, "spotlight_search",
                    lambda: spotlight_search(settings, query=query, max_results=max_results))

    # ── HTTP tool ───────────────────────────────────────────────────────────
    @mcp.tool(name="http_request",
              description="Make HTTP GET/POST/PUT/DELETE requests to external URLs.")
    def _http_request(url: str, method: str = "GET",
                      headers: Optional[Dict[str, str]] = None,
                      body: Optional[str] = None) -> Dict[str, Any]:
        return _log(audit_logger, "http_request",
                    lambda: http_request(settings, url=url, method=method,
                                         headers=headers, body=body))

    # ── Browser tools ────────────────────────────────────────────────────────
    @mcp.tool(name="browser_open_url",
              description="Open a URL in a Chrome/Chromium-compatible Linux browser via Chrome DevTools Protocol. browser: chrome|chromium|brave|edge.")
    def _browser_open_url(browser: str, url: str, new_tab: bool = True) -> Dict[str, Any]:
        return _log(audit_logger, "browser_open_url",
                    lambda: browser_open_url(settings, browser=browser, url=url, new_tab=new_tab))

    @mcp.tool(name="browser_list_tabs",
              description="List tabs in the Chrome/Chromium-compatible Linux CDP browser session.")
    def _browser_list_tabs(browser: str) -> Dict[str, Any]:
        return _log(audit_logger, "browser_list_tabs",
                    lambda: browser_list_tabs(settings, browser=browser))

    @mcp.tool(name="browser_activate_tab",
              description="Switch to a specific tab by window_index and tab_index.")
    def _browser_activate_tab(browser: str, window_index: int = 1, tab_index: int = 1) -> Dict[str, Any]:
        return _log(audit_logger, "browser_activate_tab",
                    lambda: browser_activate_tab(settings, browser=browser, window_index=window_index, tab_index=tab_index))

    @mcp.tool(name="browser_close_tab",
              description="Close a tab by window_index and tab_index.")
    def _browser_close_tab(browser: str, window_index: int = 1, tab_index: int = 1) -> Dict[str, Any]:
        return _log(audit_logger, "browser_close_tab",
                    lambda: browser_close_tab(settings, browser=browser, window_index=window_index, tab_index=tab_index))

    @mcp.tool(name="browser_execute_js",
              description="Execute JavaScript in a browser tab and return the result.")
    def _browser_execute_js(browser: str, js: str, window_index: int = 1,
                             tab_index: Optional[int] = None) -> Dict[str, Any]:
        return _log(audit_logger, "browser_execute_js",
                    lambda: browser_execute_js(settings, browser=browser, js=js,
                                               window_index=window_index, tab_index=tab_index))

    @mcp.tool(name="browser_click_selector",
              description="Click an element by CSS selector in a browser tab.")
    def _browser_click_selector(browser: str, css_selector: str, window_index: int = 1,
                                 tab_index: Optional[int] = None) -> Dict[str, Any]:
        return _log(audit_logger, "browser_click_selector",
                    lambda: browser_click_selector(settings, browser=browser, css_selector=css_selector,
                                                   window_index=window_index, tab_index=tab_index))

    @mcp.tool(name="browser_type_selector",
              description="Type text into an element by CSS selector. clear=true clears first.")
    def _browser_type_selector(browser: str, css_selector: str, text: str, clear: bool = True,
                                window_index: int = 1, tab_index: Optional[int] = None) -> Dict[str, Any]:
        return _log(audit_logger, "browser_type_selector",
                    lambda: browser_type_selector(settings, browser=browser, css_selector=css_selector,
                                                  text=text, clear=clear, window_index=window_index, tab_index=tab_index))

    @mcp.tool(name="browser_wait_for_selector",
              description="Wait until a CSS selector appears in the page. Returns found=true/false.")
    def _browser_wait_for_selector(browser: str, css_selector: str, timeout_s: int = 20,
                                    window_index: int = 1, tab_index: Optional[int] = None) -> Dict[str, Any]:
        return _log(audit_logger, "browser_wait_for_selector",
                    lambda: browser_wait_for_selector(settings, browser=browser, css_selector=css_selector,
                                                      timeout_s=timeout_s, window_index=window_index, tab_index=tab_index))

    @mcp.tool(name="browser_get_html",
              description="Get the full HTML of the current page in a browser tab.")
    def _browser_get_html(browser: str, max_chars: Optional[int] = None,
                          window_index: int = 1, tab_index: Optional[int] = None) -> Dict[str, Any]:
        return _log(audit_logger, "browser_get_html",
                    lambda: browser_get_html(settings, browser=browser, max_chars=max_chars,
                                             window_index=window_index, tab_index=tab_index))

    @mcp.tool(name="browser_wait_for_download",
              description="Wait for a new file to appear in ~/Downloads. filename_contains filters by name.")
    def _browser_wait_for_download(filename_contains: Optional[str] = None,
                                    timeout_s: int = 60) -> Dict[str, Any]:
        return _log(audit_logger, "browser_wait_for_download",
                    lambda: browser_wait_for_download(settings, filename_contains=filename_contains, timeout_s=timeout_s))

    @mcp.tool(name="browser_screenshot",
              description=(
                  "Captures only the browser window, not the full screen. "
                  "Use return_base64=true to return a base64 PNG. Use path to choose the output file."
              ))
    def _browser_screenshot(browser: str, path: Optional[str] = None,
                             window_index: int = 1, return_base64: bool = True) -> Dict[str, Any]:
        return _log(audit_logger, "browser_screenshot",
                    lambda: browser_screenshot(settings, browser=browser, path=path,
                                               window_index=window_index, return_base64=return_base64))

    @mcp.tool(name="browser_scroll",
              description=(
                  "Scrolls the page. If selector is provided, scrolls that element. "
                  "If selector is not provided, scrolls by dx and dy pixels. "
                  "Example: dy=500 scrolls down, dy=-500 scrolls up."
              ))
    def _browser_scroll(browser: str, dx: int = 0, dy: int = 300,
                        selector: Optional[str] = None, window_index: int = 1,
                        tab_index: Optional[int] = None) -> Dict[str, Any]:
        return _log(audit_logger, "browser_scroll",
                    lambda: browser_scroll(settings, browser=browser, dx=dx, dy=dy,
                                           selector=selector, window_index=window_index, tab_index=tab_index))

    @mcp.tool(name="browser_press_key",
              description=(
                  "Sends a keyboard key to the browser. "
                  "Key examples: 'return', 'escape', 'tab', 'space', 'delete', 'up', 'down', 'left', 'right', "
                  "'f5', 'a', 'A'. "
                  "modifiers listesi: ['cmd'], ['shift'], ['cmd','shift'] gibi. "
                  "Example: key='a', modifiers=['ctrl'] sends Ctrl+A."
              ))
    def _browser_press_key(browser: str, key: str, modifiers: Optional[List[str]] = None,
                            window_index: int = 1) -> Dict[str, Any]:
        return _log(audit_logger, "browser_press_key",
                    lambda: browser_press_key(settings, browser=browser, key=key,
                                              modifiers=modifiers, window_index=window_index))

    @mcp.tool(name="browser_coordinate_click",
              description=(
                  "Clicks an absolute X/Y screen coordinate. "
                  "Use rect.x and rect.y values from browser_get_snapshot. "
                  "Set double_click=true to double-click."
              ))
    def _browser_coordinate_click(browser: str, x: int, y: int,
                                   double_click: bool = False,
                                   window_index: int = 1) -> Dict[str, Any]:
        return _log(audit_logger, "browser_coordinate_click",
                    lambda: browser_coordinate_click(settings, browser=browser, x=x, y=y,
                                                     double_click=double_click, window_index=window_index))

    @mcp.tool(name="browser_get_snapshot",
              description=(
                  "Returns the visible DOM tree. Each element includes tag, text, id, class, and "
                  "screen coordinates (rect.x, rect.y, rect.w, rect.h). "
                  "Use these coordinates with browser_coordinate_click. "
                  "Use max_depth and max_children to limit traversal."
              ))
    def _browser_get_snapshot(browser: str, window_index: int = 1,
                               tab_index: Optional[int] = None,
                               max_depth: int = 6, max_children: int = 25) -> Dict[str, Any]:
        return _log(audit_logger, "browser_get_snapshot",
                    lambda: browser_get_snapshot(settings, browser=browser, window_index=window_index,
                                                 tab_index=tab_index, max_depth=max_depth,
                                                 max_children=max_children))

    # ── Interactive tools ─────────────────────────────────────────────────────
    @mcp.tool(
        name="ask_user",
        description=(
            "Ask the local user an interactive question or request guidance. "
            "A local Linux dialog opens with your question/message at the top, "
            "and an input field for the user's answer. "
            "When the user sends an answer, the response is returned to you. "
            "Skip or timeout returns response=null. "
            "Use this to get approval, preferences, or missing information without stopping an autonomous task."
        ),
    )
    def _ask_user(
        question: str,
        sender: str = "AI",
        timeout_s: int = 60,
    ) -> Dict[str, Any]:
        return _log(
            audit_logger, "ask_user",
            lambda: ask_user(settings, question=question, sender=sender, timeout_s=timeout_s),
        )

    # ── App setup ────────────────────────────────────────────────────────────
    app = mcp.streamable_http_app()
    app.add_middleware(SecurityMiddleware)

    async def health(_: Request) -> Response:
        return JSONResponse({"ok": True, "server": "linux-mcp", "workdir": str(settings.workdir)})

    async def metrics(request: Request) -> Response:
        from .telemetry import summarize_audit_metrics
        return JSONResponse(summarize_audit_metrics(request.query_params.get("range", "30d")))

    app.router.routes.append(Route("/health", health, methods=["GET"]))
  app.router.routes.append(Route("/metrics", metrics, methods=["GET"]))

    async def reset_metrics(_: Request) -> Response:
        audit_log = BASE_DIR / "audit.log"
        if audit_log.exists():
            from datetime import datetime
            archive = BASE_DIR / f"audit-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
            audit_log.rename(archive)
        return JSONResponse({"ok": True, "message": "Audit log archived; fresh counting started."})

    app.router.routes.append(Route("/metrics/reset", reset_metrics, methods=["POST"]))
    # REST API — FastAPI sub-app mounted at /api
    from fastapi import FastAPI
    from .rest_routes import router as rest_router
    rest_app = FastAPI()
    rest_app.include_router(rest_router)
    app.mount("/api", rest_app)

    return app


app = create_app()
