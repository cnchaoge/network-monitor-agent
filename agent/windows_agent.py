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
            "name": company_name,
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

# ─── 局域网拓扑扫描 ────────────────────────────────────────────────────────

# MAC 厂商 OUI 前缀库（常见品牌，缩小版）
OUI_VENDOR = {
    "00:50:56": "VMware",     "00:0C:29": "VMware",     "00:1C:14": "VMware",
    "00:05:69": "VMware",
    "00:50:BA": "NetGear",   "00:14:6C": "NetGear",
    "00:1B:2B": "HP",        "00:1F:29": "HP",         "00:21:5A": "HP",
    "00:22:64": "Dell",      "00:06:5B": "Dell",
    "00:1C:B3": "Apple",     "00:1D:4F": "Apple",      "00:1E:C9": "Apple",
    "00:1E:52": "Cisco",     "00:1A:2B": "Cisco",      "00:25:84": "Cisco",
    "00:1E:BE": "Cisco",
    "00:04:4B": "Nvidia",
    "00:1A:11": "Google",
    "00:50:F2": "Microsoft", "00:0D:3A": "Microsoft",  "00:12:5A": "Microsoft",
    "00:15:5D": "Microsoft", "00:17:FA": "Microsoft",
    "00:1A:6B": "TP-Link",   "00:27:19": "TP-Link",    "14:CC:20": "TP-Link",
    "30:B5:C2": "TP-Link",
    "00:25:9E": "Cisco-Linksys", "00:1A:70": "Cisco-Linksys",
    "00:1E:58": "D-Link",     "00:22:B0": "D-Link",     "00:26:5A": "D-Link",
    "1C:AF:F7": "D-Link",
    "00:1F:33": "Netgear",
    "00:24:B2": "ZTE",       "00:1B:3C": "ZTE",        "44:2A:60": "ZTE",
    "00:25:68": "Huawei",   "00:18:82": "Huawei",     "00:1E:10": "Huawei",
    "34:29:12": "Huawei",
    "00:09:5B": "NetGear",
    "20:CF:30": "Xiaomi",    "34:80:B3": "Xiaomi",     "F8:A4:5F": "Xiaomi",
    "C8:D7:B0": "Xiaomi",
    "18:31:BF": "Huawei",    "88:53:95": "Huawei",
    "00:17:88": "Philips",   "00:18:FE": "Philips",
    "00:16:3E": "Xensource",
    "00:1C:42": "Parallels",
    "08:00:27": "VirtualBox",
}

def get_vendor(mac):
    """根据 MAC 地址查询厂商"""
    if not mac:
        return ""
    prefix = mac.upper().replace("-", ":")[:8]
    return OUI_VENDOR.get(prefix, "")

def guess_device_type(ip, hostname, vendor, mac):
    """根据信息推测设备类型"""
    h = (hostname or "").lower()
    v = (vendor or "").lower()
    # 根据主机名判断
    if any(k in h for k in ["router", "gateway", "tplink", "netgear", "tendawifi", "mercury", "192.168"]):
        return "router"
    if any(k in h for k in ["printer", "print", "hp", "canon", "brother", "epson", "xerox"]):
        return "printer"
    if any(k in h for k in ["server", "nas", "synology", "qnap", "群晖", "威联通"]):
        return "server"
    if any(k in h for k in ["switch", "sw", "s2950", "s5050"]):
        return "switch"
    # 根据厂商判断
    if "cisco" in v and "router" in h:
        return "router"
    if "hp" in v:
        return "switch"
    if "dell" in v:
        return "server"
    if "vmware" in v or "virtualbox" in v or "xensource" in v or "parallels" in v:
        return "vm"
    if "apple" in v:
        return "phone"
    if "xiaomi" in v or "huawei" in v or "zte" in v:
        return "router"
    if "microsoft" in v and "xbox" in h:
        return "game"
    # 默认
    return "unknown"

def get_local_ip_and_mac():
    """获取本机 IP 和 MAC 地址"""
    mac_addr = ""
    ip_addr = ""
    try:
        # Windows 上隐藏子进程窗口
        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
        # Windows: getmac 或 arp -a
        result = subprocess.run("getmac /v /fo csv", capture_output=True, text=True, timeout=5, startupinfo=startupinfo)
        for line in result.stdout.splitlines():
            if "正在启用" in line or "Online" in line:
                parts = line.split(",")
                for p in parts:
                    if ":" in p and len(p.replace('"','').replace('-',':').strip()) == 17:
                        mac_addr = p.replace('"', '').strip().upper()
                        break
        # 获取 IP
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
    """获取网关 IP"""
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
    """快速 ping 扫描同 subnet 存活主机，返回 IP 列表"""
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
    """查询单个 IP 的 MAC 地址（从 ARP 缓存）"""
    try:
        # Windows 上隐藏子进程窗口
        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
        # 先强制发一个 ARP 请求
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.3)
        try:
            sock.sendto(b"", (ip, 80))
        except Exception:
            pass
        sock.close()
        # 读取 ARP 表
        result = subprocess.run("arp -a", capture_output=True, text=True, timeout=5, startupinfo=startupinfo)
        for line in result.stdout.splitlines():
            if ip in line:
                parts = line.split()
                for p in parts:
                    if ":" in p and p.count(":") == 5:
                        return p.upper()
    except Exception:
        pass
    return ""

def get_hostname(ip):
    """反解主机名"""
    try:
        name, _, _ = socket.gethostbyaddr(ip)
        return name
    except Exception:
        return ""

def scan_topology():
    """完整拓扑扫描"""
    log.info("[拓扑] 开始扫描局域网...")
    gateway = get_gateway_ip()
    local_ip, local_mac = get_local_ip_and_mac()
    subnet_prefix = local_ip.rsplit(".", 1)[0] if local_ip else "192.168.1"

    # ping 扫描
    alive_ips = ping_scan(subnet_prefix, timeout=0.5)
    log.info("[拓扑] 发现 %d 台存活主机", len(alive_ips))

    devices = []
    seen = set()

    for ip in alive_ips:
        mac = arp_lookup(ip)
        hostname = get_hostname(ip)
        vendor = get_vendor(mac)
        dtype = guess_device_type(ip, hostname, vendor, mac)

        key = mac or ip
        if key in seen:
            continue
        seen.add(key)

        # 跳过本机和网关重复
        if mac == local_mac and ip != local_ip:
            continue

        devices.append({
            "ip": ip,
            "mac": mac,
            "hostname": hostname,
            "vendor": vendor,
            "device_type": dtype,
        })
        log.info("[拓扑]  %s  %s  %s  %s", ip, mac or "- ", vendor or "-", dtype)

    log.info("[拓扑] 扫描完成，共 %d 台设备", len(devices))
    return devices

TOPOLOGY_INTERVAL = 300  # 5 分钟扫描一次拓扑

def report_topology(devices, agent_id):
    """上报拓扑数据到服务端"""
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

# ─── 设置向导窗口 ─────────────────────────────────────────────────────────

def show_setup_window(company_name=""):
    """弹出tkinter设置窗口，返回输入的企业名称或None"""
    import tkinter as tk
    from tkinter import messagebox

    result = {"company_name": "", "ready": False}

    def on_submit():
        name = entry.get().strip()
        if not name:
            messagebox.showwarning("提示", "请填写企业名称")
            return
        result["company_name"] = name
        result["ready"] = True
        win.destroy()

    def on_ok():
        win.destroy()

    win = tk.Tk()
    win.title("网络监控 - 首次设置")
    win.geometry("420x220")
    win.resizable(False, False)
    win.attributes("-topmost", True)
    win.update_idletasks()
    cx = (win.winfo_screenwidth() - 420) // 2
    cy = (win.winfo_screenheight() - 220) // 2
    win.geometry(f"420x220+{cx}+{cy}")

    title_label = tk.Label(win, text="企业网络监控", font=("Arial", 16, "bold"))
    title_label.pack(pady=12)
    sub_label = tk.Label(win, text="首次使用，请填写以下信息：", font=("Arial", 10), fg="#666")
    sub_label.pack(pady=2)

    frame = tk.Frame(win)
    frame.pack(pady=10, padx=24, fill="x")
    tk.Label(frame, text="企业名称：", font=("Arial", 11)).grid(row=0, column=0, sticky="w", pady=8)
    entry = tk.Entry(frame, font=("Arial", 11))
    entry.grid(row=0, column=1, sticky="ew", pady=8, padx=4)
    entry.insert(0, company_name)
    entry.focus()
    frame.columnconfigure(1, weight=1)

    btn_frame = tk.Frame(win)
    btn_frame.pack(pady=10)
    tk.Button(btn_frame, text="确定", font=("Arial", 11), bg="#007aff", fg="white",
              activebackground="#0064d0", width=12, command=on_submit).pack()
    entry.bind("<Return>", lambda e: on_submit())
    win.protocol("WM_DELETE_WINDOW", lambda: None)
    win.mainloop()

    if not result["ready"]:
        return None
    return result["company_name"]

def show_success_window(company_name, agent_id):
    """显示注册成功信息（复用同一 Tk 实例，避免多窗口冲突）"""
    import tkinter as tk

    win = tk.Tk()
    win.title("网络监控 - 注册成功")
    win.geometry("440x200")
    win.resizable(False, False)
    win.attributes("-topmost", True)
    win.update_idletasks()
    cx = (win.winfo_screenwidth() - 440) // 2
    cy = (win.winfo_screenheight() - 200) // 2
    win.geometry(f"440x200+{cx}+{cy}")

    tk.Label(win, text="✓ 注册成功", font=("Arial", 16, "bold"), fg="#34c759").pack(pady=12)
    tk.Label(win, text=f"企业：{company_name}", font=("Arial", 11)).pack(pady=2)
    tk.Label(win, text=f"设备ID：{agent_id}", font=("Arial", 11, "italic"), fg="#555").pack(pady=2)
    tk.Label(win, text="Agent已开始运行，数据将自动上报。", font=("Arial", 10), fg="#888").pack(pady=8)
    tk.Label(win, text="本窗口关闭后Agent将在后台继续运行", font=("Arial", 9), fg="#aaa").pack()

    def on_close():
        win.destroy()
    tk.Button(win, text="确定", font=("Arial", 11), bg="#007aff", fg="white",
             width=12, command=on_close).pack(pady=14)
    win.protocol("WM_DELETE_WINDOW", on_close)
    win.mainloop()

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
        company_name = show_setup_window(
            company_name=config.get("company_name") if config else ""
        )
        if not company_name:
            log.info("用户取消设置，退出")
            return

        log.info("正在注册企业: %s", company_name)

        # 注册
        agent_id = register_agent(company_name)
        if not agent_id:
            log.error("注册失败，退出")
            return

        log.info("注册成功，Agent ID: %s", agent_id)
        show_success_window(company_name, agent_id)

        # 保存配置
        config = {"agent_id": agent_id, "company_name": company_name}
        save_config(config)
        log.info("配置已保存")
    else:
        agent_id = config["agent_id"]
        log.info("已配置 Agent ID: %s", agent_id)

    log.info("开始探测...")
    consecutive_errors = 0
    topo_counter = 0  # 每 topo_interval 次循环做一次拓扑扫描
    TOPO_EVERY_N = max(1, TOPOLOGY_INTERVAL // REPORT_INTERVAL)  # 多少轮做一次拓扑

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

            # 拓扑扫描（每 TOPOLOGY_INTERVAL 秒一次）
            topo_counter += 1
            if topo_counter >= TOPO_EVERY_N:
                topo_counter = 0
                devices = scan_topology()
                if devices:
                    report_topology(devices, agent_id)

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
