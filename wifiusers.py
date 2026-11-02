#!/usr/bin/env python3
"""
LAN Device Scanner + Anonymous Messenger
------------------------------------------
Hackathon tool: discovers devices on your local network, lists their
names/IP/MAC, and lets you send a plain-text message to any device that
is ALSO running this app (peer-to-peer, not a silent push to arbitrary
machines).

IMPORTANT / SCOPE:
- Only run this on a network you own or have explicit permission to use
  (e.g. your own home/lab network, or a network set up for the hackathon).
- "Anonymous" here means the RECEIVER sees "Anonymous" instead of a name -
  it does NOT spoof IP/MAC addresses or try to hide the traffic from the
  network itself. This is a transparency choice on the UI, not an evasion
  technique.
- A device only receives a message if it is running this same script
  (the listener). Devices that are not running it will simply not show
  up as "reachable" for messaging - there's no way (and no attempt here)
  to pop a message onto a machine that hasn't opted in by running the app.

Dependencies: pure Python standard library only (socket, subprocess,
threading, tkinter). No scapy/nmap required, though it will use `arp`
if present for a faster/more complete scan.
"""

import socket
import subprocess
import threading
import platform
import re
import time
import ipaddress
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

APP_PORT = 50505           # TCP port this app listens on for messages
DISCOVERY_PORT = 50506     # UDP port used for "who's running this app" pings
BROADCAST_TIMEOUT = 1.5


# ----------------------------------------------------------------------
# Networking helpers
# ----------------------------------------------------------------------

def get_local_ip():
    """Best-effort local IP of the active interface."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = socket.gethostbyname(socket.gethostname())
    finally:
        s.close()
    return ip


def get_all_ipv4_candidates():
    """
    Return a list of (ip, label) for every IPv4 address on this machine,
    using the OS-native command so we see every adapter (Wi-Fi, Ethernet,
    plus virtual ones from VPNs/Docker/WSL/Hyper-V/VirtualBox etc).
    The auto-detect below prefers a 'real' private LAN range, but on
    machines with lots of virtual adapters it can still guess wrong -
    that's exactly why the subnet field in the GUI is editable.
    """
    system = platform.system().lower()
    candidates = []
    try:
        if system == "windows":
            out = subprocess.run(["ipconfig"], capture_output=True, text=True, timeout=5).stdout
            current_adapter = ""
            for line in out.splitlines():
                if line and not line.startswith(" ") and ":" in line:
                    current_adapter = line.strip().rstrip(":")
                m = re.search(r"IPv4 Address[.\s]*:\s*([\d.]+)", line)
                if m:
                    candidates.append((m.group(1), current_adapter))
        else:
            out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5).stdout
            for ip in out.split():
                candidates.append((ip.strip(), ""))
    except Exception:
        pass
    return candidates


def guess_best_ip(candidates, fallback_ip):
    """
    Prefer a typical home/office LAN address (192.168.x.x, 10.x.x.x,
    172.16-31.x.x) whose adapter name doesn't look virtual. Falls back
    to fallback_ip if nothing better is found.
    """
    virtual_hints = ("virtual", "vmware", "virtualbox", "hyper-v", "docker",
                      "wsl", "loopback", "vpn", "tap", "tunnel")
    best = None
    for ip, label in candidates:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if not addr.is_private or addr.is_loopback:
            continue
        looks_virtual = any(h in label.lower() for h in virtual_hints)
        if not looks_virtual:
            return ip  # strong match: private + real-looking adapter name
        if best is None:
            best = ip  # weak fallback if nothing better shows up
    return best or fallback_ip


def get_subnet_cidr():
    """Pick the most likely real-LAN IPv4 + assume a /24 (editable in the GUI)."""
    fallback_ip = get_local_ip()
    candidates = get_all_ipv4_candidates()
    local_ip = guess_best_ip(candidates, fallback_ip)
    parts = local_ip.split(".")
    cidr = ".".join(parts[:3]) + ".0/24"
    return cidr, local_ip


def ping(ip):
    """OS-appropriate single ping, used to populate the ARP table."""
    param = "-n" if platform.system().lower() == "windows" else "-c"
    timeout_flag = "-w" if platform.system().lower() == "windows" else "-W"
    timeout_val = "500" if platform.system().lower() == "windows" else "1"
    try:
        subprocess.run(
            ["ping", param, "1", timeout_flag, timeout_val, ip],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2
        )
    except Exception:
        pass


def sweep_subnet(cidr, progress_cb=None):
    """Ping every host in the subnet to populate the OS ARP cache (parallel)."""
    net = ipaddress.ip_network(cidr, strict=False)
    hosts = list(net.hosts())
    threads = []

    def worker(ip):
        ping(str(ip))

    for i, ip in enumerate(hosts):
        t = threading.Thread(target=worker, args=(ip,), daemon=True)
        threads.append(t)
        t.start()
        if len(threads) >= 40:  # cap concurrency
            for th in threads:
                th.join(timeout=2)
            threads = []
        if progress_cb:
            progress_cb(i + 1, len(hosts))
    for th in threads:
        th.join(timeout=2)


def read_arp_table():
    """Parse the OS ARP table into {ip: mac}."""
    system = platform.system().lower()
    results = {}
    try:
        if system == "windows":
            out = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                m = re.search(r"(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F-]{17})", line)
                if m:
                    results[m.group(1)] = m.group(2).replace("-", ":").lower()
        else:
            out = subprocess.run(["arp", "-an"], capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                m = re.search(r"\((\d+\.\d+\.\d+\.\d+)\) at ([0-9a-fA-F:]{17})", line)
                if m:
                    results[m.group(1)] = m.group(2).lower()
    except Exception:
        pass
    return results


def resolve_hostname(ip, timeout=0.3):
    """Try reverse DNS lookup with a short timeout; fall back to 'Unknown device'."""
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        name = socket.gethostbyaddr(ip)[0]
        return name
    except Exception:
        return "Unknown device"
    finally:
        socket.setdefaulttimeout(old_timeout)


def scan_network(cidr, local_ip, progress_cb=None, log_cb=None):
    """Full scan: sweep subnet, read ARP table, resolve hostnames."""
    def log(msg):
        if log_cb:
            log_cb(msg)

    log(f"Local IP guessed as {local_ip}, sweeping {cidr} ...")
    sweep_subnet(cidr, progress_cb)
    arp = read_arp_table()
    log(f"ARP table has {len(arp)} total entries (all adapters, not just this subnet).")

    net = ipaddress.ip_network(cidr, strict=False)
    in_subnet = {}
    for ip, mac in arp.items():
        if mac in ("ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00"):
            continue
        try:
            if ipaddress.ip_address(ip) not in net:
                continue
        except ValueError:
            continue
        in_subnet[ip] = mac
    log(f"{len(in_subnet)} entries fall inside {cidr} - resolving hostnames for those only "
        f"(skipping the rest so stale entries from other networks/VPNs don't stall the scan).")

    hostnames = {}
    lock = threading.Lock()

    def resolve_worker(ip):
        name = resolve_hostname(ip)
        with lock:
            hostnames[ip] = name

    threads = [threading.Thread(target=resolve_worker, args=(ip,), daemon=True) for ip in in_subnet]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=2)

    devices = []
    for ip, mac in in_subnet.items():
        devices.append({
            "ip": ip,
            "mac": mac,
            "hostname": hostnames.get(ip, "Unknown device"),
            "is_self": ip == local_ip,
        })
    devices.sort(key=lambda d: tuple(int(p) for p in d["ip"].split(".")))
    log(f"{len(devices)} usable device(s) after filtering to this subnet.")
    if not devices:
        log("Zero devices found. Likely causes: (1) the subnet above is wrong for "
            "your real Wi-Fi/Ethernet adapter - check Settings > Network and fix the "
            "CIDR field above and rescan; (2) Windows Firewall is blocking outbound "
            "ping - try running this script as Administrator; (3) the network isolates "
            "clients from each other (common on guest/campus Wi-Fi).")
    return devices


def check_messenger_running(ip, timeout=0.4):
    """Check whether the given IP is running this app's listener (opt-in check)."""
    try:
        with socket.create_connection((ip, APP_PORT), timeout=timeout) as s:
            s.sendall(b"PING")
            s.close()
            return True
    except Exception:
        return False


# ----------------------------------------------------------------------
# Messaging listener (receiver side)
# ----------------------------------------------------------------------

class MessageListener(threading.Thread):
    def __init__(self, on_message):
        super().__init__(daemon=True)
        self.on_message = on_message
        self._stop = False

    def run(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", APP_PORT))
        srv.listen(5)
        srv.settimeout(1.0)
        while not self._stop:
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                data = conn.recv(4096)
                if data and data != b"PING":
                    text = data.decode("utf-8", errors="replace")
                    self.on_message(text)
            finally:
                conn.close()
        srv.close()

    def stop(self):
        self._stop = True


def send_message(ip, text, anonymous=True):
    """Send a plain-text message to a device running this app's listener."""
    sender_tag = "Anonymous" if anonymous else socket.gethostname()
    payload = f"{sender_tag}: {text}"
    with socket.create_connection((ip, APP_PORT), timeout=3) as s:
        s.sendall(payload.encode("utf-8"))


# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------

class App:
    def __init__(self, root):
        self.root = root
        root.title("LAN Device Scanner + Messenger")
        root.geometry("760x680")
        root.minsize(680, 560)

        top = ttk.Frame(root, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="LAN Device Scanner + Anonymous Messenger",
                  font=("Segoe UI", 13, "bold")).pack(side="left")

        subnet_frame = ttk.Frame(root, padding=(10, 0))
        subnet_frame.pack(fill="x")
        ttk.Label(subnet_frame, text="Subnet (CIDR):").pack(side="left")
        default_cidr, default_ip = get_subnet_cidr()
        self.cidr_var = tk.StringVar(value=default_cidr)
        ttk.Entry(subnet_frame, textvariable=self.cidr_var, width=20).pack(side="left", padx=5)
        ttk.Label(subnet_frame, text=f"(detected local IP: {default_ip} - edit the CIDR above "
                                      "if this isn't your real Wi-Fi/Ethernet subnet)",
                  foreground="#666").pack(side="left")

        btn_frame = ttk.Frame(root, padding=(10, 5))
        btn_frame.pack(fill="x")
        self.scan_btn = ttk.Button(btn_frame, text="Scan Network", command=self.start_scan)
        self.scan_btn.pack(side="left")
        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(btn_frame, textvariable=self.status_var).pack(side="left", padx=10)

        cols = ("ip", "mac", "hostname", "reachable")
        self.tree = ttk.Treeview(root, columns=cols, show="headings", height=9)
        for c, w in zip(cols, (120, 150, 220, 90)):
            self.tree.heading(c, text=c.upper())
            self.tree.column(c, width=w)
        self.tree.pack(fill="both", expand=True, padx=10, pady=10)

        msg_frame = ttk.Frame(root, padding=10)
        msg_frame.pack(fill="x")
        ttk.Label(msg_frame, text="Message:").pack(side="left")
        self.msg_entry = ttk.Entry(msg_frame)
        self.msg_entry.pack(side="left", fill="x", expand=True, padx=5)
        self.anon_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(msg_frame, text="Send anonymously", variable=self.anon_var).pack(side="left", padx=5)
        ttk.Button(msg_frame, text="Send to selected", command=self.send_to_selected).pack(side="left")

        log_frame = ttk.LabelFrame(root, text="Diagnostics log", padding=5)
        log_frame.pack(fill="x", padx=10, pady=(0, 5))
        self.log_text = tk.Text(log_frame, height=5, wrap="word", state="disabled",
                                 font=("Consolas", 9))
        self.log_text.pack(fill="x")

        self.listener = MessageListener(self.show_incoming_message)
        self.listener.start()

    def log(self, msg):
        def _write():
            self.log_text.config(state="normal")
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
            self.log_text.config(state="disabled")
        self.root.after(0, _write)

    def start_scan(self):
        self.scan_btn.config(state="disabled")
        self.status_var.set("Scanning...")
        self.tree.delete(*self.tree.get_children())
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")
        cidr = self.cidr_var.get().strip()
        self.log(f"Scan started on {cidr} ...")
        threading.Thread(target=self._scan_worker, args=(cidr,), daemon=True).start()

    def _scan_worker(self, cidr):
        def progress(done, total):
            self.status_var.set(f"Scanning... {done}/{total}")

        _, local_ip = get_subnet_cidr()
        devices = scan_network(cidr, local_ip, progress_cb=progress, log_cb=self.log)
        self.status_var.set(f"Checking which devices are reachable for messaging...")
        for d in devices:
            reachable = "self" if d["is_self"] else ("yes" if check_messenger_running(d["ip"]) else "no")
            d["reachable"] = reachable

        self.root.after(0, lambda: self._populate(devices))

    def _populate(self, devices):
        for d in devices:
            self.tree.insert("", "end", values=(d["ip"], d["mac"], d["hostname"], d["reachable"]))
        self.status_var.set(f"Found {len(devices)} device(s).")
        self.scan_btn.config(state="normal")

    def send_to_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("No selection", "Select a device from the list first.")
            return
        values = self.tree.item(sel[0], "values")
        ip, mac, hostname, reachable = values
        text = self.msg_entry.get().strip()
        if not text:
            messagebox.showwarning("Empty message", "Type a message first.")
            return
        if reachable != "yes":
            messagebox.showerror(
                "Not reachable",
                f"{hostname} ({ip}) isn't running this app's listener, "
                "so a message can't be delivered to it."
            )
            return
        try:
            send_message(ip, text, anonymous=self.anon_var.get())
            self.msg_entry.delete(0, "end")
            self.status_var.set(f"Message sent to {hostname} ({ip}).")
        except Exception as e:
            messagebox.showerror("Send failed", str(e))

    def show_incoming_message(self, text):
        self.root.after(0, lambda: messagebox.showinfo("New message", text))


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()