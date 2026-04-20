"""
企业网络监控平台 - FastAPI 服务端 v0.4 SNMP 支持
"""
import sqlite3
import uuid
import time
import io
import secrets
import threading
import asyncio
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
import socket

# SNMP 支持
try:
    from pysnmp.hlapi import getCmd, SnmpEngine, CommunityData, UdpTransportTarget, ContextData, ObjectIdentity
    SNMP_AVAILABLE = True
except ImportError:
    SNMP_AVAILABLE = False
    print("[SNMP] pysnmp not installed, SNMP polling disabled")

# ─── 数据库 ─────────────────────────────────────────────────────────────────

DB_PATH = Path(__file__).parent / "monitor.db"

# ─── Server酱报警配置 ────────────────────────────────────────────────────────
SCKEY = "SCT339677TkI9RsTLtYUzaqgsUNBTH8XcN"
ALERT_THRESHOLD_SEC = 180

# ─── 管理员配置 ────────────────────────────────────────────────────────────
ADMIN_PASSWORD = "lanwatch2026"

# ─── SSE 实时推送 ──────────────────────────────────────────────────────────

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
        if "subnets" not in cols:
            c.execute("ALTER TABLE agents ADD COLUMN subnets TEXT DEFAULT ''")

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

        # 拓扑数据表
        c.execute("""
            CREATE TABLE IF NOT EXISTS topology (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                ip TEXT NOT NULL,
                mac TEXT NOT NULL,
                hostname TEXT DEFAULT '',
                vendor TEXT DEFAULT '',
                device_type TEXT DEFAULT 'unknown',
                last_seen REAL DEFAULT 0,
                discovered_at REAL DEFAULT 0,
                UNIQUE(agent_id, ip)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_topology_agent ON topology(agent_id)")

        # SNMP 设备表
        c.execute("""
            CREATE TABLE IF NOT EXISTS snmp_devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip TEXT UNIQUE NOT NULL,
                community TEXT NOT NULL DEFAULT 'public',
                device_name TEXT NOT NULL DEFAULT '',
                device_type TEXT DEFAULT 'router',
                status TEXT DEFAULT 'unknown',
                last_poll REAL DEFAULT 0,
                created_at REAL NOT NULL
            )
        """)
        # SNMP 指标数据表
        c.execute("""
            CREATE TABLE IF NOT EXISTS snmp_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL,
                oid TEXT NOT NULL,
                value REAL NOT NULL,
                timestamp REAL NOT NULL,
                FOREIGN KEY (device_id) REFERENCES snmp_devices(id)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_snmp_metrics_device ON snmp_metrics(device_id, timestamp DESC)")
        conn.commit()
        print("[DB] initialized at", DB_PATH)
    finally:
        close_db(conn)

# ─── SNMP 轮询引擎 ──────────────────────────────────────────────────────────

# 常用 OID 定义
SNMP_OIDS = {
    "ifInOctets":   ObjectIdentity('1.3.6.1.2.1.2.2.1.10'),   # 接口入流量
    "ifOutOctets":  ObjectIdentity('1.3.6.1.2.1.2.2.1.16'),  # 接口出流量
    "ifInUcastPkts":ObjectIdentity('1.3.6.1.2.1.2.2.1.11'),  # 接口入单播包
    "ifOutUcastPkts":ObjectIdentity('1.3.6.1.2.1.2.2.1.17'), # 接口出单播包
    "ifOperStatus": ObjectIdentity('1.3.6.1.2.1.2.2.1.8'),   # 接口状态
    "sysUpTime":    ObjectIdentity('1.3.6.1.2.1.1.3.0'),     # 运行时长
    "hrProcessorLoad": ObjectIdentity('1.3.6.1.2.1.25.3.3.1.2'), # CPU负载
    "hrStorageUsed": ObjectIdentity('1.3.6.1.2.1.25.2.3.1.6'), # 存储使用
    "hrStorageSize": ObjectIdentity('1.3.6.1.2.1.25.2.3.1.5'), # 存储总量
}

SNMP_INTERVAL = 300  # 5分钟轮询一次

def snmp_get(ips, community, oids):
    """同步 SNMP GET，支持多个 OID"""
    results = {}
    for oid_name, oid_obj in oids.items():
        snmpEngine = SnmpEngine()
        try:
            g = getCmd(
                snmpEngine,
                CommunityData(community, mpModel=1),
                UdpTransportTarget((ips, 161), timeout=3, retries=0),
                ContextData(),
                oid_obj
            )
            error_indication, error_status, error_index, var_binds = next(g)
            snmpEngine.transportDispatcher.closeDispatcher()
            if error_indication:
                results[oid_name] = None
            else:
                for var_bind in var_binds:
                    val = var_bind[1]
                    try:
                        results[oid_name] = int(val)
                    except Exception:
                        try:
                            results[oid_name] = float(val)
                        except Exception:
                            results[oid_name] = str(val)
        except StopIteration:
            results[oid_name] = None
        except Exception as e:
            results[oid_name] = None
        finally:
            try:
                snmpEngine.transportDispatcher.closeDispatcher()
            except Exception:
                pass
    return results

def poll_snmp_devices():
    """轮询所有 SNMP 设备并写入指标"""
    if not SNMP_AVAILABLE:
        return
    conn = get_db()
    try:
        devices = conn.execute("SELECT * FROM snmp_devices").fetchall()
        now = time.time()
        for dev in devices:
            metrics = snmp_get(dev['ip'], dev['community'], SNMP_OIDS)
            status = "online" if any(v is not None for v in metrics.values()) else "offline"
            conn.execute(
                "UPDATE snmp_devices SET status=?, last_poll=? WHERE id=?",
                (status, now, dev['id'])
            )
            for oid_name, value in metrics.items():
                if value is not None:
                    conn.execute(
                        "INSERT INTO snmp_metrics (device_id, oid, value, timestamp) VALUES (?,?,?,?)",
                        (dev['id'], oid_name, float(value), now)
                    )
            print(f"[SNMP] polled {dev['ip']} status={status} metrics={sum(1 for v in metrics.values() if v is not None)}")
        conn.commit()
        notify_agents_updated()
    finally:
        close_db(conn)

def start_snmp_poller():
    """启动后台 SNMP 轮询线程"""
    def loop():
        while True:
            poll_snmp_devices()
            time.sleep(SNMP_INTERVAL)
    if SNMP_AVAILABLE:
        t = threading.Thread(target=loop, daemon=True)
        t.start()
        print("[SNMP] poller started, interval=%ds" % SNMP_INTERVAL)
    else:
        print("[SNMP] poller disabled (pysnmp not available)")

# ─── SNMP Trap 接收器 ────────────────────────────────────────────────────────

from pysnmp.proto.api import v2c

def start_snmp_trap_receiver(port=10162):
    """启动 UDP 服务器接收 SNMP Trap，存入数据库"""
    import socket

    def parse_trap(data):
        """简单解析 SNMP Trap PDU"""
        try:
            # v2c Trap 格式: community + PDU
            # PDU: request-id, error-status, error-index, varbinds
            # varbinds: (OID, value) 列表
            msg = {}
            varbinds = []
            # 简单解析：尝试提取 community 字符串和 varbind 对
            # 格式大致是: community_string(压测) + 0x30(pdu) + ...
            # 这里做简化：把 trap 里包含的字符串和 OID 提取出来
            # 实际生产建议用 pysnmp 的 recvTrapIndication 回调
            return {
                'source': data[:20].hex() if len(data) >= 20 else data.hex(),
                'raw_len': len(data),
                'status': 'received'
            }
        except Exception as e:
            return {'status': 'parse_error', 'error': str(e)}

    def handle_trap(sock):
        try:
            data, addr = sock.recvfrom(4096)
            print(f"[SNMP Trap] received from {addr[0]}:{addr[1]}, {len(data)} bytes")
            result = parse_trap(data)
            # 查找对应的 snmp_device
            conn = get_db()
            try:
                dev = conn.execute(
                    "SELECT * FROM snmp_devices WHERE ip=?", (addr[0],)
                ).fetchone()
                if dev:
                    now = time.time()
                    conn.execute(
                        "UPDATE snmp_devices SET status='online', last_poll=? WHERE id=?",
                        (now, dev['id'])
                    )
                    # 写入一条 trap 记录（用 oid='trap', value=1 标记）
                    conn.execute(
                        "INSERT INTO snmp_metrics (device_id, oid, value, timestamp) VALUES (?,?,?,?)",
                        (dev['id'], 'trap', 1.0, now)
                    )
                    conn.commit()
                    print(f"[SNMP Trap] matched device {dev['device_name']}({dev['ip']})")
                    notify_agents_updated()
                else:
                    print(f"[SNMP Trap] unknown device {addr[0]}")
            finally:
                close_db(conn)
        except Exception as e:
            print(f"[SNMP Trap] error: {e}")

    def trap_server():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(('', port))
            sock.settimeout(1.0)
            print(f"[SNMP Trap] receiver listening on UDP {port}")
            while True:
                try:
                    handle_trap(sock)
                except socket.timeout:
                    continue
                except Exception as e:
                    print(f"[SNMP Trap] server error: {e}")
        except Exception as e:
            print(f"[SNMP Trap] bind error: {e}")
        finally:
            sock.close()

    t = threading.Thread(target=trap_server, daemon=True)
    t.start()
    print(f"[SNMP Trap] receiver started on UDP {port}")

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
    subnets: Optional[str] = ""

class TopologyDevice(BaseModel):
    ip: str
    mac: str
    hostname: Optional[str] = ""
    vendor: Optional[str] = ""
    device_type: Optional[str] = "unknown"  # router / switch / server / printer / pc / unknown

class TopologyReport(BaseModel):
    devices: list[TopologyDevice]

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

# ─── 管理员接口 ────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    password: str

class UserUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None

class UserCreate(BaseModel):
    name: str
    phone: Optional[str] = ""

class AgentUpdate(BaseModel):
    name: Optional[str] = None
    customer_name: Optional[str] = None
    location: Optional[str] = None
    remark: Optional[str] = None

@app.post("/api/admin/login")
def admin_login(data: LoginRequest):
    if data.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="密码错误")
    return {"ok": True, "token": "admin-session"}

@app.get("/api/admin/users")
def list_all_users():
    conn = get_db()
    try:
        c = conn.execute("SELECT * FROM users ORDER BY created_at DESC")
        rows = c.fetchall()
        result = []
        for u in rows:
            udict = dict(u)
            agents = [dict(r) for r in conn.execute(
                "SELECT * FROM agents WHERE user_id=? ORDER BY created_at", (udict["id"],)).fetchall()]
            udict["agents"] = agents
            result.append(udict)
        return result
    finally:
        close_db(conn)

@app.post("/api/admin/users")
def create_user(data: UserCreate):
    conn = get_db()
    try:
        user_id = str(uuid.uuid4())[:8]
        token = secrets.token_hex(16)
        now = time.time()
        conn.execute(
            "INSERT INTO users (id, token, name, phone, created_at) VALUES (?,?,?,?,?)",
            (user_id, token, data.name, data.phone or "", now)
        )
        agent_id = str(uuid.uuid4())[:8]
        conn.execute(
            "INSERT INTO agents (id, user_id, name, created_at, customer_name) VALUES (?,?,?,?,?)",
            (agent_id, user_id, data.name + '-监控', now, data.name)
        )
        conn.commit()
        return {"user_id": user_id, "token": token, "agent_id": agent_id}
    finally:
        close_db(conn)

@app.patch("/api/admin/users/{user_id}")
def update_user(user_id: str, data: UserUpdate):
    conn = get_db()
    try:
        c = conn.execute("SELECT id FROM users WHERE id=?", (user_id,))
        if not c.fetchone():
            raise HTTPException(status_code=404, detail="用户不存在")
        fields, values = [], []
        if data.name is not None:
            fields.append("name=?"); values.append(data.name)
        if data.phone is not None:
            fields.append("phone=?"); values.append(data.phone)
        if not fields:
            return {"ok": True}
        values.append(user_id)
        conn.execute("UPDATE users SET " + ",".join(fields) + " WHERE id=?", values)
        conn.commit()
        return {"ok": True}
    finally:
        close_db(conn)

@app.delete("/api/admin/users/{user_id}")
def delete_user(user_id: str):
    conn = get_db()
    try:
        conn.execute("DELETE FROM agents WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        conn.commit()
        return {"ok": True}
    finally:
        close_db(conn)

@app.post("/api/admin/users/{user_id}/reset-token")
def reset_user_token(user_id: str):
    conn = get_db()
    try:
        c = conn.execute("SELECT id FROM users WHERE id=?", (user_id,))
        if not c.fetchone():
            raise HTTPException(status_code=404, detail="用户不存在")
        new_token = secrets.token_hex(16)
        conn.execute("UPDATE users SET token=? WHERE id=?", (new_token, user_id))
        conn.commit()
        return {"ok": True, "token": new_token}
    finally:
        close_db(conn)

@app.patch("/api/admin/agents/{agent_id}")
def update_agent_admin(agent_id: str, data: AgentUpdate):
    conn = get_db()
    try:
        c = conn.execute("SELECT id FROM agents WHERE id=?", (agent_id,))
        if not c.fetchone():
            raise HTTPException(status_code=404, detail="设备不存在")
        fields, values = [], []
        for key in ["name", "customer_name", "location", "remark"]:
            val = getattr(data, key, None)
            if val is not None:
                fields.append(f"{key}=?"); values.append(val)
        if not fields:
            return {"ok": True}
        values.append(agent_id)
        conn.execute("UPDATE agents SET " + ",".join(fields) + " WHERE id=?", values)
        conn.commit()
        return {"ok": True}
    finally:
        close_db(conn)

@app.delete("/api/admin/agents/{agent_id}")
def delete_agent_admin(agent_id: str):
    conn = get_db()
    try:
        conn.execute("DELETE FROM agents WHERE id=?", (agent_id,))
        conn.commit()
        return {"ok": True}
    finally:
        close_db(conn)

# ─── SNMP 设备管理 API ────────────────────────────────────────────────────

class SNMPDeviceCreate(BaseModel):
    ip: str
    community: str = "public"
    device_name: str = ""
    device_type: str = "router"

class SNMPDeviceUpdate(BaseModel):
    ip: Optional[str] = None
    community: Optional[str] = None
    device_name: Optional[str] = None
    device_type: Optional[str] = None

@app.get("/api/admin/snmp")
def list_snmp_devices():
    """列出所有 SNMP 设备"""
    conn = get_db()
    try:
        devices = conn.execute("SELECT * FROM snmp_devices ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in devices]
    finally:
        close_db(conn)

@app.post("/api/admin/snmp")
def create_snmp_device(data: SNMPDeviceCreate):
    """添加 SNMP 监控设备"""
    conn = get_db()
    try:
        now = time.time()
        conn.execute(
            "INSERT INTO snmp_devices (ip, community, device_name, device_type, created_at) VALUES (?,?,?,?,?)",
            (data.ip, data.community, data.device_name, data.device_type, now)
        )
        conn.commit()
        device = conn.execute("SELECT * FROM snmp_devices WHERE ip=?", (data.ip,)).fetchone()
        return dict(device)
    finally:
        close_db(conn)

@app.patch("/api/admin/snmp/{device_id}")
def update_snmp_device(device_id: int, data: SNMPDeviceUpdate):
    """更新 SNMP 设备"""
    conn = get_db()
    try:
        c = conn.execute("SELECT id FROM snmp_devices WHERE id=?", (device_id,))
        if not c.fetchone():
            raise HTTPException(status_code=404, detail="设备不存在")
        fields, values = [], []
        for key, val in {"ip": data.ip, "community": data.community,
                         "device_name": data.device_name, "device_type": data.device_type}.items():
            if val is not None:
                fields.append(f"{key}=?"); values.append(val)
        if not fields:
            return {"ok": True}
        values.append(device_id)
        conn.execute("UPDATE snmp_devices SET " + ",".join(fields) + " WHERE id=?", values)
        conn.commit()
        return {"ok": True}
    finally:
        close_db(conn)

@app.delete("/api/admin/snmp/{device_id}")
def delete_snmp_device(device_id: int):
    """删除 SNMP 设备"""
    conn = get_db()
    try:
        conn.execute("DELETE FROM snmp_metrics WHERE device_id=?", (device_id,))
        conn.execute("DELETE FROM snmp_devices WHERE id=?", (device_id,))
        conn.commit()
        return {"ok": True}
    finally:
        close_db(conn)

@app.get("/api/admin/snmp/{device_id}/metrics")
def get_snmp_metrics(device_id: int, limit: int = 60):
    """获取 SNMP 设备历史指标"""
    conn = get_db()
    try:
        c = conn.execute(
            "SELECT * FROM snmp_metrics WHERE device_id=? ORDER BY timestamp DESC LIMIT ?",
            (device_id, limit)
        )
        return [dict(r) for r in c.fetchall()]
    finally:
        close_db(conn)

@app.post("/api/admin/snmp/{device_id}/poll")
def poll_snmp_device_now(device_id: int):
    """手动触发一次 SNMP 轮询"""
    conn = get_db()
    try:
        device = conn.execute("SELECT * FROM snmp_devices WHERE id=?", (device_id,)).fetchone()
        if not device:
            raise HTTPException(status_code=404, detail="设备不存在")
        metrics = snmp_get(device['ip'], device['community'], SNMP_OIDS)
        now = time.time()
        status = "online" if any(v is not None for v in metrics.values()) else "offline"
        conn.execute("UPDATE snmp_devices SET status=?, last_poll=? WHERE id=?", (status, now, device_id))
        for oid_name, value in metrics.items():
            if value is not None:
                conn.execute(
                    "INSERT INTO snmp_metrics (device_id, oid, value, timestamp) VALUES (?,?,?,?)",
                    (device_id, oid_name, float(value), now)
                )
        conn.commit()
        return {"ok": True, "status": status, "metrics": metrics}
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
        if data.subnets:
            conn.execute("UPDATE agents SET subnets = ? WHERE id = ?", (data.subnets, agent_id))
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

@app.post("/api/{agent_id}/topology")
def report_topology(agent_id: str, data: TopologyReport):
    """接收 Agent 上报的局域网拓扑数据"""
    conn = get_db()
    try:
        c = conn.execute("SELECT id FROM agents WHERE id=?", (agent_id,))
        if not c.fetchone():
            raise HTTPException(status_code=404, detail="Agent not found")
        now = time.time()
        for dev in data.devices:
            conn.execute("""
                INSERT OR REPLACE INTO topology
                (agent_id, ip, mac, hostname, vendor, device_type, last_seen, discovered_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, (agent_id, dev.ip, dev.mac.upper(), dev.hostname or '',
                  dev.vendor or '', dev.device_type or 'unknown', now, now))
        conn.commit()
        return {"ok": True, "count": len(data.devices)}
    finally:
        close_db(conn)

@app.get("/api/{agent_id}/topology")
def get_topology(agent_id: str):
    """获取设备的局域网拓扑信息"""
    conn = get_db()
    try:
        c = conn.execute(
            "SELECT * FROM topology WHERE agent_id=? ORDER BY last_seen DESC",
            (agent_id,)
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

@app.get("/agent/{agent_id}")
def agent_detail(agent_id: str):
    """企业设备详情页（包含拓扑图）"""
    detail_path = Path(__file__).parent / "static" / "agent_detail.html"
    if detail_path.exists():
        return FileResponse(str(detail_path))
    return {"message": "Not found"}

@app.get("/admin")
def admin_page():
    """管理员后台"""
    admin_path = Path(__file__).parent / "static" / "admin.html"
    if admin_path.exists():
        return FileResponse(str(admin_path))
    return {"message": "Not found"}

@app.get("/download")
def download_page():
    """下载中心"""
    download_path = Path(__file__).parent / "static" / "download.html"
    if download_path.exists():
        return FileResponse(str(download_path))
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
    start_snmp_poller()
    start_snmp_trap_receiver()
    print("[Server] 企业网络监控平台启动 v0.4")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
