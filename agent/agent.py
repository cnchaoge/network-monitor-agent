#!/usr/bin/env python3
"""
企业网络监控平台 - 客户端 Agent
部署在客户内网机器上，定时探测并上报数据
"""

import socket
import time
import urllib.request
import urllib.error
import json
import sys
import os
import uuid

# ─── 配置 ───────────────────────────────────────────────────────────────────

# 服务器地址（改成你的云服务器 IP 或域名）
SERVER_URL = "http://82.156.229.67:8000"

# Agent 名称（内网机器识别用，可自定义）
AGENT_NAME = socket.gethostname()

# 客户名称
CUSTOMER_NAME = ""

# 上报间隔（秒）
REPORT_INTERVAL = 60  # 1分钟

# 探测目标（默认DNS和网关，可按需修改）
TARGETS = [
    {"name": "网关", "host": "192.168.1.1"},
    {"name": "DNS", "host": "8.8.8.8"},
    {"name": "ERP", "host": "erp.example.com"},  # 按需修改
]

# ─── Agent ID（首次运行自动生成，之后持久化）───────────────────────────────────

ID_FILE = os.path.expanduser("~/.network_monitor_agent_id")

def get_or_create_agent_id():
    if os.path.exists(ID_FILE):
        with open(ID_FILE) as f:
            return f.read().strip()
    agent_id = str(uuid.uuid4())[:8]
    with open(ID_FILE, "w") as f:
        f.write(agent_id)
    return agent_id

AGENT_ID = get_or_create_agent_id()

# ─── 探测函数 ───────────────────────────────────────────────────────────────

def ping_once(host, timeout=3):
    """用 TCP 连接探测主机是否可达，返回延迟ms"""
    try:
        start = time.time()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        # 尝试常见的 HTTP/HTTPS 端口
        for port in [80, 443]:
            try:
                result = sock.connect_ex((host, port))
                if result == 0:
                    sock.close()
                    return (time.time() - start) * 1000
            except Exception:
                pass
        sock.close()
        return None
    except Exception:
        return None


def measure_dns(host="www.baidu.com"):
    """测 DNS 解析时间"""
    try:
        start = time.time()
        socket.gethostbyname(host)
        return (time.time() - start) * 1000
    except Exception:
        return None


def probe_target(target):
    """探测单个目标，返回 (可达, 延迟ms)"""
    rtt = ping_once(target["host"])
    return (rtt is not None, rtt)


def ping_multi(host, count=3, timeout=2):
    """
    多次 Ping 探测，计算丢包率和平均延迟
    先用 socket connect 试端口，再用 subprocess 试 ICMP
    """
    rtts = []
    for _ in range(count):
        rtt = ping_once(host, timeout=timeout)
        if rtt is not None:
            rtts.append(rtt)
        time.sleep(0.5)
    loss = (count - len(rtts)) / count * 100
    avg_rtt = sum(rtts) / len(rtts) if rtts else None
    return bool(rtts), avg_rtt, loss


def get_gateway():
    """获取本机默认网关"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        # 简单推断网关
        parts = local_ip.rsplit(".", 1)
        gateway = parts[0] + ".1"
        return gateway
    except Exception:
        return "192.168.1.1"


# ─── 上报 ───────────────────────────────────────────────────────────────────

def register_and_get_id():
    """向服务端注册，返回 agent_id"""
    try:
        req = urllib.request.Request(
            f"{SERVER_URL}/api/register",
            data=json.dumps({
                "name": AGENT_NAME,
                "customer_name": CUSTOMER_NAME,
                "location": "",
                "remark": ""
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            agent_id = result.get("agent_id")
            if agent_id:
                with open(ID_FILE, "w") as f:
                    f.write(agent_id)
            return agent_id
    except Exception as e:
        print(f"[ERROR] 注册失败: {e}")
        return None


def report(data):
    """上报探测数据"""
    try:
        url = f"{SERVER_URL}/api/{AGENT_ID}/report"
        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"[ERROR] 上报失败: {e}")
        return None


# ─── 主循环 ─────────────────────────────────────────────────────────────────

def run_probe():
    gateway = get_gateway()

    # 探测网关
    gw_ok, gw_rtt, gw_loss = ping_multi(gateway)

    # 探测目标列表
    target_ok = True
    target_rtt = None
    target_name = ""
    for t in TARGETS:
        ok, rtt = probe_target(t["host"])
        if ok:
            target_ok = True
            target_rtt = rtt
            target_name = t["name"]
            break
        target_ok = False

    # DNS 延迟
    dns_ms = measure_dns()

    data = {
        "ping_ok": gw_ok,
        "ping_rtt_ms": gw_rtt,
        "ping_loss_pct": gw_loss,
        "dns_ms": dns_ms,
        "gateway_reachable": gw_ok,
        "target_reachable": target_ok,
        "target_name": target_name,
        "target_rtt_ms": target_rtt,
    }
    return data


def main():
    print(f"[Agent] 网络监控 Agent 启动，ID: {AGENT_ID}")
    print(f"[Agent] 服务端: {SERVER_URL}")
    print(f"[Agent] 探测间隔: {REPORT_INTERVAL} 秒")
    print(f"[Agent] 按 Ctrl+C 停止\n")

    # 首次启动尝试注册
    if not os.path.exists(ID_FILE):
        print("[Agent] 首次运行，正在注册到服务端...")
        new_id = register_and_get_id()
        if new_id:
            AGENT_ID = new_id
            print(f"[Agent] 注册成功，ID: {AGENT_ID}")
        else:
            print("[Agent] 注册失败，将使用本地ID继续尝试")

    while True:
        try:
            data = run_probe()
            result = report(data)

            if result and result.get("ok"):
                status = "OK" if data["ping_ok"] else "FAIL"
                print(f"[{time.strftime('%H:%M:%S')}] [{status}] "
                      f"网关延迟:{data['ping_rtt_ms']:.1f}ms "
                      f"DNS:{data['dns_ms']:.1f}ms "
                      f"目标:{data['target_name'] or 'N/A'} "
                      f"可达:{data['target_reachable']}")
            else:
                print(f"[{time.strftime('%H:%M:%S')}] 上报失败")

        except KeyboardInterrupt:
            print("\n[Agent] 停止")
            break
        except Exception as e:
            print(f"[ERROR] {e}")

        time.sleep(REPORT_INTERVAL)


if __name__ == "__main__":
    main()
