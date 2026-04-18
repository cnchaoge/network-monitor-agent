#!/usr/bin/env python3
"""
企业网络监控 - Windows Agent
最小化到系统托盘，开机自启
依赖: pip install pystray pillow pyinstaller
"""
import socket
import time
import json
import sys
import os
import uuid
import logging
import subprocess
import urllib.request
import urllib.error

# ─── 配置（修改这里） ────────────────────────────────────────────────────────
SERVER_URL = "http://82.156.229.67:8000"
REPORT_INTERVAL = 60  # 上报间隔（秒）
LOG_FILE = os.path.expanduser("~/.network_monitor_agent.log")

# 探测目标（按需修改）
TARGETS = [
    {"name": "网关", "host": "192.168.1.1"},
    {"name": "DNS", "host": "8.8.8.8"},
]
# ───────────────────────────────

# 日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger()

# ─── Agent ID 持久化 ─────────────────────────────────────────────────────────
ID_FILE = os.path.expanduser("~/.network_monitor_agent_id")

def get_or_create_id():
    if os.path.exists(ID_FILE):
        with open(ID_FILE) as f:
            return f.read().strip()
    agent_id = str(uuid.uuid4())[:8]
    with open(ID_FILE, "w") as f:
        f.write(agent_id)
    return agent_id

AGENT_ID = get_or_create_id()

# ─── 探测函数 ───────────────────────────────────────────────────────────────

def ping_once(host, timeout=3):
    try:
        start = time.time()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        for port in [80, 443]:
            try:
                if sock.connect_ex((host, port)) == 0:
                    sock.close()
                    return (time.time() - start) * 1000
            except Exception:
                pass
        sock.close()
        return None
    except Exception:
        return None

def ping_multi(host, count=3, timeout=2):
    rtts = []
    for _ in range(count):
        rtt = ping_once(host, timeout)
        if rtt is not None:
            rtts.append(rtt)
        time.sleep(0.5)
    loss = (count - len(rtts)) / count * 100
    avg_rtt = sum(rtts) / len(rtts) if rtts else None
    return bool(rtts), avg_rtt, loss

def measure_dns(host="www.baidu.com"):
    try:
        start = time.time()
        socket.gethostbyname(host)
        return (time.time() - start) * 1000
    except Exception:
        return None

def get_gateway():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        parts = local_ip.rsplit(".", 1)
        return parts[0] + ".1"
    except Exception:
        return "192.168.1.1"

def probe_target(target):
    rtt = ping_once(target["host"])
    return (rtt is not None, rtt)

def run_probe():
    gateway = get_gateway()
    gw_ok, gw_rtt, gw_loss = ping_multi(gateway)
    dns_ms = measure_dns()

    target_ok = False
    target_rtt = None
    target_name = ""
    for t in TARGETS:
        ok, rtt = probe_target(t)
        if ok:
            target_ok = True
            target_rtt = rtt
            target_name = t["name"]
            break

    return {
        "ping_ok": gw_ok,
        "ping_rtt_ms": gw_rtt,
        "ping_loss_pct": gw_loss,
        "dns_ms": dns_ms,
        "gateway_reachable": gw_ok,
        "target_reachable": target_ok,
        "target_name": target_name,
        "target_rtt_ms": target_rtt,
    }

# ─── 上报 ──────────────────────────────────────────────────────────────────

def register():
    try:
        data = json.dumps({"name": socket.gethostname()}).encode()
        req = urllib.request.Request(
            f"{SERVER_URL}/api/register", data=data,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            new_id = result.get("agent_id")
            if new_id:
                with open(ID_FILE, "w") as f:
                    f.write(new_id)
                return new_id
    except Exception as e:
        log.warning("注册失败: %s", e)
    return AGENT_ID

def report(data):
    try:
        url = f"{SERVER_URL}/api/{AGENT_ID}/report"
        req = urllib.request.Request(
            url, data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.warning("上报失败: %s", e)
        return None

# ─── 托盘图标（pystray） ──────────────────────────────────────────────────

def build_tray():
    try:
        from pystray import Icon, MenuItem, Menu
        from PIL import Image, ImageDraw
    except ImportError:
        log.warning("pystray 未安装，托盘功能不可用")
        return None

    # 生成一个简单的绿色图标
    img = Image.new("RGB", (64, 64), color=(52, 199, 89))
    draw = ImageDraw.Draw(img)
    draw.ellipse([8, 8, 56, 56], fill=(255, 255, 255))
    draw.ellipse([20, 20, 44, 44], fill=(52, 199, 89))

    def on_show(icon=None, item=None):
        icon.visible = False
        # 窗口恢复（如果支持）
        log.info("托盘图标被点击")

    def on_quit(icon=None, item=None):
        log.info("Agent 退出")
        if icon:
            icon.stop()
        sys.exit(0)

    menu = Menu(
        MenuItem("显示", on_show),
        MenuItem("退出", on_quit),
    )

    icon = Icon("网络监控", img, "网络监控 Agent", menu)
    return icon

# ─── 主循环 ─────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 40)
    log.info("网络监控 Agent 启动，ID: %s", AGENT_ID)
    log.info("服务端: %s", SERVER_URL)
    log.info("探测间隔: %d 秒", REPORT_INTERVAL)
    log.info("=" * 40)

    # 托盘
    tray = build_tray()
    tray_enabled = (tray is not None)

    if tray_enabled:
        import threading
        tray_thread = threading.Thread(target=tray.run, daemon=True)
        tray_thread.start()
        log.info("托盘图标已启动")
    else:
        log.info("无托盘模式，运行于前台")

    # 注册
    if not os.path.exists(ID_FILE):
        new_id = register()
        if new_id != AGENT_ID:
            AGENT_ID = new_id
            log.info("注册成功，ID: %s", AGENT_ID)

    log.info("开始探测...")
    consecutive_errors = 0

    while True:
        try:
            data = run_probe()
            result = report(data)
            consecutive_errors = 0 if result else consecutive_errors + 1

            if result and result.get("ok"):
                status = "OK" if data["ping_ok"] else "FAIL"
                log.info("[%s] 网关:%sms DNS:%sms 目标:%s 可达:%s",
                    status,
                    f"{data['ping_rtt_ms']:.1f}" if data['ping_rtt_ms'] else "-",
                    f"{data['dns_ms']:.1f}" if data['dns_ms'] else "-",
                    data['target_name'] or "N/A",
                    data['target_reachable'])
            else:
                log.warning("上报失败 (连续失败 %d 次)", consecutive_errors)

        except KeyboardInterrupt:
            log.info("收到停止信号")
            break
        except Exception as e:
            log.error("运行异常: %s", e)
            consecutive_errors += 1

        time.sleep(REPORT_INTERVAL)

    log.info("Agent 已停止")

if __name__ == "__main__":
    # Windows 上隐藏控制台窗口
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)
    main()
