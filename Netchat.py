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

Text chat needs only the standard library. Images and voice messages
need two extra pip packages (Pillow for images; sounddevice + numpy
for audio) - if those aren't installed, text chat still works fully
and the Image/Voice buttons just explain what to install. Build with
build_exe.bat, which installs these before bundling the .exe.
"""

import socket
import subprocess
import threading
import platform
import re
import json
import io
import os
import base64
import tempfile
import wave
import ipaddress
import math
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import time


try:
    from PIL import Image, ImageTk
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

try:
    import numpy as np
    import sounddevice as sd
    HAVE_AUDIO = True
except ImportError:
    HAVE_AUDIO = False

APP_PORT = 50505
DISCOVERY_PORT = 50506
GLOBAL_KEY = "__global__"  # special "contact" id for the broadcast thread
MAX_IMAGE_DIM = 1024       # longest side an image is resized to before sending
MAX_VOICE_SECONDS = 30     # recording auto-stops after this long
DISCOVERY_TIMEOUT = 3.5
CONTACT_OFFLINE_AFTER = 120

_IS_WINDOWS = platform.system().lower() == "windows"
# Suppress the console window Windows otherwise flashes for every
# subprocess call when running from a --windowed .exe.
_SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if _IS_WINDOWS else 0


# ----------------------------------------------------------------------
# Networking: messaging protocol
# ----------------------------------------------------------------------

def _envelope(anonymous, display_name, scope, msgtype):
    return {
        "type": "msg",
        "msgtype": msgtype,  # "text" | "image" | "voice"
        "scope": scope,       # "private" (that contact's thread) or "global" (shared broadcast thread)
        "anonymous": anonymous,
        "name": "Anonymous" if anonymous else (display_name or socket.gethostname()),
    }


def _send_payload(ip, payload, port, timeout):
    raw = json.dumps(payload).encode("utf-8")
    with socket.create_connection((ip, port), timeout=timeout) as s:
        s.sendall(raw)


def send_text(ip, text, anonymous=True, display_name=None, scope="private", port=APP_PORT, timeout=5):
    payload = _envelope(anonymous, display_name, scope, "text")
    payload["text"] = text
    _send_payload(ip, payload, port, timeout)


def send_image(ip, image_bytes, filename, anonymous=True, display_name=None, scope="private",
                port=APP_PORT, timeout=15):
    payload = _envelope(anonymous, display_name, scope, "image")
    payload["filename"] = filename
    payload["data_b64"] = base64.b64encode(image_bytes).decode("ascii")
    _send_payload(ip, payload, port, timeout)


def send_voice(ip, wav_bytes, duration, anonymous=True, display_name=None, scope="private",
                port=APP_PORT, timeout=15):
    payload = _envelope(anonymous, display_name, scope, "voice")
    payload["duration"] = duration
    payload["data_b64"] = base64.b64encode(wav_bytes).decode("ascii")
    _send_payload(ip, payload, port, timeout)


def prepare_image_bytes(path):
    """Resize/compress an arbitrary image file down to send-friendly JPEG bytes."""
    if not HAVE_PIL:
        raise RuntimeError("Pillow is not installed")
    img = Image.open(path)
    img = img.convert("RGB")
    img.thumbnail((MAX_IMAGE_DIM, MAX_IMAGE_DIM))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82)
    return buf.getvalue()


class AudioRecorder:
    """Simple start/stop microphone recorder -> WAV bytes (needs sounddevice + numpy)."""

    def __init__(self, samplerate=16000, channels=1):
        self.samplerate = samplerate
        self.channels = channels
        self._frames = []
        self._stream = None

    def start(self):
        if not HAVE_AUDIO:
            raise RuntimeError("sounddevice and numpy are required to record audio")
        if self._stream is not None:
            raise RuntimeError("A recording is already in progress")

        self._frames = []

        def callback(indata, frames, time_info, status):
            self._frames.append(indata.copy())

        stream = sd.InputStream(samplerate=self.samplerate, channels=self.channels,
                                dtype="int16", callback=callback)
        try:
            stream.start()
        except Exception:
            stream.close()
            raise
        self._stream = stream

    def stop(self):
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()
        if not self._frames:
            return b"", 0.0
        data = np.concatenate(self._frames, axis=0)
        duration = len(data) / self.samplerate
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(self.channels)
            wf.setsampwidth(2)  # int16
            wf.setframerate(self.samplerate)
            wf.writeframes(data.tobytes())
        return buf.getvalue(), duration


def check_listener_running(ip, port=APP_PORT, timeout=0.4):
    try:
        with socket.create_connection((ip, port), timeout=timeout) as s:
            s.sendall(json.dumps({"type": "ping"}).encode("utf-8"))
            return True
    except Exception:
        return False


class MessageListener(threading.Thread):
    """Background TCP server; calls on_message(sender_ip, parsed_json_dict)."""

    def __init__(self, on_message, port=APP_PORT):
        super().__init__(daemon=True)
        self.on_message = on_message
        self.port = port
        self._stop_event = threading.Event()

    def run(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.port))
        srv.listen(5)
        srv.settimeout(1.0)
        while not self._stop_event.is_set():
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.settimeout(10)  # images/voice clips are bigger than a text ping
                chunks = []
                while True:
                    chunk = conn.recv(65536)
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
                    self.on_message(addr[0], data)
            finally:
                conn.close()
        srv.close()

    def stop(self):
        self._stop_event.set()


class DiscoveryListener(threading.Thread):
    """UDP responder for discovering other LAN Chat instances."""

    def __init__(self, on_error=None, port=DISCOVERY_PORT):
        super().__init__(daemon=True)
        self.on_error = on_error
        self.port = port
        self._stop_event = threading.Event()

    def run(self):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as srv:
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                srv.bind(("", self.port))
                srv.settimeout(0.5)
                while not self._stop_event.is_set():
                    try:
                        raw, sender = srv.recvfrom(65535)
                    except socket.timeout:
                        continue
                    except OSError as e:
                        if not self._stop_event.is_set():
                            self._report_error(f"UDP discovery listener failed: {e}")
                        break

                    try:
                        request = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if not isinstance(request, dict) or request.get("type") != "DISCOVERY_REQUEST":
                        continue

                    response = {
                        "type": "DISCOVERY_RESPONSE",
                        "device_name": socket.gethostname(),
                        "tcp_port": APP_PORT,
                    }
                    try:
                        srv.sendto(json.dumps(response).encode("utf-8"), sender)
                    except OSError as e:
                        if not self._stop_event.is_set():
                            self._report_error(f"Could not send UDP discovery response: {e}")
        except OSError as e:
            if not self._stop_event.is_set():
                self._report_error(
                    f"UDP discovery is unavailable on port {self.port}: {e}. "
                    "Check firewall rules or whether another app is using the port."
                )

    def _report_error(self, message):
        if self.on_error is not None:
            self.on_error(message)

    def stop(self):
        self._stop_event.set()


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
    interface_cidr = get_interface_cidr(local_ip)
    if interface_cidr:
        return interface_cidr, local_ip
    parts = local_ip.split(".")
    return ".".join(parts[:3]) + ".0/24", local_ip


def get_interface_cidr(local_ip):
    """Find the selected IPv4 interface's prefix when the OS exposes it."""
    try:
        if _IS_WINDOWS:
            result = subprocess.run(
                ["ipconfig"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=_SUBPROCESS_FLAGS,
            )
            interface_ip = None
            for line in result.stdout.splitlines():
                ip_match = re.search(r"IPv4 Address[.\s]*:\s*([\d.]+)", line)
                if ip_match:
                    interface_ip = ip_match.group(1)
                mask_match = re.search(r"Subnet Mask[.\s]*:\s*([\d.]+)", line)
                if interface_ip == local_ip and mask_match:
                    interface = ipaddress.IPv4Interface(
                        f"{local_ip}/{mask_match.group(1)}"
                    )
                    return str(interface.network)
                if interface_ip is not None and line and not line[0].isspace():
                    interface_ip = None
        else:
            result = subprocess.run(
                ["ip", "-o", "-f", "inet", "addr", "show"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for match in re.finditer(r"\binet\s+(\d+\.\d+\.\d+\.\d+/\d+)", result.stdout):
                interface = ipaddress.IPv4Interface(match.group(1))
                if str(interface.ip) == local_ip:
                    return str(interface.network)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return None


def _discovery_broadcast_addresses(local_ip, cidr):
    addresses = []
    interface_cidr = get_interface_cidr(local_ip)
    candidates = [interface_cidr, cidr]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            network = ipaddress.ip_network(candidate, strict=False)
            if isinstance(network, ipaddress.IPv4Network) and network.prefixlen < 31:
                address = str(network.broadcast_address)
                if address not in addresses:
                    addresses.append(address)
        except ValueError:
            continue
    if "255.255.255.255" not in addresses:
        addresses.append("255.255.255.255")
    return addresses


def discover_devices(local_ip, cidr, timeout=DISCOVERY_TIMEOUT, log_cb=None):
    """Broadcast a discovery request and collect valid replies for a short window."""
    request = {
        "type": "DISCOVERY_REQUEST",
        "device_name": socket.gethostname(),
        "tcp_port": APP_PORT,
    }
    payload = json.dumps(request).encode("utf-8")
    devices = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        client.settimeout(0.25)
        for broadcast_ip in _discovery_broadcast_addresses(local_ip, cidr):
            try:
                client.sendto(payload, (broadcast_ip, DISCOVERY_PORT))
            except OSError as e:
                if log_cb:
                    log_cb(f"UDP discovery broadcast to {broadcast_ip} failed: {e}")

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw, sender = client.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError as e:
                if log_cb:
                    log_cb(f"UDP discovery receive failed: {e}")
                break

            try:
                response = json.loads(raw.decode("utf-8"))
                if not isinstance(response, dict) or response.get("type") != "DISCOVERY_RESPONSE":
                    continue
                device_name = response.get("device_name")
                tcp_port = response.get("tcp_port")
                if not isinstance(device_name, str) or not device_name.strip():
                    continue
                if isinstance(tcp_port, bool) or not isinstance(tcp_port, int):
                    continue
                if tcp_port != APP_PORT:
                    continue
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue

            devices[sender[0]] = {
                "ip": sender[0],
                "hostname": device_name.strip(),
                "tcp_port": tcp_port,
                "is_self": sender[0] == local_ip,
                "last_seen": time.time(),
                "discovered_by_udp": True,
            }
    if log_cb:
        log_cb(f"UDP discovery found {len(devices)} device(s).")
    return list(devices.values())


def ping(ip, fail_counter=None, fail_lock=None):
    """
    Returns True/False for a normal ping result. If launching ping.exe
    itself raises an exception (blocked by AV/security policy, missing
    binary, etc.) that's tracked separately via fail_counter so the
    caller can tell "nobody answered" apart from "couldn't even try."
    """
    param = "-n" if _IS_WINDOWS else "-c"
    timeout_flag = "-w" if _IS_WINDOWS else "-W"
    timeout_val = "500" if _IS_WINDOWS else "1"
    try:
        result = subprocess.run(["ping", param, "1", timeout_flag, timeout_val, ip],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
                                 creationflags=_SUBPROCESS_FLAGS)
        return result.returncode == 0
    except Exception as e:
        if fail_counter is not None and fail_lock is not None:
            with fail_lock:
                fail_counter["errors"] += 1
                fail_counter["last_error"] = str(e)
        return False


def sweep_subnet(cidr, progress_cb=None, log_cb=None):
    net = ipaddress.ip_network(cidr, strict=False)
    hosts = list(net.hosts())
    threads = []
    fail_counter = {"errors": 0, "last_error": ""}
    fail_lock = threading.Lock()

    for i, ip in enumerate(hosts):
        t = threading.Thread(target=ping, args=(str(ip), fail_counter, fail_lock), daemon=True)
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

    if log_cb and fail_counter["errors"] > 0:
        pct = fail_counter["errors"] * 100 // max(len(hosts), 1)
        log_cb(f"WARNING: ping.exe itself failed to launch on {fail_counter['errors']}/{len(hosts)} "
               f"attempts ({pct}%) - error: {fail_counter['last_error']}. "
               "If this is high, antivirus/endpoint protection or a restricted user account "
               "is likely blocking the scan on this machine, not an empty network.")


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
    sweep_subnet(cidr, progress_cb, log_cb=log_cb)
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

        # chats[key] = list of entry dicts; key is an IP, or GLOBAL_KEY.
        # entry = {"who":..., "kind": "text"/"image"/"voice", ...kind-specific fields}
        self.chats = {GLOBAL_KEY: []}
        # contacts[ip] = {"hostname":..., "reachable": "yes"/"no"/"self", "unread": bool}
        self.contacts = {}
        self.selected_key = GLOBAL_KEY
        self.global_unread = False
        self._media_photo_refs = []  # keeps PhotoImage objects alive while shown

        self.recorder = AudioRecorder() if HAVE_AUDIO else None
        self.is_recording = False
        self.recording_started_at = None
        self.recording_timer_id = None
        self.recording_auto_stop_id = None
        self.voice_status_var = tk.StringVar(value="")
        self._playing_voice_entry = None
        self._active_voice_button = None
        self._playback_started_at = None
        self._playback_timer_id = None
        self._playback_generation = 0

        self._build_ui()

        self.listener = MessageListener(self.on_message)
        self.listener.start()
        self.discovery_listener = DiscoveryListener(on_error=self.log)
        self.discovery_listener.start()
        self._contact_expiry_id = self.root.after(30_000, self._expire_stale_contacts)
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
        composer.pack(fill="x", padx=8, pady=8)

# Message input
        self.msg_entry = ttk.Entry(composer)
        self.msg_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.msg_entry.bind("<Return>", lambda e: self.send_current())

# Anonymous option
        self.anon_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            composer,
            text="Anonymous",
            variable=self.anon_var
            ).pack(side="left", padx=5)

# Image button
        self.image_btn = ttk.Button(
             composer,
             text="📷  Send image",
             command=self.pick_image,
             width=16,
             padding=(8, 6)
                )
        self.image_btn.pack(side="left", padx=(4, 4))

# Voice recording button
        self.voice_btn = ttk.Button(
            composer,
            text="🎙  Record voice",
            command=self.toggle_recording,
            width=17,
            padding=(8, 6)
                    )
        self.voice_btn.pack(side="left", padx=(0, 4))
        ttk.Label(
            composer,
            textvariable=self.voice_status_var,
            foreground="#c01c28",
            font=("Segoe UI", 10, "bold")
        ).pack(side="left", padx=(2, 6))

# Send button
        self.send_btn = ttk.Button(
            composer,
            text="  ➤ Send  ",
            command=self.send_current
            )
        self.send_btn.pack(side="left", padx=(0, 4))

# Check feature availability
        if not HAVE_PIL:
            self.image_btn.config(text="📷  Image unavailable", state="disabled")

        if not HAVE_AUDIO:
            self.voice_btn.config(text="🎙  Audio unavailable", state="disabled")


# Diagnostics log
        log_frame = ttk.LabelFrame(root, text="Diagnostics log", padding=4)
        log_frame.pack(fill="x", padx=8, pady=(0, 8))

        self.log_text = tk.Text(
                log_frame,
                height=4,
                wrap="word",
                state="disabled",
                font=("Consolas", 9)
                )
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
        self._active_voice_button = None
        self.transcript.delete("1.0", "end")
        self._media_photo_refs = []  # rebuilt below; old PhotoImages can be dropped now
        for entry in self.chats.get(key, []):
            who = entry.get("who", "?")
            tag = "me" if who == "You" else "them"
            self.transcript.insert("end", f"{who}: ", ("meta",))
            kind = entry.get("kind", "text")
            if kind == "text":
                self.transcript.insert("end", f"{entry.get('text', '')}\n", (tag,))
            elif kind == "image":
                self._insert_image(entry)
                self.transcript.insert("end", "\n")
            elif kind == "voice":
                self._insert_voice(entry)
                self.transcript.insert("end", "\n")
            else:
                self.transcript.insert("end", "[unsupported message]\n", (tag,))
        self.transcript.config(state="disabled")
        self.transcript.see("end")

    def _insert_image(self, entry):
        if not HAVE_PIL:
            self.transcript.insert("end", f"[image: {entry.get('filename', '?')} "
                                           "- install Pillow to view: pip install pillow]\n")
            return
        try:
            img = Image.open(io.BytesIO(entry["data"]))
            img.thumbnail((260, 260))
            photo = ImageTk.PhotoImage(img)
        except Exception:
            self.transcript.insert("end", "[could not display image]\n")
            return
        self._media_photo_refs.append(photo)  # prevent garbage collection
        lbl = tk.Label(self.transcript, image=photo, cursor="hand2", bd=1, relief="solid")
        data, filename = entry["data"], entry.get("filename", "image.jpg")
        lbl.bind("<Button-1>", lambda e: self._open_image_full(data, filename))
        self.transcript.window_create("end", window=lbl)

    def _open_image_full(self, data, filename):
        try:
            suffix = os.path.splitext(filename)[1] or ".jpg"
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            tmp.write(data)
            tmp.close()
            if _IS_WINDOWS:
                os.startfile(tmp.name)
            else:
                subprocess.run(["xdg-open", tmp.name])
        except Exception as e:
            messagebox.showerror("Open failed", str(e))

    def _insert_voice(self, entry):
        dur = self._voice_duration(entry)
        playing = self._playing_voice_entry is entry
        elapsed = time.monotonic() - self._playback_started_at if playing else 0
        btn = ttk.Button(
            self.transcript,
            text=self._voice_button_text(dur, playing, elapsed),
            command=lambda: self._play_voice(entry, btn),
            width=31,
            padding=(8, 5)
        )
        if playing:
            self._active_voice_button = btn
        self.transcript.window_create("end", window=btn)

    @staticmethod
    def _voice_duration(entry):
        try:
            duration = float(entry.get("duration", 0))
        except (TypeError, ValueError):
            return 0.0
        return duration if math.isfinite(duration) and duration > 0 else 0.0

    @staticmethod
    def _voice_button_text(duration, playing, elapsed=0):
        seconds = max(0, int(duration))
        duration_text = f"{seconds // 60}:{seconds % 60:02d}"
        if playing:
            elapsed_seconds = max(0, int(elapsed))
            return f"⏹  Stop voice  {elapsed_seconds // 60}:{elapsed_seconds % 60:02d} / {duration_text}"
        return f"▶  Play voice message  ({duration_text})"

    def _play_voice(self, entry, button):
        if not HAVE_AUDIO:
            messagebox.showinfo("Playback unavailable",
                                 "Install sounddevice + numpy to play voice messages:\n"
                                 "pip install sounddevice numpy")
            return

        if self._playing_voice_entry is entry:
            self._stop_voice_playback()
            return
        if self._playing_voice_entry is not None:
            self._stop_voice_playback()

        self._playback_generation += 1
        generation = self._playback_generation
        self._playing_voice_entry = entry
        self._active_voice_button = button
        self._playback_started_at = time.monotonic()
        self._update_playback_indicator()

        def worker():
            error = None
            try:
                buf = io.BytesIO(entry["data"])
                with wave.open(buf, "rb") as wf:
                    if wf.getsampwidth() != 2 or wf.getcomptype() != "NONE":
                        raise ValueError("Only uncompressed 16-bit PCM voice messages are supported")
                    framerate = wf.getframerate()
                    channels = wf.getnchannels()
                    raw = wf.readframes(wf.getnframes())
                if channels < 1 or framerate < 1 or not raw:
                    raise ValueError("The voice message contains no playable audio")
                arr = np.frombuffer(raw, dtype=np.int16)
                if channels > 1:
                    arr = arr.reshape(-1, channels)
                sd.play(arr, samplerate=framerate, blocking=True)
            except Exception as e:
                error = str(e)
            finally:
                self.root.after(0, lambda: self._finish_voice_playback(generation, error))

        threading.Thread(target=worker, daemon=True).start()

    def _update_playback_indicator(self):
        if self._playing_voice_entry is None or self._playback_started_at is None:
            return
        duration = self._voice_duration(self._playing_voice_entry)
        elapsed = time.monotonic() - self._playback_started_at
        if self._active_voice_button is not None:
            self._active_voice_button.config(
                text=self._voice_button_text(duration, True, elapsed)
            )
        self._playback_timer_id = self.root.after(250, self._update_playback_indicator)

    def _stop_voice_playback(self):
        self._playback_generation += 1
        try:
            sd.stop()
        except Exception as e:
            self.log(f"Could not stop voice playback: {e}")
        self._clear_playback_state()

    def _finish_voice_playback(self, generation, error=None):
        if generation != self._playback_generation:
            return
        self._clear_playback_state()
        if error:
            self.log(f"Voice playback failed: {error}")

    def _clear_playback_state(self):
        if self._playback_timer_id is not None:
            self.root.after_cancel(self._playback_timer_id)
            self._playback_timer_id = None
        button = self._active_voice_button
        entry = self._playing_voice_entry
        self._active_voice_button = None
        self._playing_voice_entry = None
        self._playback_started_at = None
        if button is not None and entry is not None:
            button.config(text=self._voice_button_text(self._voice_duration(entry), False))

    def _append_chat(self, key, entry):
        self.chats.setdefault(key, []).append(entry)
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
            self.root.after(0, lambda: self.status_var.set(f"Scanning... {done}/{total}"))

        try:
            _, local_ip = get_subnet_cidr()
            self.root.after(0, lambda: self.status_var.set("Discovering devices over UDP..."))
            try:
                discovered_devices = discover_devices(local_ip, cidr, log_cb=self.log)
            except OSError as e:
                self.log(f"UDP discovery failed; continuing with ARP/ping scan: {e}")
                discovered_devices = []
            devices_by_ip = {device["ip"]: device for device in discovered_devices}
            try:
                arp_devices = scan_network(cidr, local_ip, progress_cb=progress, log_cb=self.log)
            except Exception as e:
                self.log(f"ARP/ping scan failed; keeping UDP discovery results: {e}")
                arp_devices = []

            for device in arp_devices:
                devices_by_ip.setdefault(device["ip"], device)

            self.root.after(
                0, lambda: self.status_var.set("Checking which devices are reachable for messaging...")
            )
            for device in devices_by_ip.values():
                if device["is_self"]:
                    device["reachable"] = "self"
                else:
                    device["reachable"] = "yes" if check_listener_running(device["ip"]) else "no"
                    if device["reachable"] == "yes":
                        device.setdefault("last_seen", time.time())
            devices = sorted(
                devices_by_ip.values(),
                key=lambda device: tuple(int(part) for part in device["ip"].split(".")),
            )
            self.root.after(0, lambda: self._apply_scan_results(devices))
        except Exception as e:
            self.log(f"Network scan failed: {e}")
            error = str(e)
            self.root.after(0, lambda error=error: self._scan_failed(error))

    def _scan_failed(self, error):
        self.scan_btn.config(state="normal")
        self.status_var.set("Scan failed")
        messagebox.showerror("Network scan failed", error)

    def _apply_scan_results(self, devices):
        for d in devices:
            existing = self.contacts.get(d["ip"], {})
            contact = existing.copy()
            discovered_name = d.get("hostname")
            if discovered_name and discovered_name != "Unknown device":
                contact["hostname"] = discovered_name
            else:
                contact.setdefault("hostname", "Unknown device")
            contact["reachable"] = d["reachable"]
            contact["unread"] = existing.get("unread", False)
            contact["last_seen"] = d.get("last_seen", existing.get("last_seen"))
            self.contacts[d["ip"]] = contact
        self._refresh_contact_list()
        self.status_var.set(f"Found {len(devices)} device(s).")
        self.scan_btn.config(state="normal")

    def _expire_stale_contacts(self):
        now = time.time()
        changed = False
        for contact in self.contacts.values():
            last_seen = contact.get("last_seen")
            if (
                contact.get("reachable") == "yes"
                and isinstance(last_seen, (int, float))
                and now - last_seen > CONTACT_OFFLINE_AFTER
            ):
                contact["reachable"] = "no"
                changed = True
        if changed:
            self._refresh_contact_list()
        self._contact_expiry_id = self.root.after(30_000, self._expire_stale_contacts)

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
            self._append_chat(GLOBAL_KEY, {"who": "You", "kind": "text", "text": text})
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
            send_text(key, text, anonymous=anonymous, display_name=display_name, scope="private")
            self._append_chat(key, {"who": "You", "kind": "text", "text": text})
            self.msg_entry.delete(0, "end")
        except Exception as e:
            messagebox.showerror("Send failed", str(e))

    def _broadcast_worker(self, targets, text, anonymous, display_name):
        sent, failed = 0, 0
        for ip in targets:
            try:
                send_text(ip, text, anonymous=anonymous, display_name=display_name, scope="global")
                sent += 1
            except Exception:
                failed += 1
        self.log(f"Global message sent to {sent} device(s), failed for {failed}.")

    # -- Images / voice -----------------------------------------------------

    def pick_image(self):
        if not HAVE_PIL:
            messagebox.showinfo("Pillow required", "Install Pillow to send images:\npip install pillow")
            return
        path = filedialog.askopenfilename(
            title="Choose an image",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.gif *.bmp *.webp"), ("All files", "*.*")]
        )
        if not path:
            return
        try:
            data = prepare_image_bytes(path)
        except Exception as e:
            messagebox.showerror("Could not prepare image", str(e))
            return
        self._send_media("image", data, filename=os.path.basename(path))

    def toggle_recording(self):
        if not HAVE_AUDIO:
            messagebox.showinfo("Audio libraries required",
                                 "Install these to send voice messages:\npip install sounddevice numpy")
            return
        if not self.is_recording:
            try:
                self.recorder.start()
            except Exception as e:
                messagebox.showerror("Could not start recording", str(e))
                return
            self.is_recording = True
            self.recording_started_at = time.monotonic()
            self.voice_btn.config(text="⏹  Stop recording")
            self.voice_status_var.set("● REC 00:00")
            self._update_recording_indicator()
            self.recording_auto_stop_id = self.root.after(
                MAX_VOICE_SECONDS * 1000, self._auto_stop_recording
            )
        else:
            self._finish_recording()

    def _update_recording_indicator(self):
        if not self.is_recording or self.recording_started_at is None:
            return
        elapsed = min(int(time.monotonic() - self.recording_started_at), MAX_VOICE_SECONDS)
        self.voice_status_var.set(f"● REC {elapsed // 60:02d}:{elapsed % 60:02d}")
        self.recording_timer_id = self.root.after(250, self._update_recording_indicator)

    def _auto_stop_recording(self):
        if self.is_recording:
            self.log(f"Recording auto-stopped after {MAX_VOICE_SECONDS}s limit.")
            self._finish_recording()

    def _finish_recording(self):
        if not self.is_recording:
            return
        self.is_recording = False
        self._cancel_recording_timers()
        self.recording_started_at = None
        self.voice_btn.config(text="🎙  Record voice")
        self.voice_status_var.set("")
        try:
            data, duration = self.recorder.stop()
        except Exception as e:
            messagebox.showerror("Could not finish recording", str(e))
            return
        if not data or duration < 0.3:
            self.voice_status_var.set("Recording too short")
            return
        self._send_media("voice", data, duration=duration)

    def _cancel_recording_timers(self):
        for timer_id in (self.recording_timer_id, self.recording_auto_stop_id):
            if timer_id is not None:
                self.root.after_cancel(timer_id)
        self.recording_timer_id = None
        self.recording_auto_stop_id = None

    def _send_media(self, kind, data, filename=None, duration=None):
        key = self.selected_key
        anonymous = self.anon_var.get()
        display_name = self.name_var.get().strip() or socket.gethostname()

        entry = {"who": "You", "kind": kind, "data": data}
        if filename is not None:
            entry["filename"] = filename
        if duration is not None:
            entry["duration"] = duration

        if key == GLOBAL_KEY:
            targets = [ip for ip, c in self.contacts.items() if c["reachable"] == "yes"]
            if not targets:
                messagebox.showwarning("No reachable devices",
                                        "Scan the network first - no devices are currently marked reachable.")
                return
            self._append_chat(GLOBAL_KEY, entry)
            threading.Thread(target=self._broadcast_media_worker,
                              args=(targets, kind, data, filename, duration, anonymous, display_name),
                              daemon=True).start()
            return

        contact = self.contacts.get(key)
        if not contact or contact["reachable"] != "yes":
            messagebox.showerror("Not reachable", "This device isn't currently reachable.")
            return
        try:
            if kind == "image":
                send_image(key, data, filename or "image.jpg", anonymous=anonymous,
                           display_name=display_name, scope="private")
            else:
                send_voice(key, data, duration or 0, anonymous=anonymous,
                           display_name=display_name, scope="private")
            self._append_chat(key, entry)
        except Exception as e:
            messagebox.showerror("Send failed", str(e))

    def _broadcast_media_worker(self, targets, kind, data, filename, duration, anonymous, display_name):
        sent, failed = 0, 0
        for ip in targets:
            try:
                if kind == "image":
                    send_image(ip, data, filename or "image.jpg", anonymous=anonymous,
                               display_name=display_name, scope="global")
                else:
                    send_voice(ip, data, duration or 0, anonymous=anonymous,
                               display_name=display_name, scope="global")
                sent += 1
            except Exception:
                failed += 1
        self.log(f"Global {kind} sent to {sent} device(s), failed for {failed}.")

    # -- Receiving ----------------------------------------------------------

    def on_message(self, sender_ip, data):
        self.root.after(0, lambda: self._handle_incoming(sender_ip, data))

    def _handle_incoming(self, sender_ip, data):
        display_name = data.get("name", "Anonymous")
        scope = data.get("scope", "private")
        msgtype = data.get("msgtype", "text")

        # Track/refresh the sender as a contact either way, so they appear in
        # the list and can be replied to privately - but WHERE the message
        # itself is shown depends on scope.
        if sender_ip not in self.contacts:
            self.contacts[sender_ip] = {
                "hostname": display_name,
                "reachable": "yes",
                "unread": False,
                "last_seen": time.time(),
            }
        else:
            self.contacts[sender_ip]["reachable"] = "yes"
            self.contacts[sender_ip]["last_seen"] = time.time()
            if self.contacts[sender_ip].get("hostname") == "Unknown device":
                self.contacts[sender_ip]["hostname"] = display_name

        if msgtype == "text":
            entry = {"who": display_name, "kind": "text", "text": data.get("text", "")}
        elif msgtype == "image":
            try:
                img_bytes = base64.b64decode(data.get("data_b64", ""))
            except Exception:
                img_bytes = b""
            entry = {"who": display_name, "kind": "image", "data": img_bytes,
                     "filename": data.get("filename", "image.jpg")}
        elif msgtype == "voice":
            try:
                voice_bytes = base64.b64decode(data.get("data_b64", ""))
            except Exception:
                voice_bytes = b""
            entry = {"who": display_name, "kind": "voice", "data": voice_bytes,
                     "duration": data.get("duration", 0)}
        else:
            entry = {"who": display_name, "kind": "text", "text": "[unsupported message type]"}

        if scope == "global":
            self._append_chat(GLOBAL_KEY, entry)
            if self.selected_key != GLOBAL_KEY:
                self.global_unread = True
                self.root.bell()
        else:
            self._append_chat(sender_ip, entry)
            if self.selected_key != sender_ip:
                self.contacts[sender_ip]["unread"] = True
                self.root.bell()

        self._refresh_contact_list()
        self.root.deiconify()
        self.root.lift()

    def _on_close(self):
        if self.is_recording:
            self.is_recording = False
            self._cancel_recording_timers()
            try:
                self.recorder.stop()
            except Exception as e:
                self.log(f"Could not stop recording during shutdown: {e}")
        if self._playing_voice_entry is not None:
            self._stop_voice_playback()
        if self._contact_expiry_id is not None:
            self.root.after_cancel(self._contact_expiry_id)
            self._contact_expiry_id = None
        self.listener.stop()
        self.listener.join(timeout=1.5)
        self.discovery_listener.stop()
        self.discovery_listener.join(timeout=1.5)
        self.root.destroy()


def main():
    root = tk.Tk()
    ChatApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()