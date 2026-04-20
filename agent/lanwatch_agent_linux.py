#!/usr/bin/env python3
"""
lanwatch_agent - 企业网络监控客户端（Linux / Ubuntu 专用版）v0.5
支持 Ubuntu / Debian 系统

依赖安装：
  pip install pystray pillow pyyaml

运行方式：
  python3 lanwatch_agent_linux.py

Linux 打包：
  pyinstaller --onefile --name lanwatch_agent lanwatch_agent_linux.py
"""
__version__ = "0.5.0"

import os
import sys
import json
import time
import socket
import uuid
import logging
import subprocess
import urllib.request
import urllib.error
import threading
import queue
import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed

# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════
SERVER_URL = "http://82.156.229.67:8000"
REPORT_INTERVAL = 60
TOPOLOGY_INTERVAL = 300       # 5 分钟扫一次拓扑

# Linux 路径规范（XDG）
HOME = os.path.expanduser("~")
CONFIG_DIR = os.path.join(HOME, ".config", "lanwatch")
AUTOSTART_DIR = os.path.join(HOME, ".config", "autostart")
LOG_FILE = os.path.join(CONFIG_DIR, "agent.log")
CONFIG_FILE = os.path.join(CONFIG_DIR, "agent.json")
DESKTOP_FILE = os.path.join(AUTOSTART_DIR, "lanwatch-agent.desktop")

# ═══════════════════════════════════════════════════════════════
# 日志
# ═══════════════════════════════════════════════════════════════
os.makedirs(CONFIG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger()

# ═══════════════════════════════════════════════════════════════
# 全局状态
# ═══════════════════════════════════════════════════════════════
_status_queue = queue.Queue()
_tray_icon_ref = None
_executor = None        # 拓扑扫描线程池

# ═══════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════

def get_local_ip():
    """获取本机局域网 IP"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def get_subnet_prefix():
    """获取本机所在网段前缀，如 192.168.1"""
    ip = get_local_ip()
    parts = ip.rsplit(".", 1)
    return parts[0] + "." + parts[1] if len(parts) == 2 else "192.168.1"

def get_gateway():
    """读取 /proc/net/route 获取网关 IP"""
    try:
        with open("/proc/net/route") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 3 and parts[1] == "00000000":
                    gw_hex = parts[2]
                    if len(gw_hex) == 8:
                        ip = ".".join([
                            str(int(gw_hex[6:8], 16)),
                            str(int(gw_hex[4:6], 16)),
                            str(int(gw_hex[2:4], 16)),
                            str(int(gw_hex[0:2], 16)),
                        ])
                        return ip
    except Exception:
        pass
    # fallback
    subnet = get_subnet_prefix()
    return subnet + ".1"

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None

def save_config(cfg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error("保存配置失败: %s", e)

# ═══════════════════════════════════════════════════════════════
# 网络探测
# ═══════════════════════════════════════════════════════════════

def ping_once(host, timeout=3):
    """ping 一次 host，返回 (成功, 延迟ms)"""
    try:
        r = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), host],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        if r.returncode == 0:
            try:
                out = subprocess.check_output(
                    ["ping", "-c", "1", "-W", str(timeout), host],
                    stderr=subprocess.DEVNULL, text=True
                )
                m = re.search(r"time[=<](\d+\.?\d*)", out)
                rtt = float(m.group(1)) if m else None
                return True, rtt
            except Exception:
                return True, None
        return False, None
    except Exception:
        return False, None

def ping_multi(host, count=3, timeout=2):
    """ping 多次 host，返回 (成功率, 平均延迟ms, 丢包率)"""
    ok = 0
    rtts = []
    for _ in range(count):
        success, rtt = ping_once(host, timeout)
        if success:
            ok += 1
            if rtt is not None:
                rtts.append(rtt)
        time.sleep(0.2)
    loss = (count - ok) / count * 100
    avg_rtt = sum(rtts) / len(rtts) if rtts else None
    return ok > 0, avg_rtt, loss

def measure_dns(host="www.baidu.com"):
    """测 DNS 延迟（用 ping 近似）"""
    _, rtt, _ = ping_multi(host, count=2, timeout=3)
    return rtt

def get_mac_for_ip(ip):
    """通过 ARP 表查 IP 对应的 MAC"""
    try:
        subprocess.run(["ping", "-c", "1", "-W", "1", ip],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.5)
        with open("/proc/net/arp") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 4 and parts[0] == ip and parts[3] != "00:00:00:00:00:00":
                    return parts[3].upper()
    except Exception:
        pass
    return None

def get_local_mac():
    """获取本机 MAC（默认网卡）"""
    for iface in ["eth0", "ens0", "en0", "wl0"]:
        path = f"/sys/class/net/{iface}/address"
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return f.read().strip().upper()
            except Exception:
                pass
    try:
        out = subprocess.check_output(["ip", "link", "show"],
                                       stderr=subprocess.DEVNULL, text=True)
        m = re.search(r"link/ether ([0-9a-f:]+)", out, re.I)
        if m:
            return m.group(1).upper()
    except Exception:
        pass
    return "00:00:00:00:00:00"

OUI_VENDOR = {
    "00:50:56": "VMware",   "00:0C:29": "VMware",   "00:1C:14": "VMware",
    "00:05:69": "VMware",
    "00:14:6C": "NetGear", "00:50:BA": "NetGear",
    "00:1B:2B": "HP",      "00:1F:29": "HP",       "00:21:5A": "HP",
    "00:22:64": "Dell",    "00:06:5B": "Dell",
    "00:1C:B3": "Apple",   "00:1D:4F": "Apple",    "00:1E:C9": "Apple",
    "00:1E:52": "Cisco",   "00:1A:2B": "Cisco",    "00:25:84": "Cisco",
    "00:04:4B": "Nvidia",
    "00:1A:11": "Google",   "00:03:93": "Google",
    "00:60:2F": "Cisco",   "00:16:3E": "Xen",
    "B8:27:EB": "Raspberry", "DC:A6:32": "Raspberry",
    "00:1A:2B": "Huawei",   "00:25:9E": "Huawei",
    "00:1E:58": "D-Link",  "00:26:5A": "D-Link",
    "00:1F:3C": "TP-Link", "00:27:19": "TP-Link",
    "20:F3:A3": "TP-Link",
    "14:CC:20": "TP-Link", "14:CF:E2": "TP-Link",
    "AC:84:C6": "TP-Link",
    "88:C3:97": "H3C",     "00:25:65": "H3C",
    "00:1A:A9": "ZTE",     "00:1B:FC": "ZTE",
    "00:19:C6": "ZTE",
    "9C:5C:8E": "Intel",   "00:1B:21": "Intel",
    "3C:97:0E": "Intel",
    "00:0E:35": "Intel",
    "94:DE:80": "Intel",
    "00:24:D7": "Intel",
    "00:18:DE": "Cisco",
    "F0:9F:C2": "Huawei",
}

def get_vendor(mac):
    if not mac or len(mac) < 8:
        return ""
    prefix = mac[:8].upper()
    return OUI_VENDOR.get(prefix, "")

def guess_device_type(hostname="", vendor="", mac=""):
    """根据 hostname / vendor / mac 推断设备类型"""
    h = hostname.lower()
    v = vendor.lower()
    if not hostname and not vendor:
        return "unknown"
    if any(k in h for k in ["router", "gateway", "ap", "tplink", "mercury", "tenda", "fast", "192.168"]):
        return "router"
    if any(k in h for k in [" printer", "laser", "inkjet"]):
        return "printer"
    if any(k in h for k in ["server", "nas", "synology", "qnap", "freenas"]):
        return "server"
    if any(k in v for k in ["cisco", "h3c", "juniper", "arista", " Aruba"]):
        return "switch"
    if any(k in h for k in ["iphone", "android", "mobile", "phone"]):
        return "phone"
    if any(k in h for k in ["macbook", "imac", "desktop", "pc", "mini"]):
        return "pc"
    if "raspberry" in v or "raspberry" in h:
        return "pc"
    if v in ["apple", "apple, inc.", "apple inc"]:
        return "pc"
    return "unknown"

def _probe_host(ip):
    """探测单个 IP：ping + ARP 获取 MAC"""
    ok, rtt, _ = ping_multi(ip, count=1, timeout=2)
    mac = get_mac_for_ip(ip) if ok else None
    vendor = get_vendor(mac) if mac else ""
    hostname = ""
    try:
        hostname = socket.getfqdn(ip)
        if hostname == ip:
            hostname = ""
    except Exception:
        hostname = ""
    return {
        "ip": ip,
        "mac": mac or "",
        "hostname": hostname,
        "vendor": vendor,
        "device_type": guess_device_type(hostname, vendor, mac),
    }

def scan_topology(subnets=None):
    """多线程并发扫描局域网（ARP + ping）"""
    if subnets is None:
        prefix = get_subnet_prefix()
        subnets = [prefix]
    if not subnets:
        return []

    targets = []
    for sp in subnets:
        for i in range(1, 255):
            targets.append(f"{sp}.{i}")

    log.info("[拓扑] 开始扫描 %s 个网段，共 %d 个 IP...", subnets, len(targets))
    results = []
    with ThreadPoolExecutor(max_workers=30) as executor:
        futures = {executor.submit(_probe_host, ip): ip for ip in targets}
        done = 0
        for future in as_completed(futures):
            done += 1
            if done % 50 == 0:
                log.info("[拓扑] 扫描进度 %d/%d", done, len(targets))
            try:
                dev = future.result(timeout=5)
                if dev["mac"]:
                    results.append(dev)
            except Exception:
                pass

    log.info("[拓扑] 发现 %d 台有 MAC 的设备", len(results))
    return results

# ═══════════════════════════════════════════════════════════════
# 服务端通信
# ═══════════════════════════════════════════════════════════════

def register_agent(company_name, location=""):
    try:
        data = json.dumps({
            "name": company_name,
            "customer_name": company_name,
            "location": location,
            "remark": "linux-agent"
        }).encode("utf-8")
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
            data=json.dumps(data).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.warning("上报失败: %s", e)
        return None

def report_offline(agent_id):
    try:
        req = urllib.request.Request(
            SERVER_URL + "/api/" + agent_id + "/offline",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.warning("离线通知失败: %s", e)
        return None

def report_uninstall(agent_id):
    """通知服务端该设备已卸载"""
    try:
        req = urllib.request.Request(
            SERVER_URL + "/api/" + agent_id + "/uninstall",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            log.info("[卸载] 通知服务端成功: %s", result)
            return result
    except Exception as e:
        log.warning("[卸载] 通知服务端失败: %s", e)
        return None

def report_topology(devices, agent_id):
    try:
        data = json.dumps({
            "devices": devices,
            "agent_id": agent_id,
        }).encode("utf-8")
        req = urllib.request.Request(
            SERVER_URL + "/api/" + agent_id + "/topology",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.warning("拓扑上报失败: %s", e)
        return None

def get_targets():
    """从配置文件读取监控目标列表"""
    cfg = load_config()
    targets = []
    if cfg and "targets" in cfg:
        return cfg["targets"]
    # 默认目标
    gw = get_gateway()
    return [
        {"name": "网关", "host": gw},
        {"name": "DNS", "host": "8.8.8.8"},
    ]

def run_probe(subnets=None):
    targets = get_targets()
    gateway = get_gateway()
    gw_ok, gw_rtt, gw_loss = ping_multi(gateway)
    dns_ms = measure_dns()

    target_ok = False
    target_rtt = None
    target_name = ""
    for t in targets:
        ok, rtt = ping_once(t["host"], timeout=3), None
        if ok:
            target_ok = True
            # 重新取延迟
            _, rtt, _ = ping_multi(t["host"], count=2, timeout=3)
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
        "subnets": ",".join(subnets) if subnets else "",
    }

# ═══════════════════════════════════════════════════════════════
# Linux 托盘图标（pystray + PIL）
# ═══════════════════════════════════════════════════════════════

_tray_icon_ref = None
_about_window_ref = None
_settings_window_ref = None
_tk_queue = None

def _init_tk_queue():
    """初始化 Tk 任务队列，Tk 窗口在独立 daemon 线程的事件循环中创建"""
    global _tk_queue
    import queue
    import threading

    def _gui_thread():
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        root.attributes("-alpha", 0)
        root.protocol("WM_DELETE_WINDOW", lambda: None)
        root.mainloop()

    _tk_queue = queue.Queue()
    t = threading.Thread(target=_gui_thread, daemon=True)
    t.start()

def _create_tray_image(color_hex="#34c759"):
    """用 PIL 生成托盘图标（16x16 圆点）"""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (16, 16), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([2, 2, 13, 13], fill=color_hex)
    return img

def setup_tray(agent_id, company_name):
    """创建并启动系统托盘图标"""
    global _tray_icon_ref

    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        log.warning("[托盘] pystray 或 pillow 未安装，托盘功能不可用")
        return None

    def _on_click(icon, item):
        if str(item) == "关于":
            _show_about_window()
        elif str(item) == "设置":
            _show_settings_window()
        elif str(item) == "查看日志":
            _open_log()
        elif str(item) == "退出网络守护":
            _exit_app(icon)

    menu = pystray.Menu(
        pystray.MenuItem(f"企业：{company_name}", None, enabled=False),
        pystray.MenuItem(f"ID：{agent_id[:8]}...", None, enabled=False),
        pystray.MenuItem("───", None, enabled=False),
        pystray.MenuItem("设置", lambda _, item: _on_click(None, item)),
        pystray.MenuItem("查看日志", lambda _, item: _on_click(None, item)),
        pystray.MenuItem("───", None, enabled=False),
        pystray.MenuItem("关于", lambda _, item: _on_click(None, item)),
        pystray.MenuItem("退出网络守护", lambda _, item: _on_click(None, item)),
    )

    icon = pystray.Icon(
        "lanwatch_agent",
        _create_tray_image("#34c759"),
        "企业网络监控",
        menu=menu
    )
    _tray_icon_ref = icon

    t = threading.Thread(target=icon.run, daemon=True)
    t.start()
    log.info("[托盘] 系统托盘已启动（Linux）")
    return icon

def update_tray_status(is_online):
    global _tray_icon_ref
    if _tray_icon_ref is None:
        return

    def _do_update():
        try:
            color = "#34c759" if is_online else "#ff3b30"
            _tray_icon_ref.icon = _create_tray_image(color)
            _tray_icon_ref.title = "企业网络监控 - " + ("正常" if is_online else "离线")
        except Exception:
            pass

    try:
        _do_update()
    except Exception:
        pass

def _open_log():
    """用 xdg-open 打开日志文件"""
    def _do():
        try:
            subprocess.run(["xdg-open", LOG_FILE],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            log.warning("[托盘] 无法打开日志文件")
    t = threading.Thread(target=_do, daemon=True)
    t.start()

def _show_about_window():
    global _about_window_ref
    if _about_window_ref is not None:
        try:
            _about_window_ref.focus_force()
        except Exception:
            _about_window_ref = None
        return

    def _do_show():
        global _about_window_ref
        import tkinter as tk
        import webbrowser

        win = tk.Toplevel()
        _about_window_ref = win
        win.title("关于")
        win.geometry("360x200")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        win.update_idletasks()
        cx = (win.winfo_screenwidth() - 360) // 2
        cy = (win.winfo_screenheight() - 200) // 2
        win.geometry(f"360x200+{cx}+{cy}")

        tk.Label(win, text="企业网络监控", font=("Arial", 16, "bold")).pack(pady=16)
        tk.Label(win, text=f"版本：v{__version__}", font=("Arial", 11)).pack(pady=4)
        tk.Label(win, text="下载地址：", font=("Arial", 10), fg="#666").pack(pady=(12, 2))
        link = tk.Label(win, text="http://www.lanwatch.net/download",
                        font=("Arial", 10), fg="#1a73e8", cursor="hand2")
        link.pack()
        link.bind("<Button-1>", lambda _: webbrowser.open("http://www.lanwatch.net/download"))

        def on_close():
            global _about_window_ref
            _about_window_ref = None
            win.destroy()

        tk.Button(win, text="确定", command=on_close,
                  font=("Arial", 10), width=10).pack(pady=12)
        win.protocol("WM_DELETE_WINDOW", on_close)
        win.transient()
        win.focus_force()
        win.grab_set()

    if _tk_queue:
        _tk_queue.put(_do_show)

def _show_settings_window():
    global _settings_window_ref
    if _settings_window_ref is not None:
        try:
            _settings_window_ref.focus_force()
        except Exception:
            _settings_window_ref = None
        return

    def _do_show():
        global _settings_window_ref
        import tkinter as tk
        from tkinter import messagebox

        win = tk.Toplevel()
        _settings_window_ref = win
        win.title("设置")
        win.geometry("360x240")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        win.update_idletasks()
        cx = (win.winfo_screenwidth() - 360) // 2
        cy = (win.winfo_screenheight() - 240) // 2
        win.geometry(f"360x240+{cx}+{cy}")

        tk.Label(win, text="企业网络监控", font=("Arial", 14, "bold")).pack(pady=12)

        autostart_var = tk.BooleanVar(value=is_autostart_enabled())

        def on_autostart_changed():
            enabled = autostart_var.get()
            ok = set_autostart(enabled)
            if ok:
                log.info("[设置] 开机自启已%s", "开启" if enabled else "关闭")
            else:
                log.warning("[设置] 开机自启设置失败")
                autostart_var.set(not enabled)

        tk.Checkbutton(
            win, text="开机自动启动", variable=autostart_var,
            font=("Arial", 11), command=on_autostart_changed,
            indicatoron=True
        ).pack(anchor="w", padx=40, pady=8)

        tk.Frame(win, height=1, bg="#e5e5ea").pack(fill="x", padx=20, pady=8)

        def on_uninstall():
            if not messagebox.askyesno("卸载确认",
                    "确定要卸载网络守护吗？\n\n将删除所有配置并停止监控。"):
                return
            log.info("[卸载] 开始卸载...")
            # 通知服务端该设备已卸载
            cfg = load_config()
            if cfg and cfg.get("agent_id"):
                report_uninstall(cfg["agent_id"])
            try:
                if os.path.exists(CONFIG_FILE):
                    os.remove(CONFIG_FILE)
                    log.info("[卸载] 配置已删除")
            except Exception as e:
                log.error("[卸载] 删除配置失败: %s", e)
            set_autostart(False)
            win.destroy()
            time.sleep(1)
            os._exit(0)

        def on_close():
            global _settings_window_ref
            _settings_window_ref = None
            win.destroy()

        tk.Button(win, text="卸载网络守护", command=on_uninstall,
                  font=("Arial", 11), fg="#ff3b30", relief="groove",
                  width=20, height=1).pack(pady=12)

        tk.Button(win, text="关闭", command=on_close,
                  font=("Arial", 10), width=10).pack(pady=8)

        win.protocol("WM_DELETE_WINDOW", on_close)
        win.transient()
        win.focus_force()
        win.grab_set()

    if _tk_queue:
        _tk_queue.put(_do_show)

def _exit_app(icon=None):
    """退出程序"""
    log.info("[托盘] 用户请求退出，正在关闭...")
    if icon:
        try:
            icon.stop()
        except Exception:
            pass
    os._exit(0)

# ═══════════════════════════════════════════════════════════════
# Linux 开机自启（XDG autostart .desktop 文件）
# ═══════════════════════════════════════════════════════════════

def set_autostart(enable=True):
    """写入/删除 ~/.config/autostart/lanwatch-agent.desktop，实现开机自启"""
    try:
        os.makedirs(AUTOSTART_DIR, exist_ok=True)
        if enable:
            exe_path = sys.executable
            script_path = os.path.abspath(__file__)
            cmd = f'"{exe_path}" "{script_path}"'
            desktop_content = (
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Name=lanwatch-agent\n"
                "Comment=企业网络监控客户端\n"
                f"Exec={cmd}\n"
                "Hidden=false\n"
                "NoDisplay=true\n"
                "X-GNOME-Autostart-enabled=true\n"
            )
            with open(DESKTOP_FILE, "w", encoding="utf-8") as f:
                f.write(desktop_content)
            log.info("[自启] 已开启开机自启（%s）", DESKTOP_FILE)
        else:
            if os.path.exists(DESKTOP_FILE):
                os.remove(DESKTOP_FILE)
                log.info("[自启] 已关闭开机自启")
        return True
    except Exception as e:
        log.warning("[自启] 设置失败: %s", e)
        return False

def is_autostart_enabled():
    """检查 .desktop 文件是否存在"""
    return os.path.exists(DESKTOP_FILE)

# ═══════════════════════════════════════════════════════════════
# 首次运行设置向导
# ═══════════════════════════════════════════════════════════════

def _show_setup_window(company_name=""):
    import tkinter as tk
    from tkinter import messagebox, scrolledtext

    result = {
        "company_name": "", "ready": False, "autostart": True,
        "subnets": [], "targets": []
    }

    local_ip = get_local_ip()
    subnet_prefix = get_subnet_prefix()
    initial_subnets = [subnet_prefix] if subnet_prefix else []
    initial_targets = [
        {"name": "网关", "host": get_gateway()},
        {"name": "DNS", "host": "8.8.8.8"},
    ]

    # ── 扫描 ──────────────────────────────────────────────────
    def do_scan():
        scan_btn.config(state="disabled", text="扫描中...")
        result_text.delete("1.0", tk.END)
        subs = list(subnet_listbox.get(0, tk.END))
        if not subs:
            result_text.insert("1.0", "请先添加至少一个网段\n")
            scan_btn.config(state="normal", text="扫描局域网")
            return
        total = 0
        for sp in subs:
            result_text.insert(tk.END, f"[{sp}.0/24] 扫描中...\n")
            result_text.update()
            devs = scan_topology([sp])
            for ip, mac in [(d["ip"], d["mac"]) for d in devs]:
                vendor = get_vendor(mac)
                suf = "- " + vendor if vendor else (mac or "-")
                result_text.insert(tk.END, f"  {ip}  {suf}\n")
                result_text.update()
            total += len(devs)
            result_text.insert(tk.END, f"  -> 发现 {len(devs)} 台\n\n")
            result_text.update()
        result_text.insert(tk.END, f"[OK] 共扫描 {len(subs)} 个网段，合计 {total} 台设备")
        scan_btn.config(state="normal", text="扫描局域网")

    def add_subnet():
        s = subnet_entry.get().strip()
        if s and s not in list(subnet_listbox.get(0, tk.END)):
            subnet_listbox.insert(tk.END, s)
        subnet_entry.delete(0, tk.END)

    def remove_subnet():
        for i in reversed(subnet_listbox.curselection()):
            subnet_listbox.delete(i)

    def add_target():
        n = target_name_entry.get().strip()
        h = target_host_entry.get().strip()
        if n and h:
            targets_listbox.insert(tk.END, n + " -> " + h)
            target_name_entry.delete(0, tk.END)
            target_host_entry.delete(0, tk.END)

    def remove_target():
        for i in reversed(targets_listbox.curselection()):
            targets_listbox.delete(i)

    def on_submit():
        name = entry.get().strip()
        if not name:
            messagebox.showwarning("提示", "请填写企业名称")
            return
        subnets = list(subnet_listbox.get(0, tk.END))
        if not subnets:
            messagebox.showwarning("提示", "请添加至少一个监控网段")
            return
        targets = []
        for item in targets_listbox.get(0, tk.END):
            parts = item.split(" -> ")
            if len(parts) == 2:
                targets.append({"name": parts[0].strip(), "host": parts[1].strip()})
        if not targets:
            messagebox.showwarning("提示", "请添加至少一个监控目标")
            return
        result["company_name"] = name
        result["ready"] = True
        result["autostart"] = autostart_var.get()
        result["subnets"] = subnets
        result["targets"] = targets
        win.destroy()

    # ── 窗口 ──────────────────────────────────────────────────
    win = tk.Tk()
    win.title("网络监控 - 首次设置")
    win.geometry("560x680")
    win.resizable(False, False)
    win.attributes("-topmost", True)
    win.update_idletasks()
    cx = (win.winfo_screenwidth() - 560) // 2
    cy = (win.winfo_screenheight() - 680) // 2
    win.geometry(f"560x680+{cx}+{cy}")

    tk.Label(win, text="企业网络监控", font=("Arial", 16, "bold")).pack(pady=8)
    tk.Label(win, text=f"本机IP：{local_ip}", font=("Arial", 9), fg="#888").pack()

    # 基本信息
    basic = tk.Frame(win)
    basic.pack(fill="x", padx=20, pady=6)
    tk.Label(basic, text="企业名称：", font=("Arial", 11)).grid(row=0, column=0, sticky="w", pady=6)
    entry = tk.Entry(basic, font=("Arial", 11))
    entry.grid(row=0, column=1, sticky="ew", pady=6, padx=4)
    entry.insert(0, company_name)
    entry.focus()
    basic.columnconfigure(1, weight=1)
    autostart_var = tk.BooleanVar(value=True)
    tk.Checkbutton(basic, text="开机自动启动", variable=autostart_var,
                   font=("Arial", 10)).grid(row=1, column=0, columnspan=2, sticky="w", pady=2)

    # 网段管理
    tk.Label(win, text="监控网段", font=("Arial", 12, "bold")).pack(anchor="w", padx=20, pady=(10, 2))
    tk.Label(win, text="可扫描多个网段，如企业有多个分支机构",
             font=("Arial", 9), fg="#888", anchor="w").pack(anchor="w", padx=20)

    subnet_f = tk.Frame(win)
    subnet_f.pack(fill="x", padx=20, pady=4)
    subnet_entry = tk.Entry(subnet_f, font=("Arial", 10))
    subnet_entry.pack(side="left", fill="x", expand=True, padx=(0, 4))
    subnet_entry.insert(0, subnet_prefix)
    tk.Button(subnet_f, text="添加", command=add_subnet, font=("Arial", 9)).pack(side="left")
    tk.Button(subnet_f, text="删除", command=remove_subnet, font=("Arial", 9)).pack(side="left", padx=2)

    subnet_listbox = tk.Listbox(win, font=("Arial", 10), height=3)
    subnet_listbox.pack(fill="x", padx=20)
    for s in initial_subnets:
        subnet_listbox.insert(tk.END, s)

    # 目标管理
    tk.Label(win, text="监控目标", font=("Arial", 12, "bold")).pack(anchor="w", padx=20, pady=(8, 2))
    tk.Label(win, text="可同时监控多个目标（网关、DNS、ERP服务器等）",
             font=("Arial", 9), fg="#888", anchor="w").pack(anchor="w", padx=20)

    tgt_f = tk.Frame(win)
    tgt_f.pack(fill="x", padx=20, pady=4)
    target_name_entry = tk.Entry(tgt_f, font=("Arial", 9), width=10)
    target_name_entry.pack(side="left", padx=(0, 2))
    tk.Label(tgt_f, text="名称", font=("Arial", 9)).pack(side="left", padx=(0, 4))
    target_host_entry = tk.Entry(tgt_f, font=("Arial", 9))
    target_host_entry.pack(side="left", fill="x", expand=True, padx=(0, 4))
    tk.Label(tgt_f, text="IP/域名", font=("Arial", 9)).pack(side="left", padx=(0, 4))
    tk.Button(tgt_f, text="添加", command=add_target, font=("Arial", 9)).pack(side="left")
    tk.Button(tgt_f, text="删除", command=remove_target, font=("Arial", 9)).pack(side="left", padx=2)

    targets_listbox = tk.Listbox(win, font=("Arial", 10), height=3)
    targets_listbox.pack(fill="x", padx=20)
    for t in initial_targets:
        targets_listbox.insert(tk.END, t["name"] + " -> " + t["host"])

    # 扫描
    scan_btn = tk.Button(win, text="扫描局域网", command=do_scan,
                         font=("Arial", 10), bg="#f0f0f5")
    scan_btn.pack(pady=6)
    result_text = scrolledtext.ScrolledText(win, font=("Arial", 9), height=8, wrap=tk.WORD)
    result_text.pack(fill="both", padx=20, pady=4)

    # 提交
    tk.Button(win, text="完成配置", command=on_submit,
              font=("Arial", 11), bg="#007aff", fg="white",
              width=15, height=1).pack(pady=10)

    win.protocol("WM_DELETE_WINDOW", lambda: None)
    win.mainloop()
    return result["company_name"], result["autostart"], \
           ",".join(result["subnets"]), result["subnets"], result["targets"]

# ═══════════════════════════════════════════════════════════════
# 主循环
# ═══════════════════════════════════════════════════════════════

def main():
    global _tray_icon_ref

    log.info("=" * 40)
    log.info("lanwatch_agent Linux 启动 v%s", __version__)
    log.info("服务端: %s", SERVER_URL)
    log.info("=" * 40)

    # 初始化 Tk 任务队列（供托盘回调跨线程创建窗口）
    _init_tk_queue()

    config = load_config()

    # ── 首次注册 ──
    if not config or not config.get("agent_id"):
        log.info("首次运行，显示设置向导...")
        company_name, autostart, location, subnets, targets = _show_setup_window(
            company_name=config.get("company_name") if config else ""
        )
        if not company_name:
            log.info("用户取消设置，退出")
            return

        log.info("正在注册企业: %s", company_name)
        log.info("监控网段: %s", subnets)
        log.info("监控目标: %s", targets)
        agent_id = register_agent(company_name, location)
        if not agent_id:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("错误", "注册失败，请检查网络连接后重试。")
            root.destroy()
            log.error("注册失败，退出")
            return

        log.info("注册成功，Agent ID: %s", agent_id)

        config = {
            "agent_id": agent_id,
            "company_name": company_name,
            "subnets": subnets,
            "targets": targets,
        }
        save_config(config)

        # 开机自启
        if autostart:
            set_autostart(True)

        agent_id = config["agent_id"]
        company_name = config["company_name"]
    else:
        agent_id = config["agent_id"]
        company_name = config.get("company_name", "")
        log.info("已配置 Agent ID: %s", agent_id)

    # 确保 subnets 在循环作用域内
    subnets = config.get("subnets", [])

    # ── 启动托盘 ──
    tray_icon = setup_tray(agent_id, company_name)

    log.info("开始探测...")
    consecutive_errors = 0
    topo_counter = 0
    TOPO_EVERY_N = max(1, TOPOLOGY_INTERVAL // REPORT_INTERVAL)
    last_status_online = True

    while True:
        try:
            data = run_probe(subnets)
            result = report(data, agent_id)
            consecutive_errors = 0 if result else consecutive_errors + 1

            is_online = data["ping_ok"]
            update_tray_status(is_online)

            if result and result.get("ok"):
                status = "OK" if is_online else "FAIL"
                log.info("[%s] 网关:%sms DNS:%sms 目标:%s 可达:%s",
                    status,
                    f"{data['ping_rtt_ms']:.1f}" if data["ping_rtt_ms"] else "-",
                    f"{data['dns_ms']:.1f}" if data["dns_ms"] else "-",
                    data["target_name"] or "N/A",
                    data["target_reachable"])
            else:
                log.warning("上报失败 (连续失败 %d 次)", consecutive_errors)
                update_tray_status(False)

            topo_counter += 1
            if topo_counter >= TOPO_EVERY_N:
                topo_counter = 0
                cfg = load_config()
                subs = (cfg or {}).get("subnets", [])
                devices = scan_topology(subs) if subs else scan_topology()
                if devices:
                    report_topology(devices, agent_id)

        except KeyboardInterrupt:
            log.info("收到停止信号")
            break
        except Exception as e:
            log.error("运行异常: %s", e)
            consecutive_errors += 1
            update_tray_status(False)

        time.sleep(REPORT_INTERVAL)

    if tray_icon:
        try:
            tray_icon.stop()
        except Exception:
            pass
    log.info("Agent 已停止")

if __name__ == "__main__":
    main()
