from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status

from .security import Settings, resolve_path, truncate

MAX_READ_CHARS = 200_000


# ── Write ────────────────────────────────────────────────────────────────────

def write_file(settings: Settings, path: str, content: str) -> Dict[str, Any]:
    target = resolve_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(target), "bytes": len(content.encode())}


def write_files_batch(settings: Settings, files: List[Dict[str, str]], atomic: bool = True) -> Dict[str, Any]:
    if not files:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "files list is empty.")
    written = []
    for item in files:
        p, c = item.get("path", ""), item.get("content", "")
        if not p:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Each file needs a 'path'.")
        t = resolve_path(p)
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text(c, encoding="utf-8")
        written.append(str(t))
    return {"ok": True, "written_count": len(written), "written": written}


# ── Read ─────────────────────────────────────────────────────────────────────

def read_file(settings: Settings, path: str, offset: int = 0, length: Optional[int] = None) -> Dict[str, Any]:
    target = resolve_path(path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"File not found: {path}")
    content = target.read_text(encoding="utf-8", errors="replace")
    if offset or length is not None:
        lines = content.splitlines(keepends=True)
        sliced = lines[offset: offset + length if length else None]
        content = "".join(sliced)
    bounded, truncated = truncate(content, MAX_READ_CHARS)
    return {"ok": True, "path": str(target), "content": bounded, "truncated": truncated}


def read_multiple_files(settings: Settings, paths: List[str]) -> Dict[str, Any]:
    results = []
    for path in paths:
        try:
            target = resolve_path(path)
            if target.exists() and target.is_file():
                content = target.read_text(encoding="utf-8", errors="replace")
                bounded, _ = truncate(content, 50_000)
                results.append({"path": path, "content": bounded, "status": "ok"})
            else:
                results.append({"path": path, "error": "Not found", "status": "error"})
        except Exception as e:
            results.append({"path": path, "error": str(e), "status": "error"})
    return {"ok": True, "files": results}


# ── Edit (find & replace) ────────────────────────────────────────────────────

def edit_file(settings: Settings, path: str, old_string: str, new_string: str,
              expected_replacements: int = 1) -> Dict[str, Any]:
    """Find-and-replace in a file. Fails if count doesn't match expected_replacements."""
    target = resolve_path(path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"File not found: {path}")
    content = target.read_text(encoding="utf-8")
    count = content.count(old_string)
    if count == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "old_string not found in file.")
    if count != expected_replacements:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"Found {count} occurrences but expected {expected_replacements}. "
                            "Be more specific or set expected_replacements correctly.")
    new_content = content.replace(old_string, new_string)
    target.write_text(new_content, encoding="utf-8")
    return {"ok": True, "path": str(target), "replacements": count}


# ── Directory ops ─────────────────────────────────────────────────────────────

def list_directory(settings: Settings, path: str) -> Dict[str, Any]:
    target = resolve_path(path)
    if not target.exists() or not target.is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Directory not found: {path}")
    entries = []
    for entry in sorted(target.iterdir()):
        stat = entry.stat()
        entries.append({
            "name": entry.name,
            "type": "directory" if entry.is_dir() else "file",
            "size": stat.st_size if entry.is_file() else None,
            "modified": time.ctime(stat.st_mtime),
        })
    return {"ok": True, "path": str(target), "count": len(entries), "entries": entries}


def directory_tree(settings: Settings, path: str, depth: int = 3) -> Dict[str, Any]:
    target = resolve_path(path)
    if not target.exists() or not target.is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Directory not found: {path}")

    def build(p: Path, d: int) -> Dict:
        node: Dict[str, Any] = {"name": p.name, "type": "directory"}
        if d <= 0:
            node["children"] = ["..."]
            return node
        children = []
        try:
            for entry in sorted(p.iterdir()):
                if entry.is_dir():
                    children.append(build(entry, d - 1))
                else:
                    children.append({"name": entry.name, "type": "file", "size": entry.stat().st_size})
        except PermissionError:
            pass
        node["children"] = children
        return node

    return {"ok": True, "path": str(target), "tree": build(target, depth)}


def create_directory(settings: Settings, path: str) -> Dict[str, Any]:
    target = resolve_path(path)
    target.mkdir(parents=True, exist_ok=True)
    return {"ok": True, "path": str(target)}


# ── Move / Copy / Delete ──────────────────────────────────────────────────────

def move_file(settings: Settings, source: str, destination: str) -> Dict[str, Any]:
    src = resolve_path(source)
    dst = resolve_path(destination)
    if not src.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Source not found: {source}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return {"ok": True, "source": str(src), "destination": str(dst)}


def copy_file(settings: Settings, source: str, destination: str) -> Dict[str, Any]:
    src = resolve_path(source)
    dst = resolve_path(destination)
    if not src.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Source not found: {source}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(str(src), str(dst))
    else:
        shutil.copy2(str(src), str(dst))
    return {"ok": True, "source": str(src), "destination": str(dst)}


def delete_path(settings: Settings, path: str, recursive: bool = False) -> Dict[str, Any]:
    target = resolve_path(path)
    if not target.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Path not found: {path}")
    if target.is_dir():
        if not recursive:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "Path is a directory. Set recursive=true to delete it.")
        shutil.rmtree(str(target))
    else:
        target.unlink()
    return {"ok": True, "deleted": str(target)}


# ── File info ─────────────────────────────────────────────────────────────────

def get_file_info(settings: Settings, path: str) -> Dict[str, Any]:
    target = resolve_path(path)
    if not target.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Path not found: {path}")
    stat = target.stat()
    return {
        "ok": True,
        "path": str(target),
        "type": "directory" if target.is_dir() else "file",
        "size": stat.st_size,
        "created": time.ctime(stat.st_ctime),
        "modified": time.ctime(stat.st_mtime),
        "mode": oct(stat.st_mode),
        "suffix": target.suffix,
    }


# ── Find files by name ────────────────────────────────────────────────────────

def find_files(settings: Settings, pattern: str, path: str = str(Path.home()),
               file_type: str = "any") -> Dict[str, Any]:
    """Find files/directories by name pattern (glob). file_type: file | dir | any."""
    root = resolve_path(path)
    if not root.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Path not found: {path}")
    results = []
    try:
        for match in sorted(root.rglob(pattern)):
            if file_type == "file" and not match.is_file():
                continue
            if file_type == "dir" and not match.is_dir():
                continue
            results.append({
                "path": str(match),
                "type": "directory" if match.is_dir() else "file",
                "size": match.stat().st_size if match.is_file() else None,
            })
            if len(results) >= 500:
                break
    except PermissionError:
        pass
    return {"ok": True, "count": len(results), "results": results}
