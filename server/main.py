"""
企业网络监控平台 - FastAPI 服务端 v0.1
"""
import sqlite3
import uuid
import time
import io
import secrets
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import StreamingResponse
from fastapi.responses import Response, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
import urllib.request
import urllib.parse
import qrcode

# ─── 数据库 ─────────────────────────────────────────────────────────────────

DB_PATH = Path(__file__).parent / "monitor.db"

# ─── Server酱报警配置 ────────────────────────────────────────────────────────
SCKEY = "SCT339677TkI9RsTLtYUzaqgsUNBTH8XcN"
ALERT_THRESHOLD_SEC = 180

# ─── SSE 实时推送 ──────────────────────────────────────────────────────────
import asyncio
import json
import threading

clients = set()  # 活跃的 SSE 连接
_clients_lock = threading.Lock()

def notify_agents_updated():
    """通知所有 SSE 客户端刷新设备列表（在独立线程中执行）"""
    event_data = f"data: {json.dumps({'type': 'refresh'}, ensure_ascii=False)}\n\n"
    with _clients_lock:
        dead = set()
        for queue in clients:
            try:
                queue.put_nowait(event_data)
            except Exception:
                dead.add(queue)
        for q in dead:
            clients.discard(q)

async def sse_generator():
    """SSE 流，广播设备列表更新"""
    queue = asyncio.Queue()
    with _clients_lock:
        clients.add(queue)
    try:
        while True:
            data = await queue.get()
            yield data
    finally:
        with _clients_lock:
            clients.discard(queue)

def send_alert(title, content):
    try:
        url = f"https://sc.ftqq.com/{SCKEY}.send"
        data = urllib.parse.urlencode({"text": title, "desp": content}).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read()
    except Exception as e:
        print(f"[Alert] 发送失败: {e}")
        return None

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
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                token TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                phone TEXT,
                created_at REAL NOT NULL
            )
        """)

        # 旧 agents 表可能没有 user_id，需要添加
        c.execute("PRAGMA table_info(agents)")
        cols = [row[1] for row in c.fetchall()]
        if "user_id" not in cols:
            c.execute("ALTER TABLE agents ADD COLUMN user_id TEXT NOT NULL DEFAULT 'default'")

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
        c.execute("CREATE INDEX IF NOT EXISTS idx_probes_agent_time ON probes(agent_id, timestamp DESC)")
        try:
            c.execute("CREATE INDEX IF NOT EXISTS idx_agents_user ON agents(user_id)")
        except Exception:
            pass
        conn.commit()
        print("[DB] initialized at", DB_PATH)
    finally:
        close_db(conn)

# ─── 数据模型 ────────────────────────────────────────────────────────────────

class UserRegister(BaseModel):
    name: str
    phone: Optional[str] = ""

class AgentRegister(BaseModel):
    name: str

class ProbeReport(BaseModel):
    ping_ok: bool
    ping_rtt_ms: Optional[float] = None
    ping_loss_pct: float = 0.0
    dns_ms: Optional[float] = None
    gateway_reachable: bool = True
    target_reachable: bool = True
    target_name: Optional[str] = ""
    target_rtt_ms: Optional[float] = None

# ─── 认证 ───────────────────────────────────────────────────────────────────

def get_current_user(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    token = authorization[7:]
    conn = get_db()
    try:
        c = conn.execute("SELECT * FROM users WHERE token = ?", (token,))
        user = c.fetchone()
        if not user:
            raise HTTPException(status_code=401, detail="无效的登录凭证")
        return dict(user)
    finally:
        close_db(conn)

# ─── FastAPI ────────────────────────────────────────────────────────────────

app = FastAPI(title="企业网络监控平台", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── 认证接口 ───────────────────────────────────────────────────────────────

@app.post("/api/register")
def register_user(data: UserRegister):
    """注册企业主账号，返回 token，并自动创建第一个监控设备"""
    conn = get_db()
    try:
        user_id = str(uuid.uuid4())[:8]
        token = secrets.token_hex(16)
        now = time.time()
        conn.execute(
            "INSERT INTO users (id, token, name, phone, created_at) VALUES (?,?,?,?,?)",
            (user_id, token, data.name, data.phone or "", now)
        )
        # 自动创建第一个监控设备
        agent_id = str(uuid.uuid4())[:8]
        conn.execute(
            "INSERT INTO agents (id, user_id, name, created_at, customer_name) VALUES (?,?,?,?,?)",
            (agent_id, user_id, data.name + '-监控', now, data.name)
        )
        conn.commit()
        return {"user_id": user_id, "token": token, "name": data.name, "agent_id": agent_id}
    finally:
        close_db(conn)

@app.get("/api/me")
def get_me(authorization: Optional[str] = Header(None)):
    """获取当前登录用户信息"""
    user = get_current_user(authorization)
    conn = get_db()
    try:
        c = conn.execute("SELECT * FROM agents WHERE user_id = ?", (user["id"],))
        agents = [dict(r) for r in c.fetchall()]
        return {"name": user["name"], "phone": user.get("phone"), "agents": agents}
    finally:
        close_db(conn)

# ─── 管理员接口（无认证） ─────────────────────────────────────────────────

@app.post("/api/agents")
def register_agent(data: AgentRegister, authorization: Optional[str] = Header(None)):
    """注册监控设备（需登录）"""
    user = get_current_user(authorization)
    conn = get_db()
    try:
        agent_id = str(uuid.uuid4())[:8]
        now = time.time()
        conn.execute(
            "INSERT INTO agents (id, user_id, name, created_at, customer_name) VALUES (?,?,?,?,?)",
            (agent_id, user["id"], data.name, now, user["name"])
        )
        conn.commit()
        return {"agent_id": agent_id}
    finally:
        close_db(conn)

@app.get("/api/agents")
def list_agents_admin():
    """所有设备列表（管理员用，暂不认证）"""
    conn = get_db()
    try:
        c = conn.execute("SELECT * FROM agents ORDER BY last_seen DESC")
        rows = c.fetchall()
        return [dict(r) for r in rows]
    finally:
        close_db(conn)

@app.get("/api/agents/{agent_id}")
def get_agent_admin(agent_id: str):
    conn = get_db()
    try:
        c = conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Not found")
        return dict(row)
    finally:
        close_db(conn)

@app.get("/api/agents/{agent_id}/qr")
def get_agent_qr(agent_id: str):
    """生成设备专属二维码"""
    conn = get_db()
    try:
        c = conn.execute("SELECT user_id FROM agents WHERE id=?", (agent_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Not found")
    finally:
        close_db(conn)

    url = f"http://82.156.229.67:8000/mobile?agent={agent_id}"
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(content=buf.read(), media_type="image/png")

@app.patch("/api/agents/{agent_id}")
def update_agent(agent_id: str, data: dict, authorization: Optional[str] = Header(None)):
    """更新设备信息（需登录，只能操作自己的设备）"""
    user = get_current_user(authorization)
    conn = get_db()
    try:
        c = conn.execute("SELECT user_id FROM agents WHERE id=?", (agent_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Not found")
        if row["user_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="无权操作")
        fields = []
        values = []
        for key in ["name", "customer_name", "location", "remark"]:
            if key in data and data[key] is not None:
                fields.append(f"{key}=?")
                values.append(data[key])
        if not fields:
            return {"ok": True}
        values.append(agent_id)
        conn.execute("UPDATE agents SET " + ",".join(fields) + " WHERE id=?", values)
        conn.commit()
        return {"ok": True}
    finally:
        close_db(conn)

@app.post("/api/admin/agents/{agent_id}/bind")
def bind_agent(agent_id: str, user_id: str = ""):
    """管理员：将设备绑定到用户（管理员调用，暂不认证）"""
    conn = get_db()
    try:
        c = conn.execute("SELECT id FROM agents WHERE id=?", (agent_id,))
        if not c.fetchone():
            raise HTTPException(status_code=404, detail="Agent not found")
        c2 = conn.execute("SELECT id FROM users WHERE id=?", (user_id,))
        if not c2.fetchone():
            raise HTTPException(status_code=404, detail="User not found")
        conn.execute("UPDATE agents SET user_id=? WHERE id=?", (user_id, agent_id))
        conn.commit()
        return {"ok": True}
    finally:
        close_db(conn)

# ─── 数据上报（Agent 用，无认证） ───────────────────────────────────────────

@app.post("/api/{agent_id}/report")
def report_probe(agent_id: str, data: ProbeReport):
    conn = get_db()
    try:
        c = conn.execute(
            "SELECT id, name, customer_name, last_seen FROM agents WHERE id = ?",
            (agent_id,)
        )
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Agent not found")

        now = time.time()
        last_seen = row["last_seen"] or 0

        # 离线报警
        offline_seconds = now - last_seen
        if last_seen > 0 and offline_seconds > ALERT_THRESHOLD_SEC:
            customer = row["customer_name"] or row["name"]
            offline_min = int(offline_seconds / 60)
            send_alert(
                f"⚠️ {customer} 网络离线 {offline_min} 分钟",
                f"设备：{row['name']}\n离线时间：约 {offline_min} 分钟\n检测时间：{time.strftime('%H:%M:%S')}"
            )

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
        conn.execute("UPDATE agents SET last_seen = ? WHERE id = ?", (now, agent_id))
        conn.commit()
        # 实时推送更新到所有浏览器
        asyncio.get_event_loop().call_soon_threadsafe(notify_agents_updated)
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

# ─── 报警测试 ─────────────────────────────────────────────────────────────

@app.get("/api/alert/test")
def test_alert():
    result = send_alert(
        "🧪 测试消息",
        f"报警通道正常\n时间：{time.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    return {"ok": True, "alert_sent": result is not None}

# ─── 健康检查 ───────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "db": str(DB_PATH)}

# ─── 静态页面 ─────────────────────────────────────────────────────────────

@app.get("/")
def index():
    static_path = Path(__file__).parent / "static" / "index.html"
    if static_path.exists():
        return FileResponse(str(static_path))
    return {"message": "网络监控平台"}

@app.get("/mobile")
def mobile():
    mobile_path = Path(__file__).parent / "static" / "mobile.html"
    if mobile_path.exists():
        return FileResponse(str(mobile_path))
    return {"message": "Not found"}

@app.get("/setup")
def setup():
    setup_path = Path(__file__).parent / "static" / "setup.html"
    if setup_path.exists():
        return FileResponse(str(setup_path))
    return {"message": "Not found"}

@app.get("/api/qr")
def generate_qr(text: str = ""):
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    img = qrcode.make(text)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(content=buf.read(), media_type="image/png")

@app.get("/api/events")
async def events():
    """SSE 实时推送，浏览器订阅此接口即可实时更新"""
    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )

# ─── 启动 ──────────────────────────────────────────────────────────────────

@app.on_event("startup")
def startup():
    init_db()
    print("[Server] 企业网络监控平台启动 v0.1")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
