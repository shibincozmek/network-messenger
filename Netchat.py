#!/usr/bin/env python3
"""
LAN Chat - single-file anonymous messenger with a Global (everyone) thread.

Discovery is automatic: every few seconds the app announces itself on EVERY
network adapter of this PC (broadcast + direct UDP probe of every address in
each subnet) and lists only the devices that are actually running this app.

Each running copy has a unique DEVICE_ID, so a PC with several network
adapters (WiFi, VirtualBox, VMware, ...) shows up ONCE in the list, and your
own PC is never listed.

Global chat: a message sent there goes to every online device, and any
message anyone sends to Global shows up for everyone.

Only run this on a network you own or have permission to use.
Needs: Python 3 + tkinter. Optional: pillow (images), sounddevice + numpy (voice).
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
import uuid
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog

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
GLOBAL_KEY = "__global__"
MAX_IMAGE_DIM = 1024
MAX_VOICE_SECONDS = 30
DISCOVERY_TIMEOUT = 3.0

FAST_SCAN_INTERVAL_MS = 6_000     # how often we look for other devices
CONTACT_EXPIRY_CHECK_MS = 8_000
CONTACT_OFFLINE_AFTER = 20        # seconds of silence before a contact shows as offline
EDIT_DELETE_WINDOW_SECONDS = 120

# Unique id of THIS running copy. Used to recognise our own PC (even when it
# has many IP addresses) and to show another PC only once.
DEVICE_ID = uuid.uuid4().hex

_IS_WINDOWS = platform.system().lower() == "windows"
_SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if _IS_WINDOWS else 0


# ----------------------------------------------------------------------
# Messaging protocol
# ----------------------------------------------------------------------

def new_msg_id():
    return uuid.uuid4().hex


def _envelope(anonymous, display_name, scope, msgtype, msg_id):
    return {
        "type": "msg",
        "msgtype": msgtype,
        "scope": scope,
        "anonymous": anonymous,
        "name": "Anonymous" if anonymous else (display_name or socket.gethostname()),
        "device_id": DEVICE_ID,
        "host": socket.gethostname(),
        "msg_id": msg_id,
    }


def _send_payload(ip, payload, port, timeout):
    raw = json.dumps(payload).encode("utf-8")
    with socket.create_connection((ip, port), timeout=timeout) as s:
        s.sendall(raw)


def send_text(ip, text, anonymous=True, display_name=None, scope="private", port=APP_PORT, timeout=5,
              msg_id=None):
    payload = _envelope(anonymous, display_name, scope, "text", msg_id or new_msg_id())
    payload["text"] = text
    _send_payload(ip, payload, port, timeout)


def send_image(ip, image_bytes, filename, anonymous=True, display_name=None, scope="private",
               port=APP_PORT, timeout=15, msg_id=None):
    payload = _envelope(anonymous, display_name, scope, "image", msg_id or new_msg_id())
    payload["filename"] = filename
    payload["data_b64"] = base64.b64encode(image_bytes).decode("ascii")
    _send_payload(ip, payload, port, timeout)


def send_voice(ip, wav_bytes, duration, anonymous=True, display_name=None, scope="private",
               port=APP_PORT, timeout=15, msg_id=None):
    payload = _envelope(anonymous, display_name, scope, "voice", msg_id or new_msg_id())
    payload["duration"] = duration
    payload["data_b64"] = base64.b64encode(wav_bytes).decode("ascii")
    _send_payload(ip, payload, port, timeout)


def send_action(ip, action, msg_id, new_text=None, scope="private", port=APP_PORT, timeout=5):
    payload = {"type": "msg_action", "action": action, "msg_id": msg_id, "scope": scope}
    if new_text is not None:
        payload["new_text"] = new_text
    _send_payload(ip, payload, port, timeout)


def prepare_image_bytes(path):
    if not HAVE_PIL:
        raise RuntimeError("Pillow is not installed")
    img = Image.open(path)
    img = img.convert("RGB")
    img.thumbnail((MAX_IMAGE_DIM, MAX_IMAGE_DIM))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82)
    return buf.getvalue()


class AudioRecorder:
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
            wf.setsampwidth(2)
            wf.setframerate(self.samplerate)
            wf.writeframes(data.tobytes())
        return buf.getvalue(), duration


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
                conn.settimeout(10)
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
                if data.get("type") in ("msg", "msg_action"):
                    self.on_message(addr[0], data)
            except Exception:
                pass
            finally:
                conn.close()
        srv.close()

    def stop(self):
        self._stop_event.set()


class DiscoveryListener(threading.Thread):
    """UDP responder: answers 'who is running LAN Chat?' requests."""

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
                    except ConnectionResetError:
                        continue  # Windows: harmless ICMP "port unreachable" from an earlier reply
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
                    if request.get("device_id") == DEVICE_ID:
                        continue  # our own probe

                    response = {
                        "type": "DISCOVERY_RESPONSE",
                        "device_id": DEVICE_ID,
                        "device_name": socket.gethostname(),
                        "tcp_port": APP_PORT,
                    }
                    try:
                        srv.sendto(json.dumps(response).encode("utf-8"), sender)
                    except OSError:
                        pass
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
# Discovery
# ----------------------------------------------------------------------

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return None
    finally:
        s.close()


def get_all_ipv4_candidates():
    candidates = []
    try:
        if _IS_WINDOWS:
            out = subprocess.run(["ipconfig"], capture_output=True, text=True, timeout=5,
                                 creationflags=_SUBPROCESS_FLAGS).stdout
            for line in out.splitlines():
                m = re.search(r"IPv4[^:]*:\s*([\d.]+)", line)
                if m:
                    candidates.append(m.group(1))
        else:
            out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5).stdout
            candidates.extend(out.split())
    except Exception:
        pass
    return candidates


def get_interface_cidr(local_ip):
    """Find the subnet of the adapter that owns local_ip."""
    try:
        if _IS_WINDOWS:
            out = subprocess.run(["ipconfig"], capture_output=True, text=True, timeout=5,
                                 creationflags=_SUBPROCESS_FLAGS).stdout
            interface_ip = None
            for line in out.splitlines():
                ip_match = re.search(r"IPv4[^:]*:\s*([\d.]+)", line)
                if ip_match:
                    interface_ip = ip_match.group(1)
                mask_match = re.search(r"Subnet Mask[.\s]*:\s*([\d.]+)", line)
                if interface_ip == local_ip and mask_match:
                    return str(ipaddress.IPv4Interface(f"{local_ip}/{mask_match.group(1)}").network)
        else:
            out = subprocess.run(["ip", "-o", "-f", "inet", "addr", "show"],
                                 capture_output=True, text=True, timeout=5).stdout
            for match in re.finditer(r"\binet\s+(\d+\.\d+\.\d+\.\d+/\d+)", out):
                iface = ipaddress.IPv4Interface(match.group(1))
                if str(iface.ip) == local_ip:
                    return str(iface.network)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


def get_local_interfaces():
    """Return {ip: cidr} for EVERY private IPv4 address on this PC (all adapters)."""
    ips = get_all_ipv4_candidates()
    main_ip = get_local_ip()
    if main_ip:
        ips.append(main_ip)
    result = {}
    for ip in ips:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if not addr.is_private or addr.is_loopback or addr.is_link_local or ip in result:
            continue
        result[ip] = get_interface_cidr(ip) or ".".join(ip.split(".")[:3]) + ".0/24"
    return result


def discover_devices(timeout=DISCOVERY_TIMEOUT, log_cb=None):
    """
    Look for other copies of this app on every adapter's subnet.
    Returns a list of {"device_id", "hostname", "ips": [..]} - one entry per
    DEVICE (not per IP), never including this PC.
    """
    interfaces = get_local_interfaces()
    request = json.dumps({
        "type": "DISCOVERY_REQUEST",
        "device_id": DEVICE_ID,
        "device_name": socket.gethostname(),
        "tcp_port": APP_PORT,
    }).encode("utf-8")

    found = {}
    lock = threading.Lock()

    def probe(local_ip, cidr):
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            return
        targets = []
        if net.prefixlen < 31:
            targets.append(str(net.broadcast_address))
        if net.num_addresses <= 1024:
            # Direct probe of every address: works even when broadcast is blocked.
            targets += [str(h) for h in net.hosts() if str(h) != local_ip]
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.bind((local_ip, 0))  # send out of THIS adapter
                s.settimeout(0.25)
                for t in targets:
                    try:
                        s.sendto(request, (t, DISCOVERY_PORT))
                    except OSError:
                        pass
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    try:
                        raw, sender = s.recvfrom(65535)
                    except socket.timeout:
                        continue
                    except ConnectionResetError:
                        continue
                    except OSError:
                        break
                    try:
                        resp = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if not isinstance(resp, dict) or resp.get("type") != "DISCOVERY_RESPONSE":
                        continue
                    did, name, port = resp.get("device_id"), resp.get("device_name"), resp.get("tcp_port")
                    if not isinstance(did, str) or not did or did == DEVICE_ID:
                        continue
                    if not isinstance(name, str) or not name.strip() or port != APP_PORT:
                        continue
                    with lock:
                        d = found.setdefault(did, {"device_id": did, "hostname": name.strip(), "ips": []})
                        if sender[0] not in d["ips"]:
                            d["ips"].append(sender[0])
        except OSError as e:
            if log_cb:
                log_cb(f"Discovery on {local_ip} failed: {e}")

    threads = [threading.Thread(target=probe, args=(ip, cidr), daemon=True)
               for ip, cidr in interfaces.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout + 3)
    return list(found.values())


# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------

class ChatApp:
    def __init__(self, root):
        self.root = root
        root.title("LAN Chat")
        root.geometry("920x640")
        root.minsize(760, 520)

        # chats[key] = list of entries; key is a device_id or GLOBAL_KEY
        self.chats = {GLOBAL_KEY: []}
        # contacts[device_id] = {"hostname", "ip", "reachable": "yes"/"no", "unread", "last_seen"}
        self.contacts = {}
        self.selected_key = GLOBAL_KEY
        self.global_unread = False
        self._media_photo_refs = []

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

        self._fast_scan_id = None
        self._contact_expiry_id = None
        self._scan_running = False

        self._build_ui()

        self.listener = MessageListener(self.on_message)
        self.listener.start()
        self.discovery_listener = DiscoveryListener(on_error=self.log)
        self.discovery_listener.start()

        self.root.after(200, self._fast_scan_tick)
        self._contact_expiry_id = self.root.after(CONTACT_EXPIRY_CHECK_MS, self._expire_stale_contacts)

        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- UI construction --------------------------------------------------

    def _build_ui(self):
        root = self.root

        top = ttk.Frame(root, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="LAN Chat", font=("Segoe UI", 13, "bold")).pack(side="left")
        self.status_var = tk.StringVar(value="Searching for devices...")
        ttk.Label(top, textvariable=self.status_var, foreground="#666").pack(side="left", padx=15)

        name_row = ttk.Frame(root, padding=(8, 2))
        name_row.pack(fill="x")
        ttk.Label(name_row, text="Your name (used when not anonymous):").pack(side="left")
        self.name_var = tk.StringVar(value=socket.gethostname())
        ttk.Entry(name_row, textvariable=self.name_var, width=25).pack(side="left", padx=5)

        body = ttk.Frame(root)
        body.pack(fill="both", expand=True, padx=8, pady=8)

        left = ttk.Frame(body, width=260)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        ttk.Label(left, text="Devices running LAN Chat", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.contact_list = tk.Listbox(left, activestyle="none", font=("Segoe UI", 10),
                                       exportselection=False)
        self.contact_list.pack(fill="both", expand=True, pady=(4, 0))
        self.contact_list.bind("<<ListboxSelect>>", self._on_select_contact)
        self._contact_keys = []
        self._refresh_contact_list()

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))

        self.chat_title_var = tk.StringVar(value="Global (everyone running LAN Chat)")
        ttk.Label(right, textvariable=self.chat_title_var, font=("Segoe UI", 11, "bold")).pack(anchor="w")

        self.transcript = tk.Text(right, wrap="word", state="disabled", font=("Segoe UI", 10))
        self.transcript.pack(fill="both", expand=True, pady=(4, 6))

        composer = ttk.Frame(right)
        composer.pack(fill="x", padx=8, pady=8)

        # Enter sends; Shift+Enter inserts a newline.
        self.msg_entry = tk.Text(composer, height=3, wrap="word", font=("Segoe UI", 10))
        self.msg_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.msg_entry.bind("<Return>", self._on_composer_return)
        self.msg_entry.bind("<Shift-Return>", lambda e: None)

        self.anon_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(composer, text="Anonymous", variable=self.anon_var).pack(side="left", padx=5)

        self.image_btn = ttk.Button(composer, text="📷  Send image", command=self.pick_image,
                                    width=16, padding=(8, 6))
        self.image_btn.pack(side="left", padx=(4, 4))

        self.voice_btn = ttk.Button(composer, text="🎙  Record voice", command=self.toggle_recording,
                                    width=17, padding=(8, 6))
        self.voice_btn.pack(side="left", padx=(0, 4))
        ttk.Label(composer, textvariable=self.voice_status_var, foreground="#c01c28",
                  font=("Segoe UI", 10, "bold")).pack(side="left", padx=(2, 6))

        self.send_btn = ttk.Button(composer, text="  ➤ Send  ", command=self.send_current)
        self.send_btn.pack(side="left", padx=(0, 4))

        if not HAVE_PIL:
            self.image_btn.config(text="📷  Image unavailable", state="disabled")
        if not HAVE_AUDIO:
            self.voice_btn.config(text="🎙  Audio unavailable", state="disabled")

        log_frame = ttk.LabelFrame(root, text="Diagnostics log", padding=4)
        log_frame.pack(fill="x", padx=8, pady=(0, 8))
        self.log_text = tk.Text(log_frame, height=4, wrap="word", state="disabled", font=("Consolas", 9))
        self.log_text.pack(fill="x")

    # -- Contact list -----------------------------------------------------

    def _refresh_contact_list(self):
        self.contact_list.delete(0, "end")
        self._contact_keys = [GLOBAL_KEY]
        self.contact_list.insert("end", "🌐 Global (everyone)" + (" ●" if self.global_unread else ""))

        for did, c in sorted(self.contacts.items(), key=lambda kv: kv[1]["hostname"].lower()):
            marker = "🟢" if c["reachable"] == "yes" else "⚪"
            unread = " ●" if c.get("unread") else ""
            self.contact_list.insert("end", f"{marker} {c['hostname']} ({c['ip']}){unread}")
            self._contact_keys.append(did)

        if self.selected_key in self._contact_keys:
            self.contact_list.selection_set(self._contact_keys.index(self.selected_key))

    def _online_count(self):
        return sum(1 for c in self.contacts.values() if c["reachable"] == "yes")

    def _update_status(self):
        self.status_var.set(f"{self._online_count()} device(s) online")

    def _on_composer_return(self, event):
        self.send_current()
        return "break"

    def _on_select_contact(self, event):
        sel = self.contact_list.curselection()
        if not sel:
            return
        key = self._contact_keys[sel[0]]
        self.selected_key = key
        if key == GLOBAL_KEY:
            self.global_unread = False
        elif key in self.contacts:
            self.contacts[key]["unread"] = False
        self._refresh_contact_list()
        self._render_transcript()

    def _render_transcript(self):
        key = self.selected_key
        if key == GLOBAL_KEY:
            self.chat_title_var.set("Global (everyone running LAN Chat)")
        else:
            c = self.contacts.get(key, {"hostname": "Unknown device", "ip": "?"})
            self.chat_title_var.set(f"{c['hostname']} ({c['ip']})")

        self.transcript.config(state="normal")
        self._active_voice_button = None
        self.transcript.delete("1.0", "end")
        self._media_photo_refs = []
        for entry in self.chats.get(key, []):
            self._render_entry(entry)
            self.transcript.insert("end", "\n")
        self.transcript.config(state="disabled")
        self.transcript.see("end")

    def _render_entry(self, entry):
        who = entry.get("who", "?")
        deleted = entry.get("deleted", False)
        kind = entry.get("kind", "text")
        tag_color = "#1a5fb4" if who == "You" else "#1a1a1a"

        frame = tk.Frame(self.transcript)
        tk.Label(frame, text=f"{who}: ", fg="#888", font=("Segoe UI", 10)).pack(side="left", anchor="n")

        if deleted:
            tk.Label(frame, text="This message was deleted", fg="#999999",
                     font=("Segoe UI", 10, "italic")).pack(side="left")
        elif kind == "text":
            suffix = " (edited)" if entry.get("edited") else ""
            tk.Label(frame, text=entry.get("text", "") + suffix, fg=tag_color,
                     font=("Segoe UI", 10), justify="left", wraplength=520).pack(side="left")
        elif kind == "image":
            self._build_image_widget(frame, entry)
        elif kind == "voice":
            self._build_voice_widget(frame, entry)
        else:
            tk.Label(frame, text="[unsupported message]", fg=tag_color).pack(side="left")

        if who == "You" and not deleted:
            ellipsis = ttk.Button(frame, text="⋮", width=2,
                                  command=lambda: self._open_message_menu(entry, ellipsis))
            ellipsis.pack(side="left", padx=(6, 0))

        self.transcript.window_create("end", window=frame)

    def _open_message_menu(self, entry, widget):
        within_window = (time.time() - entry.get("ts", 0)) <= EDIT_DELETE_WINDOW_SECONDS
        can_edit = within_window and entry.get("kind") == "text" and bool(entry.get("msg_id"))
        can_delete_everyone = within_window and bool(entry.get("msg_id"))

        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Edit", command=lambda: self._edit_message(entry),
                         state="normal" if can_edit else "disabled")
        menu.add_command(label="Delete for Everyone", command=lambda: self._delete_message(entry, True),
                         state="normal" if can_delete_everyone else "disabled")
        menu.add_command(label="Delete for Me", command=lambda: self._delete_message(entry, False))
        if not within_window:
            menu.add_separator()
            menu.add_command(label="(Edit / Delete for Everyone expire after 2 minutes)", state="disabled")
        menu.tk_popup(widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height())

    def _edit_message(self, entry):
        new_text = simpledialog.askstring("Edit message", "Edit your message:",
                                          initialvalue=entry.get("text", ""), parent=self.root)
        if new_text is None:
            return
        new_text = new_text.strip()
        if not new_text or new_text == entry.get("text", ""):
            return
        entry["text"] = new_text
        entry["edited"] = True
        self._render_transcript()
        self._propagate_action("edit", entry, new_text=new_text)

    def _delete_message(self, entry, for_everyone):
        if for_everyone:
            entry["deleted"] = True
            entry["kind"] = "text"
            entry["text"] = "This message was deleted"
            self._render_transcript()
            self._propagate_action("delete", entry)
        else:
            key = self.selected_key
            self.chats[key] = [e for e in self.chats.get(key, []) if e is not entry]
            self._render_transcript()

    def _targets_for(self, key):
        """IPs to send to: every online device for Global, or just that one device."""
        if key == GLOBAL_KEY:
            return [c["ip"] for c in self.contacts.values() if c["reachable"] == "yes"]
        c = self.contacts.get(key)
        return [c["ip"]] if c else []

    def _propagate_action(self, action, entry, new_text=None):
        msg_id = entry.get("msg_id")
        if not msg_id:
            return
        key = self.selected_key
        scope = "global" if key == GLOBAL_KEY else "private"
        targets = self._targets_for(key)

        def worker():
            for ip in targets:
                try:
                    send_action(ip, action, msg_id, new_text=new_text, scope=scope)
                except Exception:
                    pass

        threading.Thread(target=worker, daemon=True).start()

    def _build_image_widget(self, frame, entry):
        if not HAVE_PIL:
            tk.Label(frame, text=f"[image: {entry.get('filename', '?')} "
                                 "- install Pillow to view: pip install pillow]").pack(side="left")
            return
        try:
            img = Image.open(io.BytesIO(entry["data"]))
            img.thumbnail((260, 260))
            photo = ImageTk.PhotoImage(img)
        except Exception:
            tk.Label(frame, text="[could not display image]").pack(side="left")
            return
        self._media_photo_refs.append(photo)
        lbl = tk.Label(frame, image=photo, cursor="hand2", bd=1, relief="solid")
        data, filename = entry["data"], entry.get("filename", "image.jpg")
        lbl.bind("<Button-1>", lambda e: self._open_image_full(data, filename))
        lbl.pack(side="left")

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

    def _build_voice_widget(self, frame, entry):
        dur = self._voice_duration(entry)
        playing = self._playing_voice_entry is entry
        elapsed = time.monotonic() - self._playback_started_at if playing else 0
        btn = ttk.Button(frame, text=self._voice_button_text(dur, playing, elapsed),
                         command=lambda: self._play_voice(entry, btn), width=31, padding=(8, 5))
        if playing:
            self._active_voice_button = btn
        btn.pack(side="left")

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
            e = max(0, int(elapsed))
            return f"⏹  Stop voice  {e // 60}:{e % 60:02d} / {duration_text}"
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
                with wave.open(io.BytesIO(entry["data"]), "rb") as wf:
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
            self._active_voice_button.config(text=self._voice_button_text(duration, True, elapsed))
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
            try:
                button.config(text=self._voice_button_text(self._voice_duration(entry), False))
            except tk.TclError:
                pass

    def _append_chat(self, key, entry):
        self.chats.setdefault(key, []).append(entry)
        if self.selected_key == key:
            self._render_transcript()

    def log(self, msg):
        def _write():
            self.log_text.config(state="normal")
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
            self.log_text.config(state="disabled")
        self.root.after(0, _write)

    # -- Automatic discovery ----------------------------------------------

    def _fast_scan_tick(self):
        if not self._scan_running:
            threading.Thread(target=self._scan_worker, daemon=True).start()
        self._fast_scan_id = self.root.after(FAST_SCAN_INTERVAL_MS, self._fast_scan_tick)

    def _scan_worker(self):
        self._scan_running = True
        try:
            devices = discover_devices(log_cb=self.log)
            self.root.after(0, lambda: self._apply_discovery_devices(devices))
        except Exception as e:
            self.log(f"Discovery failed: {e}")
        finally:
            self._scan_running = False

    def _apply_discovery_devices(self, devices):
        changed = False
        for d in devices:
            did = d["device_id"]
            existing = self.contacts.get(did)
            if existing is None:
                self.contacts[did] = {"hostname": d["hostname"], "ip": d["ips"][0], "reachable": "yes",
                                      "unread": False, "last_seen": time.time()}
                changed = True
                continue
            if existing["reachable"] != "yes":
                changed = True
            existing["reachable"] = "yes"
            existing["last_seen"] = time.time()
            # keep the current IP if it still answers, so the label doesn't flicker
            if existing["ip"] not in d["ips"]:
                existing["ip"] = d["ips"][0]
                changed = True
            if existing["hostname"] != d["hostname"]:
                existing["hostname"] = d["hostname"]
                changed = True
        if changed:
            self._refresh_contact_list()
        self._update_status()

    def _expire_stale_contacts(self):
        now = time.time()
        changed = False
        for c in self.contacts.values():
            if c["reachable"] == "yes" and now - c.get("last_seen", 0) > CONTACT_OFFLINE_AFTER:
                c["reachable"] = "no"
                changed = True
        if changed:
            self._refresh_contact_list()
            self._update_status()
        self._contact_expiry_id = self.root.after(CONTACT_EXPIRY_CHECK_MS, self._expire_stale_contacts)

    # -- Sending ------------------------------------------------------------

    def send_current(self):
        text = self.msg_entry.get("1.0", "end-1c").strip()
        if not text:
            return
        key = self.selected_key
        anonymous = self.anon_var.get()
        display_name = self.name_var.get().strip() or socket.gethostname()
        msg_id = new_msg_id()
        ts = time.time()

        if key == GLOBAL_KEY:
            targets = self._targets_for(GLOBAL_KEY)
            if not targets:
                messagebox.showwarning(
                    "No devices online",
                    "No other device running LAN Chat was found yet. Wait a few seconds, "
                    "and make sure the other PC has the app open and allows it through the firewall."
                )
                return
            self._append_chat(GLOBAL_KEY, {"who": "You", "kind": "text", "text": text,
                                           "msg_id": msg_id, "ts": ts})
            self.msg_entry.delete("1.0", "end")
            threading.Thread(target=self._broadcast_worker,
                             args=(targets, text, anonymous, display_name, msg_id), daemon=True).start()
            return

        contact = self.contacts.get(key)
        if not contact or contact["reachable"] != "yes":
            messagebox.showerror("Not reachable", "This device is offline right now.")
            return
        try:
            send_text(contact["ip"], text, anonymous=anonymous, display_name=display_name,
                      scope="private", msg_id=msg_id)
            self._append_chat(key, {"who": "You", "kind": "text", "text": text,
                                    "msg_id": msg_id, "ts": ts})
            self.msg_entry.delete("1.0", "end")
        except Exception as e:
            messagebox.showerror("Send failed", str(e))

    def _broadcast_worker(self, targets, text, anonymous, display_name, msg_id):
        sent, failed = 0, 0
        for ip in targets:
            try:
                send_text(ip, text, anonymous=anonymous, display_name=display_name,
                          scope="global", msg_id=msg_id)
                sent += 1
            except Exception:
                failed += 1
        self.log(f"Global message sent to {sent} device(s), failed for {failed}.")

    # -- Images / voice -------------------------------------------------------

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
            self.recording_auto_stop_id = self.root.after(MAX_VOICE_SECONDS * 1000,
                                                          self._auto_stop_recording)
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
        msg_id = new_msg_id()

        entry = {"who": "You", "kind": kind, "data": data, "msg_id": msg_id, "ts": time.time()}
        if filename is not None:
            entry["filename"] = filename
        if duration is not None:
            entry["duration"] = duration

        if key == GLOBAL_KEY:
            targets = self._targets_for(GLOBAL_KEY)
            if not targets:
                messagebox.showwarning("No devices online", "No other device running LAN Chat was found yet.")
                return
            self._append_chat(GLOBAL_KEY, entry)
            threading.Thread(target=self._broadcast_media_worker,
                             args=(targets, kind, data, filename, duration, anonymous, display_name, msg_id),
                             daemon=True).start()
            return

        contact = self.contacts.get(key)
        if not contact or contact["reachable"] != "yes":
            messagebox.showerror("Not reachable", "This device is offline right now.")
            return
        try:
            if kind == "image":
                send_image(contact["ip"], data, filename or "image.jpg", anonymous=anonymous,
                           display_name=display_name, scope="private", msg_id=msg_id)
            else:
                send_voice(contact["ip"], data, duration or 0, anonymous=anonymous,
                           display_name=display_name, scope="private", msg_id=msg_id)
            self._append_chat(key, entry)
        except Exception as e:
            messagebox.showerror("Send failed", str(e))

    def _broadcast_media_worker(self, targets, kind, data, filename, duration, anonymous, display_name, msg_id):
        sent, failed = 0, 0
        for ip in targets:
            try:
                if kind == "image":
                    send_image(ip, data, filename or "image.jpg", anonymous=anonymous,
                               display_name=display_name, scope="global", msg_id=msg_id)
                else:
                    send_voice(ip, data, duration or 0, anonymous=anonymous,
                               display_name=display_name, scope="global", msg_id=msg_id)
                sent += 1
            except Exception:
                failed += 1
        self.log(f"Global {kind} sent to {sent} device(s), failed for {failed}.")

    # -- Receiving --------------------------------------------------------------

    def on_message(self, sender_ip, data):
        self.root.after(0, lambda: self._dispatch_incoming(sender_ip, data))

    def _dispatch_incoming(self, sender_ip, data):
        if data.get("type") == "msg_action":
            self._handle_incoming_action(data)
        else:
            self._handle_incoming(sender_ip, data)

    def _handle_incoming_action(self, data):
        action = data.get("action")
        msg_id = data.get("msg_id")
        scope = data.get("scope", "private")
        if not msg_id:
            return
        keys_to_check = [GLOBAL_KEY] if scope == "global" else list(self.chats.keys())
        for key in keys_to_check:
            for entry in self.chats.get(key, []):
                if entry.get("msg_id") == msg_id:
                    if action == "edit":
                        entry["text"] = data.get("new_text", entry.get("text", ""))
                        entry["edited"] = True
                    elif action == "delete":
                        entry["deleted"] = True
                        entry["kind"] = "text"
                        entry["text"] = "This message was deleted"
                    if self.selected_key == key:
                        self._render_transcript()
                    return

    def _handle_incoming(self, sender_ip, data):
        display_name = data.get("name", "Anonymous")
        scope = data.get("scope", "private")
        msgtype = data.get("msgtype", "text")
        did = data.get("device_id") or f"ip:{sender_ip}"
        host = data.get("host") or display_name
        if did == DEVICE_ID:
            return

        # A message proves this device is online right now. Contacts are keyed by
        # device_id, so a PC with many IPs is still ONE entry.
        c = self.contacts.get(did)
        if c is None:
            self.contacts[did] = {"hostname": host, "ip": sender_ip, "reachable": "yes",
                                  "unread": False, "last_seen": time.time()}
        else:
            c["reachable"] = "yes"
            c["last_seen"] = time.time()
            c["ip"] = sender_ip

        msg_id = data.get("msg_id")
        ts = time.time()

        if msgtype == "text":
            entry = {"who": display_name, "kind": "text", "text": data.get("text", ""),
                     "msg_id": msg_id, "ts": ts}
        elif msgtype == "image":
            try:
                img_bytes = base64.b64decode(data.get("data_b64", ""))
            except Exception:
                img_bytes = b""
            entry = {"who": display_name, "kind": "image", "data": img_bytes,
                     "filename": data.get("filename", "image.jpg"), "msg_id": msg_id, "ts": ts}
        elif msgtype == "voice":
            try:
                voice_bytes = base64.b64decode(data.get("data_b64", ""))
            except Exception:
                voice_bytes = b""
            entry = {"who": display_name, "kind": "voice", "data": voice_bytes,
                     "duration": data.get("duration", 0), "msg_id": msg_id, "ts": ts}
        else:
            entry = {"who": display_name, "kind": "text", "text": "[unsupported message type]",
                     "msg_id": msg_id, "ts": ts}

        if scope == "global":
            self._append_chat(GLOBAL_KEY, entry)
            if self.selected_key != GLOBAL_KEY:
                self.global_unread = True
                self.root.bell()
        else:
            self._append_chat(did, entry)
            if self.selected_key != did:
                self.contacts[did]["unread"] = True
                self.root.bell()

        self._refresh_contact_list()
        self._update_status()
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
        for timer_id in (self._contact_expiry_id, self._fast_scan_id):
            if timer_id is not None:
                self.root.after_cancel(timer_id)
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