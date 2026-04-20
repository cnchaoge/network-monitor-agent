#!/usr/bin/env python3
"""
企业网络监控 - Windows Agent (自注册版) v0.4.0
- 首次运行弹出设置向导
- 注册后显示系统托盘图标
- 支持开机自启
- 右键托盘：查看日志 / 退出
"""
__version__ = "0.4.0"
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
import ctypes
import re

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

# ─── 托盘图标（纯文本像素数据，避免图片依赖）──────────────────────────────
def make_tray_icon():
    """生成一个简单的 tray 图标（绿色圆点 = 在线，红色圆点 = 离线）"""
    try:
        from pystray import Icon, MenuItem, Menu
        from PIL import Image, ImageDraw
        def create_image(color_hex="#34c759"):
            # 16x16 绿色/红色圆点图标
            img = Image.new("RGB", (16, 16), (0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.ellipse([2, 2, 13, 13], fill=color_hex)
            return img
        return create_image
    except ImportError:
        return None

# ─── 开机自启 ─────────────────────────────────────────────────────────────
def set_autostart(enable=True):
    """写入/删除 Windows 注册表 Run 键，实现开机自启"""
    try:
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS)
        if enable:
            exe_path = sys.executable
            script_path = os.path.abspath(__file__)
            # 用 pythonw.exe 运行，避免黑窗口
            cmd = f'"{exe_path}" "{script_path}"'
            winreg.SetValueEx(key, "NetworkMonitorAgent", 0, winreg.REG_SZ, cmd)
            log.info("[自启] 已开启开机自启")
        else:
            try:
                winreg.DeleteValue(key, "NetworkMonitorAgent")
                log.info("[自启] 已关闭开机自启")
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        return True
    except Exception as e:
        log.warning("[自启] 设置失败: %s", e)
        return False

def is_autostart_enabled():
    try:
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ)
        try:
            val, _ = winreg.QueryValueEx(key, "NetworkMonitorAgent")
            winreg.CloseKey(key)
            return bool(val)
        except FileNotFoundError:
            winreg.CloseKey(key)
            return False
    except Exception:
        return False

# ─── 配置文件 ─────────────────────────────────────────────────────────────
def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return None

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error("保存配置失败: %s", e)

# ─── 网段检测 ───────────────────────────────────────────────────────────
def get_local_subnet():
    """获取本机所在网段，如 192.168.1"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        parts = local_ip.rsplit(".", 1)
        subnet = parts[0] + "." + parts[1]
        return local_ip, subnet
    except Exception:
        return "", ""

def scan_subnet_devices(subnet_prefix, timeout=0.5):
    """快速扫描指定网段，返回 (IP, MAC) 列表"""
    results = []
    for i in range(1, 255):
        ip = f"{subnet_prefix}.{i}"
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(timeout)
            sock.connect((ip, 80))
            sock.close()
            mac = arp_lookup(ip)
            results.append((ip, mac))
        except Exception:
            pass
    return results

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

# ─── 默认监控目标（配置文件优先）──────────────────────────────────────
DEFAULT_TARGETS = [
    {"name": "网关", "host": "192.168.1.1"},
    {"name": "DNS", "host": "8.8.8.8"},
]

def probe_target(target):
    rtt = ping_once(target["host"])
    return (rtt is not None, rtt)

def get_targets():
    """从配置文件读取监控目标，无则用默认值"""
    cfg = load_config()
    if cfg and cfg.get("targets"):
        return cfg["targets"]
    return DEFAULT_TARGETS

def run_probe(subnets=None):
    targets = get_targets()
    gateway = get_gateway()
    gw_ok, gw_rtt, gw_loss = ping_multi(gateway)
    dns_ms = measure_dns()

    target_ok = False
    target_rtt = None
    target_name = ""
    for t in targets:
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
        "subnets": ",".join(subnets) if subnets else "",
    }

# ─── 自注册 ───────────────────────────────────────────────────────────────

def register_agent(company_name, location=""):
    try:
        data = json.dumps({
            "name": company_name,
            "customer_name": company_name,
            "location": location,
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

# ─── 局域网拓扑扫描 ────────────────────────────────────────────────────

OUI_VENDOR = {
    "00:50:56": "VMware",   "00:0C:29": "VMware",   "00:1C:14": "VMware",
    "00:05:69": "VMware",
    "00:14:6C": "NetGear", "00:50:BA": "NetGear",
    "00:1B:2B": "HP",      "00:1F:29": "HP",       "00:21:5A": "HP",
    "00:22:64": "Dell",    "00:06:5B": "Dell",
    "00:1C:B3": "Apple",   "00:1D:4F": "Apple",    "00:1E:C9": "Apple",
    "00:1E:52": "Cisco",   "00:1A:2B": "Cisco",    "00:25:84": "Cisco",
    "00:04:4B": "Nvidia",
    "00:1A:11": "Google",
    "00:50:F2": "Microsoft","00:0D:3A": "Microsoft","00:12:5A": "Microsoft",
    "00:15:5D": "Microsoft","00:17:FA": "Microsoft",
    "00:1A:6B": "TP-Link", "00:27:19": "TP-Link",  "14:CC:20": "TP-Link",
    "30:B5:C2": "TP-Link",
    "00:25:9E": "Cisco-Linksys","00:1A:70": "Cisco-Linksys",
    "00:1E:58": "D-Link",  "00:22:B0": "D-Link",   "00:26:5A": "D-Link",
    "1C:AF:F7": "D-Link",
    "00:24:B2": "ZTE",     "00:1B:3C": "ZTE",     "44:2A:60": "ZTE",
    "00:25:68": "Huawei",  "00:18:82": "Huawei",   "00:1E:10": "Huawei",
    "34:29:12": "Huawei",
    "20:CF:30": "Xiaomi", "34:80:B3": "Xiaomi",   "F8:A4:5F": "Xiaomi",
    "C8:D7:B0": "Xiaomi",
    "18:31:BF": "Huawei", "88:53:95": "Huawei",
    "08:00:27": "VirtualBox",
    "00:1C:42": "Parallels",
    "00:16:3E": "Xensource",
}

def get_vendor(mac):
    if not mac:
        return ""
    prefix = mac.upper().replace("-", ":")[:8]
    return OUI_VENDOR.get(prefix, "")

def guess_device_type(ip, hostname, vendor, mac):
    h = (hostname or "").lower()
    v = (vendor or "").lower()
    if any(k in h for k in ["router","gateway","tplink","netgear","tendawifi","mercury"]):
        return "router"
    if any(k in h for k in ["printer","print","hp","canon","brother","epson"]):
        return "printer"
    if any(k in h for k in ["server","nas","synology","qnap"]):
        return "server"
    if any(k in h for k in ["switch","sw"]):
        return "switch"
    if "cisco" in v: return "router"
    if "hp" in v: return "switch"
    if "dell" in v: return "server"
    if "vmware" in v or "virtualbox" in v: return "vm"
    if "apple" in v: return "phone"
    if "xiaomi" in v or "huawei" in v or "zte" in v: return "router"
    return "unknown"

def get_local_ip_and_mac():
    mac_addr = ""
    ip_addr = ""
    try:
        si = None
        if sys.platform == "win32":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = subprocess.SW_HIDE
        result = subprocess.run("getmac /v /fo csv", capture_output=True, text=True, timeout=5, startupinfo=si)
        for line in result.stdout.splitlines():
            if "Online" in line or "正在启用" in line:
                parts = line.split(",")
                for p in parts:
                    p = p.replace('"', '').strip()
                    if ":" in p and p.count(":") == 5:
                        mac_addr = p.upper()
                        break
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip_addr = s.getsockname()[0]
        finally:
            s.close()
    except Exception as e:
        log.warning("获取本机MAC失败: %s", e)
    return ip_addr, mac_addr

def get_gateway_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        parts = local_ip.rsplit(".", 1)
        return parts[0] + ".1"
    except Exception:
        return "192.168.1.1"

def ping_scan(subnet_prefix, timeout=0.5):
    alive = []
    for i in range(1, 255):
        ip = f"{subnet_prefix}.{i}"
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(timeout)
            sock.connect((ip, 80))
            sock.close()
            alive.append(ip)
        except Exception:
            pass
    return alive

def arp_lookup(ip):
    try:
        si = None
        if sys.platform == "win32":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = subprocess.SW_HIDE
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.3)
        try:
            sock.sendto(b"", (ip, 80))
        except Exception:
            pass
        sock.close()
        result = subprocess.run("arp -a", capture_output=True, text=True, timeout=5, startupinfo=si)
        for line in result.stdout.splitlines():
            if ip in line:
                for p in line.split():
                    if ":" in p and p.count(":") == 5:
                        return p.upper()
    except Exception:
        pass
    return ""

def get_hostname(ip):
    try:
        name, _, _ = socket.gethostbyaddr(ip)
        return name
    except Exception:
        return ""

def scan_topology(subnets=None):
    """扫描指定网段列表，返回所有发现的设备"""
    if subnets is None:
        cfg = load_config()
        local_ip, subnet_prefix = get_local_subnet()
        subnets = [subnet_prefix] if subnet_prefix else []

    log.info("[拓扑] 开始扫描 %d 个网段...", len(subnets))
    local_ip, local_mac = get_local_ip_and_mac()

    all_devices = []
    seen = set()

    for subnet_prefix in subnets:
        log.info("[拓扑] 扫描网段 %s.0/24...", subnet_prefix)
        alive_ips = ping_scan(subnet_prefix, timeout=0.5)
        log.info("[拓扑]   发现 %d 台存活主机", len(alive_ips))

        for ip in alive_ips:
            mac = arp_lookup(ip)
            hostname = get_hostname(ip)
            vendor = get_vendor(mac)
            dtype = guess_device_type(ip, hostname, vendor, mac)

            key = mac or ip
            if key in seen:
                continue
            seen.add(key)
            if mac == local_mac and ip != local_ip:
                continue

            all_devices.append({
                "ip": ip, "mac": mac, "hostname": hostname,
                "vendor": vendor, "device_type": dtype,
            })
            log.info("[拓扑]   %s  %s  %s  %s", ip, mac or "-", vendor or "-", dtype)

    log.info("[拓扑] 扫描完成，共 %d 台设备", len(all_devices))
    return all_devices

TOPOLOGY_INTERVAL = 300

def report_topology(devices, agent_id):
    try:
        req = urllib.request.Request(
            SERVER_URL + "/api/" + agent_id + "/topology",
            data=json.dumps({"devices": devices}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            log.info("[拓扑] 上报成功，已录入 %s 台设备", result.get("count", len(devices)))
            return result
    except Exception as e:
        log.warning("[拓扑] 上报失败: %s", e)
        return None

# ─── GUI 设置窗口 ────────────────────────────────────────────────────────

def show_setup_window(company_name=""):
    import tkinter as tk
    from tkinter import messagebox, scrolledtext
    import threading

    result = {
        "company_name": "", "ready": False, "autostart": True,
        "subnets": [], "targets": []
    }

    local_ip, subnet_prefix = get_local_subnet()
    initial_subnets = [subnet_prefix] if subnet_prefix else []
    initial_targets = [{"name": "网关", "host": "192.168.1.1"},
                       {"name": "DNS", "host": "8.8.8.8"}]

    # ── 扫描 ──────────────────────────────────────────────────────────
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
            devs = scan_subnet_devices(sp)
            for ip, mac in devs:
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

    # ── 窗口 ──────────────────────────────────────────────────────────
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
    subnet_listbox = tk.Listbox(subnet_f, font=("Courier New", 9), height=4, bg="#f5f5f7")
    subnet_listbox.pack(side="left", fill="x", expand=True)
    for s in initial_subnets:
        subnet_listbox.insert(tk.END, s)
    tk.Frame(subnet_f, width=4).pack(side="left")
    btn_col = tk.Frame(subnet_f)
    btn_col.pack(side="left")
    tk.Button(btn_col, text="+", font=("Arial", 11), width=3, bg="#34c759", fg="white",
              command=add_subnet).pack(pady=1)
    tk.Button(btn_col, text="-", font=("Arial", 11), width=3, bg="#ff3b30", fg="white",
              command=remove_subnet).pack(pady=1)

    subnet_e_f = tk.Frame(win)
    subnet_e_f.pack(fill="x", padx=20, pady=(0, 4))
    tk.Label(subnet_e_f, text="网段前缀：", font=("Arial", 10)).pack(side="left")
    subnet_entry = tk.Entry(subnet_e_f, font=("Arial", 10))
    subnet_entry.pack(side="left", fill="x", expand=True, padx=4)
    subnet_entry.insert(0, subnet_prefix)
    subnet_entry.bind("<Return>", lambda e: add_subnet())
    tk.Label(subnet_e_f, text="（如 192.168.2）", font=("Arial", 9), fg="#aaa").pack(side="left")

    # 监控目标
    tk.Label(win, text="监控目标", font=("Arial", 12, "bold")).pack(anchor="w", padx=20, pady=(10, 2))
    tk.Label(win, text="探测这些地址的连通性，至少填一个",
             font=("Arial", 9), fg="#888", anchor="w").pack(anchor="w", padx=20)

    targets_f = tk.Frame(win)
    targets_f.pack(fill="x", padx=20, pady=4)
    targets_listbox = tk.Listbox(targets_f, font=("Courier New", 9), height=4, bg="#f5f5f7")
    targets_listbox.pack(side="left", fill="x", expand=True)
    for t in initial_targets:
        targets_listbox.insert(tk.END, t["name"] + " -> " + t["host"])
    tk.Frame(targets_f, width=4).pack(side="left")
    btn_col2 = tk.Frame(targets_f)
    btn_col2.pack(side="left")
    tk.Button(btn_col2, text="+", font=("Arial", 11), width=3, bg="#34c759", fg="white",
              command=add_target).pack(pady=1)
    tk.Button(btn_col2, text="-", font=("Arial", 11), width=3, bg="#ff3b30", fg="white",
              command=remove_target).pack(pady=1)

    target_e_f = tk.Frame(win)
    target_e_f.pack(fill="x", padx=20, pady=(0, 4))
    target_name_entry = tk.Entry(target_e_f, font=("Arial", 9), width=10)
    target_name_entry.pack(side="left")
    target_name_entry.insert(0, "名称")
    tk.Label(target_e_f, text=" -> ", font=("Arial", 10)).pack(side="left")
    target_host_entry = tk.Entry(target_e_f, font=("Arial", 9))
    target_host_entry.pack(side="left", fill="x", expand=True, padx=4)
    target_host_entry.insert(0, "IP或域名")

    # 扫描
    scan_btn = tk.Button(win, text="扫描局域网", font=("Arial", 10),
                         bg="#5856d6", fg="white", activebackground="#4845a0",
                         command=lambda: threading.Thread(target=do_scan, daemon=True).start())
    scan_btn.pack(pady=6)

    result_text = scrolledtext.ScrolledText(win, font=("Courier New", 8),
                                            height=8, bg="#f5f5f7")
    result_text.pack(fill="both", expand=True, padx=20, pady=4)
    result_text.insert("1.0", "点击「扫描局域网」开始搜索...")
    result_text.config(state="disabled")

    btn_frame = tk.Frame(win)
    btn_frame.pack(pady=8)
    tk.Button(btn_frame, text="确定", font=("Arial", 11), bg="#007aff", fg="white",
              activebackground="#0064d0", width=12, command=on_submit).pack()
    entry.bind("<Return>", lambda e: on_submit())
    win.protocol("WM_DELETE_WINDOW", lambda: None)
    win.mainloop()

    if not result["ready"]:
        return None, True, "", [], []
    location = ", ".join(result["subnets"])
    return result["company_name"], result["autostart"], location, result["subnets"], result["targets"]

def show_about_window():
    import tkinter as tk
    import webbrowser

    win = tk.Tk()
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
    link = tk.Label(win, text="http://www.lanwatch.net/download", font=("Arial", 10), fg="#1a73e8", cursor="hand2")
    link.pack()
    link.bind("<Button-1>", lambda _: webbrowser.open("http://www.lanwatch.net/download"))
    tk.Button(win, text="确定", command=win.destroy, font=("Arial", 10), width=10).pack(pady=12)
    win.mainloop()


def show_success_window(company_name, agent_id, location=""):
    import tkinter as tk

    win = tk.Tk()
    win.title("网络监控 - 注册成功")
    win.geometry("460x240")
    win.resizable(False, False)
    win.attributes("-topmost", True)
    win.update_idletasks()
    cx = (win.winfo_screenwidth() - 460) // 2
    cy = (win.winfo_screenheight() - 240) // 2
    win.geometry(f"460x240+{cx}+{cy}")

    tk.Label(win, text="✓ 注册成功", font=("Arial", 16, "bold"), fg="#34c759").pack(pady=10)
    tk.Label(win, text=f"企业：{company_name}", font=("Arial", 11)).pack(pady=2)
    tk.Label(win, text=f"设备ID：{agent_id}", font=("Arial", 11, "italic"), fg="#555").pack(pady=2)
    if location:
        tk.Label(win, text=f"监控网段：{location}", font=("Arial", 11), fg="#5856d6").pack(pady=2)
    tk.Label(win, text="Agent已在后台运行，数据将自动上报。", font=("Arial", 10), fg="#888").pack(pady=6)
    tk.Label(win, text="系统托盘图标已显示，右键菜单可查看日志或退出。", font=("Arial", 9), fg="#aaa").pack()

    def on_close():
        win.destroy()
    tk.Button(win, text="确定", font=("Arial", 11), bg="#007aff", fg="white",
             width=12, command=on_close).pack(pady=14)
    win.protocol("WM_DELETE_WINDOW", on_close)
    win.mainloop()

# ─── 系统托盘 ────────────────────────────────────────────────────────────

_agent_id_for_tray = None
_company_name_for_tray = None
_tray_icon_ref = None

def tray_online(icon):
    """更新托盘为在线状态（绿色）"""
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (16, 16), (0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([2, 2, 13, 13], fill="#34c759")
        icon.icon = img
        icon.update = True
    except Exception:
        pass

def tray_offline(icon):
    """更新托盘为离线状态（红色）"""
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (16, 16), (0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([2, 2, 13, 13], fill="#ff3b30")
        icon.icon = img
        icon.update = True
    except Exception:
        pass

def open_log_file():
    """用记事本打开日志文件"""
    try:
        os.startfile(LOG_FILE)
    except Exception:
        pass

def stop_agent(tray_icon):
    try:
        tray_icon.stop()
    except Exception:
        pass
    log.info("[托盘] 用户请求退出，正在关闭...")
    os._exit(0)

def setup_tray(agent_id, company_name):
    """创建并启动系统托盘图标"""
    global _agent_id_for_tray, _company_name_for_tray, _tray_icon_ref
    _agent_id_for_tray = agent_id
    _company_name_for_tray = company_name

    try:
        from pystray import Icon, MenuItem, Menu
        from PIL import Image, ImageDraw
    except ImportError:
        log.warning("[托盘] pystray 未安装，托盘功能不可用")
        return None

    def create_image(color_hex="#34c759"):
        img = Image.new("RGB", (16, 16), (0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([2, 2, 13, 13], fill=color_hex)
        return img

    menu = Menu(
        MenuItem(f"企业：{company_name}", lambda _: None, enabled=False),
        MenuItem(f"ID：{agent_id[:8]}...", lambda _: None, enabled=False),
        MenuItem("───", lambda _: None, enabled=False),
        MenuItem("关于", lambda icon, _: show_about_window()),
        MenuItem("查看日志", lambda icon, _: open_log_file()),
        MenuItem("退出网络守护", lambda icon, _: stop_agent(icon)),
    )

    icon = Icon(
        "NetworkMonitorAgent",
        create_image("#34c759"),
        "企业网络监控",
        menu=menu
    )
    _tray_icon_ref = icon

    # 后台线程运行托盘
    t = threading.Thread(target=icon.run, daemon=True)
    t.start()
    log.info("[托盘] 系统托盘已启动")
    return icon

def update_tray_status(is_online):
    """根据在线/离线状态更新托盘颜色"""
    if _tray_icon_ref:
        try:
            if is_online:
                tray_online(_tray_icon_ref)
            else:
                tray_offline(_tray_icon_ref)
        except Exception:
            pass

# ─── 主流程 ───────────────────────────────────────────────────────────────

def main():
    global _tray_icon_ref

    log.info("=" * 40)
    log.info("网络监控 Agent 启动 v0.4")
    log.info("服务端: %s", SERVER_URL)
    log.info("=" * 40)

    # Windows 上隐藏控制台窗口（pythonw.exe 自动无窗口）
    if sys.platform == "win32":
        try:
            ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)
        except Exception:
            pass

    # 动态导入 winreg（仅 Windows）
    global winreg
    if sys.platform == "win32":
        try:
            winreg = __import__("winreg")
        except ImportError:
            winreg = None

    config = load_config()

    # ── 首次注册 ──
    if not config or not config.get("agent_id"):
        log.info("首次运行，显示设置向导...")
        company_name, autostart, location, subnets, targets = show_setup_window(
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
        show_success_window(company_name, agent_id, location)

        config = {
            "agent_id": agent_id,
            "company_name": company_name,
            "subnets": subnets,
            "targets": targets,
        }
        save_config(config)

        # 开机自启
        if autostart and winreg:
            set_autostart(True)

        agent_id = config["agent_id"]
        company_name = config["company_name"]
    else:
        agent_id = config["agent_id"]
        company_name = config.get("company_name", "")
        log.info("已配置 Agent ID: %s", agent_id)
        # 开机自启保持不变

    # 确保 subnets 在循环作用域内
    cfg = load_config()
    subnets = (cfg or {}).get("subnets", [])

    # ── 启动托盘 ──
    tray_icon = setup_tray(agent_id, company_name)
    if not tray_icon:
        log.warning("[托盘] 托盘启动失败，将继续无托盘运行")

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
                    f"{data['ping_rtt_ms']:.1f}" if data['ping_rtt_ms'] else "-",
                    f"{data['dns_ms']:.1f}" if data['dns_ms'] else "-",
                    data['target_name'] or "N/A",
                    data['target_reachable'])
            else:
                log.warning("上报失败 (连续失败 %d 次)", consecutive_errors)
                update_tray_status(False)

            topo_counter += 1
            if topo_counter >= TOPO_EVERY_N:
                topo_counter = 0
                cfg = load_config()
                subnets = (cfg or {}).get("subnets", [])
                devices = scan_topology(subnets) if subnets else scan_topology()
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
