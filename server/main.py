"""
企业网络监控平台 - FastAPI 服务端
"""
import sqlite3
import uuid
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

# ─── 数据库 ─────────────────────────────────────────────────────────────────

DB_PATH = Path(__file__).parent / "monitor.db"

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def close_db(conn):
    if conn:
        conn.close()

def init_db():
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS agents (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                created_at REAL NOT NULL,
                last_seen REAL,
                customer_name TEXT,
                location TEXT,
                remark TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS probes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                ping_ok INTEGER,
                ping_rtt_ms REAL,
                ping_loss_pct REAL,
                dns_ms REAL,
                gateway_reachable INTEGER,
                target_reachable INTEGER,
                target_name TEXT,
                target_rtt_ms REAL,
                UNIQUE(agent_id, timestamp)
            )
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_probes_agent_time
            ON probes(agent_id, timestamp DESC)
        """)
        conn.commit()
        print("[DB] initialized at", DB_PATH)
    finally:
        close_db(conn)

# ─── 数据模型 ────────────────────────────────────────────────────────────────

class AgentRegister(BaseModel):
    name: str
    customer_name: Optional[str] = ""
    location: Optional[str] = ""
    remark: Optional[str] = ""

class ProbeReport(BaseModel):
    ping_ok: bool
    ping_rtt_ms: Optional[float] = None
    ping_loss_pct: float = 0.0
    dns_ms: Optional[float] = None
    gateway_reachable: bool = True
    target_reachable: bool = True
    target_name: Optional[str] = ""
    target_rtt_ms: Optional[float] = None

# ─── FastAPI ────────────────────────────────────────────────────────────────

app = FastAPI(title="企业网络监控平台", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── API ───────────────────────────────────────────────────────────────────

@app.post("/api/register")
def register(data: AgentRegister):
    conn = get_db()
    try:
        agent_id = str(uuid.uuid4())[:8]
        now = time.time()
        conn.execute(
            "INSERT INTO agents (id,name,created_at,customer_name,location,remark) VALUES (?,?,?,?,?,?)",
            (agent_id, data.name, now, data.customer_name, data.location, data.remark)
        )
        conn.commit()
        return {"agent_id": agent_id}
    finally:
        close_db(conn)

@app.post("/api/{agent_id}/report")
def report_probe(agent_id: str, data: ProbeReport):
    conn = get_db()
    try:
        c = conn.execute("SELECT id FROM agents WHERE id = ?", (agent_id,))
        if not c.fetchone():
            raise HTTPException(status_code=404, detail="Agent not found")
        now = time.time()
        conn.execute(
            """INSERT OR REPLACE INTO probes
               (agent_id, timestamp, ping_ok, ping_rtt_ms, ping_loss_pct,
                dns_ms, gateway_reachable, target_reachable, target_name, target_rtt_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (agent_id, now, int(data.ping_ok), data.ping_rtt_ms,
             data.ping_loss_pct, data.dns_ms,
             int(data.gateway_reachable), int(data.target_reachable),
             data.target_name, data.target_rtt_ms)
        )
        conn.execute(
            "UPDATE agents SET last_seen = ? WHERE id = ?", (now, agent_id)
        )
        conn.commit()
        return {"ok": True}
    finally:
        close_db(conn)

@app.get("/api/{agent_id}/latest")
def latest(agent_id: str):
    conn = get_db()
    try:
        c = conn.execute(
            "SELECT * FROM probes WHERE agent_id=? ORDER BY timestamp DESC LIMIT 1",
            (agent_id,)
        )
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="No data")
        return dict(row)
    finally:
        close_db(conn)

@app.get("/api/{agent_id}/history")
def history(agent_id: str, limit: int = 60):
    conn = get_db()
    try:
        c = conn.execute(
            "SELECT * FROM probes WHERE agent_id=? ORDER BY timestamp DESC LIMIT ?",
            (agent_id, limit)
        )
        rows = c.fetchall()
        return [dict(r) for r in rows]
    finally:
        close_db(conn)

@app.get("/api/agents")
def list_agents():
    conn = get_db()
    try:
        c = conn.execute("SELECT * FROM agents ORDER BY last_seen DESC")
        rows = c.fetchall()
        return [dict(r) for r in rows]
    finally:
        close_db(conn)

@app.get("/api/agents/{agent_id}")
def get_agent(agent_id: str):
    conn = get_db()
    try:
        c = conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Not found")
        return dict(row)
    finally:
        close_db(conn)

# ─── 健康检查 ───────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "db": str(DB_PATH)}

# ─── 启动 ──────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    from fastapi.responses import FileResponse
    static_path = Path(__file__).parent / "static" / "index.html"
    if static_path.exists():
        return FileResponse(str(static_path))
    return {"message": "网络监控平台"}

@app.get("/mobile")
def mobile():
    from fastapi.responses import FileResponse
    mobile_path = Path(__file__).parent / "static" / "mobile.html"
    if mobile_path.exists():
        return FileResponse(str(mobile_path))
    return {"message": "Not found"}

@app.on_event("startup")
def startup():
    init_db()
    print("[Server] 企业网络监控平台启动")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
