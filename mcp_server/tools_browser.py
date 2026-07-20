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
