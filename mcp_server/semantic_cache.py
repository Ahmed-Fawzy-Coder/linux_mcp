from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

SECRET = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization|cookie)\s*[:=]\s*\S+|bearer\s+\S+")
DENY = {"edit_file", "write_file", "write_files_batch", "run_command", "run_commands_parallel", "start_background_job", "stop_job"}
ALLOW = {"search_files", "read_file", "read_multiple_files", "get_job_status", "get_job_output"}

def _safe(text: str) -> bool:
    return not SECRET.search(text)

def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9_]{2,}", text.lower())
    aliases = {"authentication": "auth", "authenticate": "auth", "handlers": "handler", "locate": "find"}
    return {aliases.get(word, word.removesuffix("ing").removesuffix("s")) for word in words}

def _vector(text: str) -> dict[str, float]:
    tokens = _tokens(text)
    return {token: 1.0 + math.log1p(text.lower().count(token)) for token in tokens}

def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    common = set(a) & set(b)
    numerator = sum(a[k] * b[k] for k in common)
    denom = math.sqrt(sum(v*v for v in a.values()) * sum(v*v for v in b.values()))
    return numerator / denom if denom else 0.0

class SemanticCache:
    def __init__(self, db: Path, *, ttl_s: int = 3600, threshold: float = 0.92, enabled: bool = True):
        self.db = Path(db); self.ttl_s = max(1, int(ttl_s)); self.threshold = max(0.92, float(threshold)); self.enabled = enabled
        with sqlite3.connect(self.db) as c:
            c.execute("CREATE TABLE IF NOT EXISTS semantic_cache (key TEXT PRIMARY KEY, project_id TEXT, action TEXT, query TEXT, vector TEXT, payload TEXT, deps TEXT, tool_version TEXT, created REAL, expires REAL, quality REAL, feedback INTEGER)")

    def _key(self, project_id: str, action: str, query: str, deps: dict[str, str]) -> str:
        return hashlib.sha256(json.dumps([project_id, action, query, deps], sort_keys=True).encode()).hexdigest()

    def lookup(self, project_id: str, action: str, query: str, deps: dict[str, str], tool_version: str) -> Optional[dict[str, Any]]:
        if not self.enabled or action not in ALLOW or not _safe(query): return None
        now = time.time(); needle = _vector(query); best = None
        with sqlite3.connect(self.db) as c:
            rows = c.execute("SELECT key,query,vector,payload,deps,created,expires,quality FROM semantic_cache WHERE project_id=? AND action=? AND tool_version=? AND expires>?", (project_id, action, tool_version, now)).fetchall()
            for key, stored_query, vector, payload, stored_deps, created, expires, quality in rows:
                if json.loads(stored_deps) != deps: continue
                score = _cosine(needle, json.loads(vector))
                if score >= self.threshold and (best is None or score > best["confidence"]):
                    best = {**json.loads(payload), "cache": "semantic-hit", "confidence": round(score, 4), "age_s": max(0, int(now-created)), "source": "cached-candidate"}
        return best

    def put(self, project_id: str, action: str, query: str, deps: dict[str, str], payload: dict[str, Any], tool_version: str, quality: float = 1.0) -> bool:
        if not self.enabled or action not in ALLOW or not _safe(query) or not _safe(json.dumps(payload, ensure_ascii=False)): return False
        now = time.time(); key = self._key(project_id, action, query, deps)
        with sqlite3.connect(self.db) as c:
            c.execute("INSERT OR REPLACE INTO semantic_cache VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (key, project_id, action, query, json.dumps(_vector(query)), json.dumps(payload, ensure_ascii=False), json.dumps(deps, sort_keys=True), tool_version, now, now+self.ttl_s, float(quality), 0))
        return True

    def feedback(self, key: str, accepted: bool) -> None:
        with sqlite3.connect(self.db) as c: c.execute("UPDATE semantic_cache SET feedback=feedback+?, quality=MAX(0, MIN(1, quality+?)) WHERE key=?", (1 if accepted else 0, .01 if accepted else -.05, key))
