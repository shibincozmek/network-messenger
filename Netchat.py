#!/usr/bin/env python3
"""
LAN Chat — single-file scanner + anonymous messenger, chat-style.

One program does everything: discovers devices on the local network,
shows a per-device chat thread (like a normal messaging app), and
includes a "Global" thread that broadcasts a message to every
currently-reachable device at once.

SCOPE / HONESTY NOTES (kept from the earlier versions, still true):
- Only run this on a network you own or have permission to use.
- "Anonymous" hides the display name shown to the recipient; it does
  not spoof IP/MAC or hide the traffic from the network itself.
- A device only shows up as "reachable" / can only receive a message
  if it is ALSO running this app (or an earlier receive-compatible
  version using the same protocol on port 50505). There is no way to
  push a message onto a device that hasn't opted in by running it.

Standard library only - nothing to pip install, and this single file
is all PyInstaller needs to build one .exe.
"""

import socket
import subprocess
import threading
import platform
import re
import json
import ipaddress
import tkinter as tk
from tkinter import ttk, messagebox

APP_PORT = 50505
GLOBAL_KEY = "__global__"  # special "contact" id for the broadcast thread

_IS_WINDOWS = platform.system().lower() == "windows"
# Suppress the console window Windows otherwise flashes for every
# subprocess call when running from a --windowed .exe.
_SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if _IS_WINDOWS else 0


# ----------------------------------------------------------------------
# Networking: messaging protocol
# ----------------------------------------------------------------------

def send_message(ip, text, anonymous=True, display_name=None, scope="private", port=APP_PORT, timeout=3):
    """
    scope: "private" (a normal 1:1 message, shown in that contact's thread)
           or "global" (a broadcast - the recipient shows it in their
           shared Global thread, not mixed into a private conversation).
    """
    payload = {
        "type": "msg",
        "scope": scope,
        "anonymous": anonymous,
        "name": "Anonymous" if anonymous else (display_name or socket.gethostname()),
        "text": text,
    }
    with socket.create_connection((ip, port), timeout=timeout) as s:
        s.sendall(json.dumps(payload).encode("utf-8"))


def check_listener_running(ip, port=APP_PORT, timeout=0.4):
    try:
        with socket.create_connection((ip, port), timeout=timeout) as s:
            s.sendall(json.dumps({"type": "ping"}).encode("utf-8"))
            return True
    except Exception:
        return False


class MessageListener(threading.Thread):
    """Background TCP server; calls on_message(sender_ip, name, text)."""

    def __init__(self, on_message, port=APP_PORT):
        super().__init__(daemon=True)
        self.on_message = on_message
        self.port = port
        self._stop = False

    def run(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.port))
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
                conn.settimeout(3)
                chunks = []
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                raw = b"".join(chunks)
                if not raw:
                    continue
                try:
                    data = json.loads(raw.decode("utf-8", errors="replace"))
                except Exception:
                    continue
                if data.get("type") == "ping":
                    continue
                if data.get("type") == "msg":
                    self.on_message(addr[0], data.get("name", "Anonymous"), data.get("text", ""),
                                     data.get("scope", "private"))
            finally:
                conn.close()
        srv.close()

    def stop(self):
        self._stop = True


# ----------------------------------------------------------------------
# Networking: discovery / scanning
# ----------------------------------------------------------------------

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return socket.gethostbyname(socket.gethostname())
    finally:
        s.close()


def get_all_ipv4_candidates():
    system = platform.system().lower()
    candidates = []
    try:
        if system == "windows":
            out = subprocess.run(["ipconfig"], capture_output=True, text=True, timeout=5,
                                  creationflags=_SUBPROCESS_FLAGS).stdout
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
        if not any(h in label.lower() for h in virtual_hints):
            return ip
        if best is None:
            best = ip
    return best or fallback_ip


def get_subnet_cidr():
    fallback_ip = get_local_ip()
    local_ip = guess_best_ip(get_all_ipv4_candidates(), fallback_ip)
    parts = local_ip.split(".")
    return ".".join(parts[:3]) + ".0/24", local_ip


def ping(ip):
    param = "-n" if _IS_WINDOWS else "-c"
    timeout_flag = "-w" if _IS_WINDOWS else "-W"
    timeout_val = "500" if _IS_WINDOWS else "1"
    try:
        subprocess.run(["ping", param, "1", timeout_flag, timeout_val, ip],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
                        creationflags=_SUBPROCESS_FLAGS)
    except Exception:
        pass


def sweep_subnet(cidr, progress_cb=None):
    net = ipaddress.ip_network(cidr, strict=False)
    hosts = list(net.hosts())
    threads = []
    for i, ip in enumerate(hosts):
        t = threading.Thread(target=ping, args=(str(ip),), daemon=True)
        threads.append(t)
        t.start()
        if len(threads) >= 40:
            for th in threads:
                th.join(timeout=2)
            threads = []
        if progress_cb:
            progress_cb(i + 1, len(hosts))
    for th in threads:
        th.join(timeout=2)


def read_arp_table():
    results = {}
    try:
        if _IS_WINDOWS:
            out = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=5,
                                  creationflags=_SUBPROCESS_FLAGS).stdout
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
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        return socket.gethostbyaddr(ip)[0]
    except Exception:
        return "Unknown device"
    finally:
        socket.setdefaulttimeout(old)


def scan_network(cidr, local_ip, progress_cb=None, log_cb=None):
    def log(msg):
        if log_cb:
            log_cb(msg)

    log(f"Local IP guessed as {local_ip}, sweeping {cidr} ...")
    sweep_subnet(cidr, progress_cb)
    arp = read_arp_table()
    log(f"ARP table has {len(arp)} total entries (all adapters).")

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
    log(f"{len(in_subnet)} entries fall inside {cidr} - resolving hostnames for those only.")

    hostnames = {}
    lock = threading.Lock()

    def worker(ip):
        name = resolve_hostname(ip)
        with lock:
            hostnames[ip] = name

    threads = [threading.Thread(target=worker, args=(ip,), daemon=True) for ip in in_subnet]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=2)

    devices = []
    for ip, mac in in_subnet.items():
        devices.append({
            "ip": ip, "mac": mac,
            "hostname": hostnames.get(ip, "Unknown device"),
            "is_self": ip == local_ip,
        })
    devices.sort(key=lambda d: tuple(int(p) for p in d["ip"].split(".")))
    log(f"{len(devices)} usable device(s) found.")
    return devices


# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------

class ChatApp:
    def __init__(self, root):
        self.root = root
        root.title("LAN Chat")
        root.geometry("920x640")
        root.minsize(760, 520)

        # chats[key] = list of (who, text) tuples; key is an IP, or GLOBAL_KEY
        self.chats = {GLOBAL_KEY: []}
        # contacts[ip] = {"hostname":..., "reachable": "yes"/"no"/"self", "unread": bool}
        self.contacts = {}
        self.selected_key = GLOBAL_KEY
        self.global_unread = False

        self._build_ui()

        self.listener = MessageListener(self.on_message)
        self.listener.start()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- UI construction --------------------------------------------------

    def _build_ui(self):
        root = self.root

        top = ttk.Frame(root, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="LAN Chat", font=("Segoe UI", 13, "bold")).pack(side="left")

        subnet_row = ttk.Frame(root, padding=(8, 0))
        subnet_row.pack(fill="x")
        ttk.Label(subnet_row, text="Subnet (CIDR):").pack(side="left")
        default_cidr, default_ip = get_subnet_cidr()
        self.cidr_var = tk.StringVar(value=default_cidr)
        ttk.Entry(subnet_row, textvariable=self.cidr_var, width=18).pack(side="left", padx=5)
        self.scan_btn = ttk.Button(subnet_row, text="Scan Network", command=self.start_scan)
        self.scan_btn.pack(side="left", padx=(10, 0))
        self.status_var = tk.StringVar(value=f"Your IP: {default_ip}")
        ttk.Label(subnet_row, textvariable=self.status_var, foreground="#666").pack(side="left", padx=10)

        name_row = ttk.Frame(root, padding=(8, 2))
        name_row.pack(fill="x")
        ttk.Label(name_row, text="Your name (used when not anonymous):").pack(side="left")
        self.name_var = tk.StringVar(value=socket.gethostname())
        ttk.Entry(name_row, textvariable=self.name_var, width=25).pack(side="left", padx=5)

        body = ttk.Frame(root)
        body.pack(fill="both", expand=True, padx=8, pady=8)

        # Left: contact list
        left = ttk.Frame(body, width=260)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        ttk.Label(left, text="Contacts", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.contact_list = tk.Listbox(left, activestyle="none", font=("Segoe UI", 10))
        self.contact_list.pack(fill="both", expand=True, pady=(4, 0))
        self.contact_list.bind("<<ListboxSelect>>", self._on_select_contact)
        self._contact_keys = []  # parallel list: index -> key (ip or GLOBAL_KEY)
        self._refresh_contact_list()

        # Right: chat transcript + composer
        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))

        self.chat_title_var = tk.StringVar(value="Global (broadcast to all reachable devices)")
        ttk.Label(right, textvariable=self.chat_title_var, font=("Segoe UI", 11, "bold")).pack(anchor="w")

        self.transcript = tk.Text(right, wrap="word", state="disabled", font=("Segoe UI", 10))
        self.transcript.pack(fill="both", expand=True, pady=(4, 6))
        self.transcript.tag_configure("me", foreground="#1a5fb4")
        self.transcript.tag_configure("them", foreground="#1a1a1a")
        self.transcript.tag_configure("meta", foreground="#888")

        composer = ttk.Frame(right)
        composer.pack(fill="x")
        self.msg_entry = ttk.Entry(composer)
        self.msg_entry.pack(side="left", fill="x", expand=True)
        self.msg_entry.bind("<Return>", lambda e: self.send_current())
        self.anon_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(composer, text="Anonymous", variable=self.anon_var).pack(side="left", padx=5)
        self.send_btn = ttk.Button(composer, text="Send", command=self.send_current)
        self.send_btn.pack(side="left")

        log_frame = ttk.LabelFrame(root, text="Diagnostics log", padding=4)
        log_frame.pack(fill="x", padx=8, pady=(0, 8))
        self.log_text = tk.Text(log_frame, height=4, wrap="word", state="disabled", font=("Consolas", 9))
        self.log_text.pack(fill="x")

    # -- Contact list helpers ---------------------------------------------

    def _refresh_contact_list(self):
        self.contact_list.delete(0, "end")
        self._contact_keys = [GLOBAL_KEY]
        global_label = "🌐 Global (send to all)" + (" ●" if self.global_unread else "")
        self.contact_list.insert("end", global_label)

        for ip in sorted(self.contacts, key=lambda ip: tuple(int(p) for p in ip.split("."))):
            c = self.contacts[ip]
            label = c["hostname"]
            if c["reachable"] == "self":
                continue  # don't list ourselves as a chat target
            marker = "🟢" if c["reachable"] == "yes" else "⚪"
            unread = " ●" if c.get("unread") else ""
            self.contact_list.insert("end", f"{marker} {label} ({ip}){unread}")
            self._contact_keys.append(ip)

        # Keep selection on whatever was selected before refreshing
        if self.selected_key in self._contact_keys:
            idx = self._contact_keys.index(self.selected_key)
            self.contact_list.selection_set(idx)

    def _on_select_contact(self, event):
        sel = self.contact_list.curselection()
        if not sel:
            return
        key = self._contact_keys[sel[0]]
        self.selected_key = key
        if key == GLOBAL_KEY:
            self.global_unread = False
            self._refresh_contact_list()
        elif key in self.contacts:
            self.contacts[key]["unread"] = False
            self._refresh_contact_list()
        self._render_transcript()

    def _render_transcript(self):
        key = self.selected_key
        if key == GLOBAL_KEY:
            self.chat_title_var.set("Global (broadcast to all reachable devices)")
        else:
            c = self.contacts.get(key, {"hostname": "Unknown device"})
            self.chat_title_var.set(f"{c['hostname']} ({key})")

        self.transcript.config(state="normal")
        self.transcript.delete("1.0", "end")
        for who, text in self.chats.get(key, []):
            tag = "me" if who == "You" else "them"
            self.transcript.insert("end", f"{who}: ", ("meta",))
            self.transcript.insert("end", f"{text}\n", (tag,))
        self.transcript.config(state="disabled")
        self.transcript.see("end")

    def _append_chat(self, key, who, text):
        self.chats.setdefault(key, []).append((who, text))
        if self.selected_key == key:
            self._render_transcript()

    # -- Logging ------------------------------------------------------------

    def log(self, msg):
        def _write():
            self.log_text.config(state="normal")
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
            self.log_text.config(state="disabled")
        self.root.after(0, _write)

    # -- Scanning -------------------------------------------------------

    def start_scan(self):
        self.scan_btn.config(state="disabled")
        self.status_var.set("Scanning...")
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
        self.status_var.set("Checking which devices are reachable for messaging...")
        for d in devices:
            d["reachable"] = "self" if d["is_self"] else ("yes" if check_listener_running(d["ip"]) else "no")
        self.root.after(0, lambda: self._apply_scan_results(devices))

    def _apply_scan_results(self, devices):
        for d in devices:
            existing = self.contacts.get(d["ip"], {})
            self.contacts[d["ip"]] = {
                "hostname": d["hostname"],
                "reachable": d["reachable"],
                "unread": existing.get("unread", False),
            }
        self._refresh_contact_list()
        self.status_var.set(f"Found {len(devices)} device(s).")
        self.scan_btn.config(state="normal")

    # -- Sending ----------------------------------------------------------

    def send_current(self):
        text = self.msg_entry.get().strip()
        if not text:
            return
        key = self.selected_key
        anonymous = self.anon_var.get()
        display_name = self.name_var.get().strip() or socket.gethostname()

        if key == GLOBAL_KEY:
            targets = [ip for ip, c in self.contacts.items() if c["reachable"] == "yes"]
            if not targets:
                messagebox.showwarning(
                    "No reachable devices",
                    "Scan the network first - no devices are currently marked reachable."
                )
                return
            self._append_chat(GLOBAL_KEY, "You", text)
            self.msg_entry.delete(0, "end")
            threading.Thread(target=self._broadcast_worker,
                              args=(targets, text, anonymous, display_name), daemon=True).start()
            return

        # single-contact (private) send
        contact = self.contacts.get(key)
        if not contact or contact["reachable"] != "yes":
            messagebox.showerror(
                "Not reachable",
                "This device isn't currently reachable. Rescan the network, or wait for "
                "them to message you first (that also opens a chat with them)."
            )
            return
        try:
            send_message(key, text, anonymous=anonymous, display_name=display_name, scope="private")
            self._append_chat(key, "You", text)
            self.msg_entry.delete(0, "end")
        except Exception as e:
            messagebox.showerror("Send failed", str(e))

    def _broadcast_worker(self, targets, text, anonymous, display_name):
        sent, failed = 0, 0
        for ip in targets:
            try:
                send_message(ip, text, anonymous=anonymous, display_name=display_name, scope="global")
                sent += 1
            except Exception:
                failed += 1
        self.log(f"Global message sent to {sent} device(s), failed for {failed}.")

    # -- Receiving ----------------------------------------------------------

    def on_message(self, sender_ip, display_name, text, scope):
        self.root.after(0, lambda: self._handle_incoming(sender_ip, display_name, text, scope))

    def _handle_incoming(self, sender_ip, display_name, text, scope):
        # Track/refresh the sender as a contact either way, so they appear in
        # the list and can be replied to privately - but WHERE the message
        # itself is shown depends on scope.
        if sender_ip not in self.contacts:
            self.contacts[sender_ip] = {"hostname": display_name, "reachable": "yes", "unread": False}
        else:
            self.contacts[sender_ip]["reachable"] = "yes"

        if scope == "global":
            self._append_chat(GLOBAL_KEY, display_name, text)
            if self.selected_key != GLOBAL_KEY:
                self.global_unread = True
                self.root.bell()
        else:
            self._append_chat(sender_ip, display_name, text)
            if self.selected_key != sender_ip:
                self.contacts[sender_ip]["unread"] = True
                self.root.bell()

        self._refresh_contact_list()
        self.root.deiconify()
        self.root.lift()

    def _on_close(self):
        self.listener.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    ChatApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()