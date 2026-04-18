#!/usr/bin/env python3
"""
企业网络监控 - Windows Agent (自注册版)
首次运行弹出设置向导，之后静默运行
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
import threading

# ─── 配置 ─────────────────────────────────────────────────────────────────
SERVER_URL = "http://82.156.229.67:8000"
REPORT_INTERVAL = 60
LOG_FILE = os.path.expanduser("~/.network_monitor_agent.log")
CONFIG_FILE = os.path.expanduser("~/.network_monitor_agent.json")

# ─── 日志 ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger()

# ─── 配置文件 ─────────────────────────────────────────────────────────────
def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return None

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

# ─── 探测函数 ─────────────────────────────────────────────────────────────

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

TARGETS = [
    {"name": "网关", "host": "192.168.1.1"},
    {"name": "DNS", "host": "8.8.8.8"},
]

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

# ─── 自注册 ───────────────────────────────────────────────────────────────

def register_agent(company_name):
    try:
        data = json.dumps({
            "name": socket.gethostname(),
            "customer_name": company_name,
            "location": "",
            "remark": "windows-agent"
        }).encode()
        req = urllib.request.Request(
            SERVER_URL + "/api/register",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            return result.get("agent_id")
    except Exception as e:
        log.error("注册失败: %s", e)
        return None

def report(data, agent_id):
    try:
        req = urllib.request.Request(
            SERVER_URL + "/api/" + agent_id + "/report",
            data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.warning("上报失败: %s", e)
        return None

# ─── 设置向导窗口 ─────────────────────────────────────────────────────────

def show_setup_window(agent_id=None, company_name=""):
    """弹出tkinter设置窗口"""
    try:
        import tkinter as tk
        from tkinter import messagebox
    except ImportError:
        log.error("tkinter 不可用，无法弹出设置窗口")
        return None

    result = {"agent_id": agent_id, "company_name": company_name, "ready": False}

    def on_submit():
        result["company_name"] = entry.get().strip()
        if not result["company_name"]:
            messagebox.showwarning("提示", "请填写企业名称")
            return
        result["ready"] = True
        win.destroy()

    win = tk.Tk()
    win.title("网络监控 - 首次设置")
    win.geometry("420x240")
    win.resizable(False, False)
    win.attributes("-topmost", True)

    # 居中
    win.update_idletasks()
    x = (win.winfo_screenwidth() - 420) // 2
    y = (win.winfo_screenheight() - 240) // 2
    win.geometry(f"420x240+{x}+{y}")

    tk.Label(win, text="企业网络监控", font=("Arial", 16, "bold")).pack(pady=16)
    tk.Label(win, text="首次使用，请填写以下信息：", font=("Arial", 10)).pack(pady=4)

    frame = tk.Frame(win)
    frame.pack(pady=12, padx=24, fill="x")
    tk.Label(frame, text="企业名称：", font=("Arial", 11)).grid(row=0, column=0, sticky="w", pady=8)
    entry = tk.Entry(frame, font=("Arial", 11))
    entry.grid(row=0, column=1, sticky="ew", pady=8, padx=4)
    entry.insert(0, company_name)
    entry.focus()
    frame.columnconfigure(1, weight=1)

    btn_frame = tk.Frame(win)
    btn_frame.pack(pady=12)
    tk.Button(btn_frame, text="确定", font=("Arial", 11), bg="#007aff", fg="white",
              activebackground="#0064d0", width=12, command=on_submit).pack()

    def on_enter(e):
        on_submit()
    entry.bind("<Return>", on_enter)

    win.protocol("WM_DELETE_WINDOW", lambda: None)  # 禁止关闭
    win.mainloop()
    return result if result["ready"] else None

def show_message_window(title, msg):
    """弹出消息窗口（注册成功/失败）"""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        messagebox.showinfo(title, msg)
        root.destroy()
    except Exception:
        log.info("%s: %s", title, msg)

# ─── 主流程 ───────────────────────────────────────────────────────────────

def main():
    log.info("=" * 40)
    log.info("网络监控 Agent 启动")
    log.info("服务端: %s", SERVER_URL)
    log.info("=" * 40)

    # Windows 上隐藏控制台窗口
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)
        except Exception:
            pass

    # 读取配置
    config = load_config()

    if not config or not config.get("agent_id"):
        # 首次运行，弹出设置向导
        log.info("首次运行，显示设置向导...")
        setup_result = show_setup_window(
            agent_id=config.get("agent_id") if config else None,
            company_name=config.get("company_name") if config else ""
        )
        if not setup_result:
            log.info("用户取消设置，退出")
            return

        company_name = setup_result["company_name"]
        log.info("正在注册企业: %s", company_name)

        # 注册
        agent_id = register_agent(company_name)
        if not agent_id:
            show_message_window("注册失败", "无法连接到服务器，请检查网络后重新运行。")
            log.error("注册失败，退出")
            return

        log.info("注册成功，Agent ID: %s", agent_id)
        show_message_window("注册成功",
            f"企业：{company_name}\n设备ID：{agent_id}\n\n安装完成，Agent已开始运行。")

        # 保存配置
        config = {"agent_id": agent_id, "company_name": company_name}
        save_config(config)
        log.info("配置已保存")
    else:
        agent_id = config["agent_id"]
        log.info("已配置 Agent ID: %s", agent_id)

    log.info("开始探测...")
    consecutive_errors = 0

    while True:
        try:
            data = run_probe()
            result = report(data, agent_id)
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
    main()
