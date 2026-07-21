from __future__ import annotations

import hashlib, json, os, re, sqlite3, subprocess, time
from pathlib import Path
from typing import Any, Dict, Optional

SECRET = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization|cookie)=\S+|Bearer\s+\S+")
READ_ONLY = {"search_files", "read_file", "read_multiple_files", "get_job_status", "get_job_output"}

def _redact(value: str) -> str:
    return SECRET.sub(lambda m: m.group(0).split("=")[0] + "=<redacted>" if "=" in m.group(0) else "<redacted>", value)

def project_id(root: str) -> str:
    return hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:32]

class Ledger:
    def __init__(self, root: str, tool_version: str = "linux-mcp"):
        self.root = str(Path(root).resolve()); self.project_id = project_id(self.root); self.tool_version = tool_version
        base = Path(os.getenv("LINUX_MCP_HOME", str(Path.home()))) / ".linux-mcp" / "project-ledger" / self.project_id
        base.mkdir(parents=True, exist_ok=True); self.db = base / "ledger.sqlite3"
        with sqlite3.connect(self.db) as c:
            c.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, root TEXT UNIQUE, created REAL, tool_version TEXT);
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, project_id TEXT, conversation_id TEXT, goal TEXT, status TEXT, updated REAL);
            CREATE TABLE IF NOT EXISTS facts(id INTEGER PRIMARY KEY, project_id TEXT, task_id TEXT, conversation_id TEXT, kind TEXT, payload TEXT, fingerprint TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY, project_id TEXT, task_id TEXT, conversation_id TEXT, decision TEXT, reason TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS checkpoints(id INTEGER PRIMARY KEY, project_id TEXT, task_id TEXT, conversation_id TEXT, snapshot TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS exact_cache(key TEXT PRIMARY KEY, project_id TEXT, action TEXT, payload TEXT, deps TEXT, git_head TEXT, tool_version TEXT, policy TEXT, created REAL);
            """); c.execute("INSERT OR IGNORE INTO projects VALUES(?,?,?,?)", (self.project_id,self.root,time.time(),self.tool_version))
    def _git(self):
        try: return subprocess.check_output(["git","-C",self.root,"rev-parse","HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
        except Exception: return ""
    def task(self, task_id: str, conversation_id: str, goal: str, status: str="active"):
        with sqlite3.connect(self.db) as c: c.execute("INSERT OR REPLACE INTO tasks VALUES(?,?,?,?,?,?)",(task_id,self.project_id,conversation_id,_redact(goal),status,time.time()))
    def decision(self, task_id: str, conversation_id: str, decision: str, reason: str):
        with sqlite3.connect(self.db) as c: c.execute("INSERT INTO decisions(project_id,task_id,conversation_id,decision,reason,created) VALUES(?,?,?,?,?,?)",(self.project_id,task_id,conversation_id,_redact(decision),_redact(reason),time.time()))
    def fact(self, task_id: str, conversation_id: str, kind: str, payload: Any):
        def metadata(v):
            if isinstance(v, dict):
                return {k: (metadata(x) if k not in {"content", "stdout", "stderr", "command", "commands"} else {"sha256": hashlib.sha256(str(x).encode()).hexdigest(), "chars": len(str(x))}) for k,x in v.items()}
            if isinstance(v, list): return [metadata(x) for x in v]
            return v
        raw=_redact(json.dumps(metadata(payload),ensure_ascii=False,sort_keys=True,default=str)); fp=hashlib.sha256(raw.encode()).hexdigest()
        with sqlite3.connect(self.db) as c: c.execute("INSERT INTO facts(project_id,task_id,conversation_id,kind,payload,fingerprint,created) VALUES(?,?,?,?,?,?,?)",(self.project_id,task_id,conversation_id,kind,raw,fp,time.time()))
    def checkpoint(self, task_id: str, conversation_id: str, snapshot: Dict[str,Any]):
        raw=_redact(json.dumps(snapshot,ensure_ascii=False,sort_keys=True,default=str))
        with sqlite3.connect(self.db) as c: c.execute("INSERT INTO checkpoints(project_id,task_id,conversation_id,snapshot,created) VALUES(?,?,?,?,?)",(self.project_id,task_id,conversation_id,raw,time.time()))
    def state(self, task_id: str, conversation_id: str):
        with sqlite3.connect(self.db) as c:
            c.row_factory=sqlite3.Row
            task=c.execute("SELECT * FROM tasks WHERE id=? AND project_id=? AND conversation_id=?",(task_id,self.project_id,conversation_id)).fetchone()
            if not task: raise PermissionError("task is not owned by this project and conversation")
            return {"task":dict(task),"decisions":[dict(x) for x in c.execute("SELECT decision,reason,created FROM decisions WHERE project_id=? AND task_id=? AND conversation_id=? ORDER BY id DESC",(self.project_id,task_id,conversation_id))],"checkpoint":next((dict(x) for x in c.execute("SELECT snapshot,created FROM checkpoints WHERE project_id=? AND task_id=? AND conversation_id=? ORDER BY id DESC LIMIT 1",(self.project_id,task_id,conversation_id))),None)}
    def cache_key(self, action: str, args: Dict[str,Any], deps: Dict[str,str], policy: str):
        if action not in READ_ONLY or policy != "read-only": return None
        return hashlib.sha256(json.dumps([self.project_id,action,args,self._git(),deps,self.tool_version,policy],sort_keys=True,default=str).encode()).hexdigest()
    def cache_get(self,key):
        with sqlite3.connect(self.db) as c:
            row=c.execute("SELECT payload FROM exact_cache WHERE key=? AND project_id=? AND git_head=? AND tool_version=?",(key,self.project_id,self._git(),self.tool_version)).fetchone()
            return json.loads(row[0]) if row else None
    def cache_put(self,key,action,payload,deps,policy):
        with sqlite3.connect(self.db) as c: c.execute("INSERT OR REPLACE INTO exact_cache VALUES(?,?,?,?,?,?,?,?,?)",(key,self.project_id,action,json.dumps(payload,ensure_ascii=False),json.dumps(deps,sort_keys=True),self._git(),self.tool_version,policy,time.time()))
