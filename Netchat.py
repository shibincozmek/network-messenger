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

Global video call: Meet/Zoom style room with camera + microphone, mute and
camera on/off buttons. Joining puts you in the room and shows your own video.

Only run this on a network you own or have permission to use.
Needs: Python 3 + tkinter. Optional: pillow (images), sounddevice + numpy (voice + call audio),
opencv-python (video calling).
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
import struct
import sys
import queue
import traceback
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
except Exception:  # ImportError, or OSError if the PortAudio library is missing
    HAVE_AUDIO = False

# Catch ANY error (not just ImportError): a broken OpenCV bundle inside the .exe
# can fail with other exceptions, and we want to show the real reason.
CV2_ERROR = ""
CV2_TB = ""
try:
    import cv2
    HAVE_CV2 = True
except Exception as _e:
    HAVE_CV2 = False
    CV2_ERROR = f"{type(_e).__name__}: {_e}"
    CV2_TB = traceback.format_exc()

APP_PORT = 50505
DISCOVERY_PORT = 50506
VIDEO_PORT = 50507
GLOBAL_KEY = "__global__"
VIDEO_KEY = "__video__"
MAX_IMAGE_DIM = 1024
MAX_VOICE_SECONDS = 30
DISCOVERY_TIMEOUT = 3.0

FAST_SCAN_INTERVAL_MS = 6_000     # how often we look for other devices
CONTACT_EXPIRY_CHECK_MS = 8_000
CONTACT_OFFLINE_AFTER = 20        # seconds of silence before a contact shows as offline
EDIT_DELETE_WINDOW_SECONDS = 120

VIDEO_FPS = 8                      # modest frame rate - keeps LAN bandwidth and CPU sane
VIDEO_MAX_DIM = 320                # longest side of each video frame sent over the wire
VIDEO_JPEG_QUALITY = 55
VIDEO_PEER_RECONNECT_MS = 3_000    # how often we retry connecting to in-call peers we're missing

AUDIO_RATE = 16000                 # call audio: 16 kHz mono 16-bit
AUDIO_BLOCK = 640                  # 40 ms of audio per packet

# Unique id of THIS running copy. Used to recognise our own PC (even when it
# has many IP addresses) and to show another PC only once.
DEVICE_ID = uuid.uuid4().hex

EMOJIS = ("😀 😃 😄 😁 😆 😅 😂 🤣 😊 😇 🙂 😉 😍 🥰 😘 😋 😎 🤩 🥳 😏 😒 😞 😔 😟 😕 🙁 😣 😢 😭 😤 😠 😡 "
          "🤯 😳 🥵 🥶 😱 😨 😰 🤔 🤗 🤫 😐 😑 😶 🙄 😴 🤤 😷 🤒 🤕 🤢 🥴 😵 🤠 😈 💀 👻 🤖 💩 "
          "👍 👎 👌 ✌️ 🤞 🤟 🤘 👏 🙌 🙏 💪 👋 🤝 ✋ 👀 🧠 ❤️ 🧡 💛 💚 💙 💜 🖤 💔 💯 🔥 ✨ ⭐ 🎉 🎁 "
          "☕ 🍕 🍔 🍟 🍰 🍎 🎵 🎮 📷 💻 📱 ✅ ❌ ⚠️ ❓ ❗").split()

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

    def __init__(self, on_error=None, port=DISCOVERY_PORT, video_status_cb=None):
        super().__init__(daemon=True)
        self.on_error = on_error
        self.port = port
        self.video_status_cb = video_status_cb or (lambda: False)
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
                        "in_video_call": bool(self.video_status_cb()),
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
    Returns a list of {"device_id", "hostname", "ips": [..], "in_video_call": bool} -
    one entry per DEVICE (not per IP), never including this PC.
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
                        d = found.setdefault(did, {"device_id": did, "hostname": name.strip(), "ips": [],
                                                    "in_video_call": False})
                        if sender[0] not in d["ips"]:
                            d["ips"].append(sender[0])
                        if resp.get("in_video_call"):
                            d["in_video_call"] = True
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
# Video calling
# ----------------------------------------------------------------------

def _recv_exact(sock, n):
    """Read exactly n bytes or raise ConnectionError."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return buf


def _send_framed(sock, payload_bytes):
    sock.sendall(struct.pack(">I", len(payload_bytes)) + payload_bytes)


def open_camera():
    """Try several camera indexes and Windows backends. Returns a working capture or None."""
    backends = [cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY] if _IS_WINDOWS else [cv2.CAP_ANY]
    for index in range(4):
        for backend in backends:
            try:
                cap = cv2.VideoCapture(index, backend)
            except Exception:
                continue
            if cap.isOpened():
                ok, _ = cap.read()   # make sure it really delivers frames
                if ok:
                    return cap
            cap.release()
    return None


class VideoCallListener(threading.Thread):
    """
    Always-on TCP server for incoming call streams. Each connecting peer sends
    one small JSON header ({"device_id","hostname"}) then a continuous stream
    of length-prefixed packets. The first byte of each packet is its type:
        b"V" = video (JPEG bytes; EMPTY body means the camera is off)
        b"A" = audio (16-bit mono PCM)
        b"K" = keepalive
    We keep the latest video frame per peer in a shared dict, and hand audio
    to the on_audio callback.
    """

    def __init__(self, frames_dict, frames_lock, on_peer_seen, on_audio=None, port=VIDEO_PORT):
        super().__init__(daemon=True)
        self.frames = frames_dict
        self.frames_lock = frames_lock
        self.on_peer_seen = on_peer_seen
        self.on_audio = on_audio
        self.port = port
        self._stop_event = threading.Event()

    def run(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.port))
        srv.listen(8)
        srv.settimeout(1.0)
        while not self._stop_event.is_set():
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_peer, args=(conn,), daemon=True).start()
        srv.close()

    def _handle_peer(self, conn):
        device_id = None
        try:
            conn.settimeout(10)
            header_len = struct.unpack(">I", _recv_exact(conn, 4))[0]
            header = json.loads(_recv_exact(conn, header_len).decode("utf-8"))
            device_id = header.get("device_id")
            hostname = header.get("hostname", "?")
            if self.on_peer_seen:
                self.on_peer_seen(device_id, hostname)
            conn.settimeout(15)  # the sender sends a keepalive every second
            while not self._stop_event.is_set():
                frame_len = struct.unpack(">I", _recv_exact(conn, 4))[0]
                if frame_len < 1 or frame_len > 5_000_000:  # sanity cap
                    break
                packet = _recv_exact(conn, frame_len)
                kind, body = packet[:1], packet[1:]
                if kind == b"V":
                    with self.frames_lock:
                        self.frames[device_id] = body
                elif kind == b"A" and self.on_audio:
                    self.on_audio(device_id, body)
                # b"K" = keepalive, nothing to do
        except Exception:
            pass
        finally:
            conn.close()
            if device_id is not None:
                with self.frames_lock:
                    self.frames.pop(device_id, None)

    def stop(self):
        self._stop_event.set()


# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------

class ChatApp:
    def __init__(self, root):
        self.root = root
        root.title("LAN Chat")
        root.geometry("920x640")
        root.minsize(560, 460)

        # chats[key] = list of entries; key is a device_id or GLOBAL_KEY
        self.chats = {GLOBAL_KEY: []}
        # contacts[device_id] = {"hostname", "ip", "reachable": "yes"/"no", "unread", "last_seen",
        #                         "in_video_call": bool}
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
        self.pending_voice = None   # (wav_bytes, duration) recorded but NOT sent yet
        self._emoji_win = None
        self._playing_voice_entry = None
        self._active_voice_button = None
        self._playback_started_at = None
        self._playback_timer_id = None
        self._playback_generation = 0

        self._fast_scan_id = None
        self._contact_expiry_id = None
        self._scan_running = False

        # -- video calling state --
        self.in_video_call = False
        self.video_capture = None
        self.video_send_thread_stop = threading.Event()
        self.video_send_thread = None
        self.video_peers_out = {}   # device_id -> socket (outgoing stream to that peer)
        self.video_peers_lock = threading.Lock()
        self.video_send_lock = threading.Lock()   # one writer at a time on the sockets
        self.video_frames_in = {}   # device_id -> latest JPEG bytes received from them
        self.video_frames_lock = threading.Lock()
        self.video_photo_refs = {}  # device_id -> PhotoImage (keeps them alive in the grid)
        self.video_tiles = {}       # device_id -> dict of widgets in the call window
        self.call_window = None
        self._video_reconnect_id = None
        self._video_grid_refresh_id = None

        # -- call audio / mic / camera state --
        self.mic_on = True
        self.camera_on = True
        self.mic_stream = None
        self.out_stream = None
        self.audio_q = queue.Queue(maxsize=50)   # mic chunks waiting to be sent
        self.audio_bufs = {}                      # device_id -> bytearray of PCM to play
        self.audio_lock = threading.Lock()
        self.mic_level = 0
        self._mic_rate = AUDIO_RATE
        self._out_rate = AUDIO_RATE
        self.mic_level_var = None
        self.mic_btn = None
        self.cam_btn = None

        self._build_ui()

        self.listener = MessageListener(self.on_message)
        self.listener.start()
        self.discovery_listener = DiscoveryListener(on_error=self.log,
                                                      video_status_cb=lambda: self.in_video_call)
        self.discovery_listener.start()
        self.video_listener = VideoCallListener(self.video_frames_in, self.video_frames_lock,
                                                  on_peer_seen=self._on_video_peer_seen,
                                                  on_audio=self._on_audio_in)
        self.video_listener.start()

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

        # Composer is packed at the BOTTOM first, so when the window is small the
        # chat area shrinks instead of the buttons being cut off. Two rows:
        #   row 1: emoji | message box | Send        row 2: options + media buttons
        composer = ttk.Frame(right)
        composer.pack(side="bottom", fill="x", padx=4, pady=(0, 4), before=self.transcript)

        input_row = ttk.Frame(composer)
        input_row.pack(fill="x")
        buttons_row = ttk.Frame(composer)
        buttons_row.pack(fill="x", pady=(6, 0))

        self.emoji_btn = tk.Button(input_row, text="😊", font=("Segoe UI Emoji", 14), relief="flat",
                                   cursor="hand2", command=self.toggle_emoji_picker)
        self.emoji_btn.pack(side="left", padx=(0, 6))

        # Send is packed before the text box so it always keeps its space.
        self.send_btn = ttk.Button(input_row, text="  ➤ Send  ", command=self.send_current,
                                   padding=(8, 6))
        self.send_btn.pack(side="right", padx=(8, 0))

        # Enter sends; Shift+Enter inserts a newline.
        self.msg_entry = tk.Text(input_row, height=3, width=10, wrap="word", font=("Segoe UI", 10))
        self.msg_entry.pack(side="left", fill="x", expand=True)
        self.msg_entry.bind("<Return>", self._on_composer_return)
        self.msg_entry.bind("<Shift-Return>", lambda e: None)

        self.anon_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(buttons_row, text="Anonymous", variable=self.anon_var).pack(side="left", padx=(0, 8))

        self.image_btn = ttk.Button(buttons_row, text="📷  Send image", command=self.pick_image,
                                    width=16, padding=(8, 4))
        self.image_btn.pack(side="left", padx=(0, 4))

        self.voice_btn = ttk.Button(buttons_row, text="🎙  Record voice", command=self.toggle_recording,
                                    width=17, padding=(8, 4))
        self.voice_btn.pack(side="left", padx=(0, 4))
        ttk.Label(buttons_row, textvariable=self.voice_status_var, foreground="#c01c28",
                  font=("Segoe UI", 10, "bold")).pack(side="left", padx=(2, 6))

        # shown only while a recorded voice message is waiting to be sent
        self.discard_btn = ttk.Button(buttons_row, text="✕ Discard", command=self._discard_pending_voice)

        if not HAVE_PIL:
            self.image_btn.config(text="📷  Image unavailable", state="disabled")
        if not HAVE_AUDIO:
            self.voice_btn.config(text="🎙  Audio unavailable", state="disabled")

        log_frame = ttk.LabelFrame(root, text="Diagnostics log", padding=4)
        log_frame.pack(fill="x", padx=8, pady=(0, 8))
        self.log_text = tk.Text(log_frame, height=4, wrap="word", state="disabled", font=("Consolas", 9))
        self.log_text.pack(fill="x")

    # -- Contact list -----------------------------------------------------

    def _video_available_count(self):
        return sum(1 for c in self.contacts.values()
                   if c.get("in_video_call") and c.get("reachable") == "yes")

    def _update_video_room_label(self):
        # Keep the list row's live count in sync, and keep the call window's
        # tiles in sync with who's currently in the room, even if neither
        # changed via a click just now.
        self._refresh_contact_list()
        if self.call_window is not None and self.call_window.winfo_exists():
            self._refresh_call_tiles()

    def _refresh_contact_list(self):
        self.contact_list.delete(0, "end")
        self._contact_keys = [GLOBAL_KEY, VIDEO_KEY]
        self.contact_list.insert("end", "🌐 Global (everyone)" + (" ●" if self.global_unread else ""))
        n = self._video_available_count()
        label = f"📹 Global Video Call ({n} in call)" if n else "📹 Global Video Call (start one)"
        if self.in_video_call:
            label = "📹 Global Video Call (you're in it)"
        self.contact_list.insert("end", label)

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
        if key == VIDEO_KEY:
            # Not a chat thread - clicking it opens the call room, then the
            # list selection snaps back to whatever chat was actually open.
            self._open_call_window()
            self._refresh_contact_list()
            return
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
        self._scroll_to_bottom()

    def _scroll_to_bottom(self):
        # Messages are embedded widgets whose size is only known after layout, so
        # scrolling right away lands in the wrong place (the top). Lay out first,
        # then scroll, and once more shortly after to catch late image sizing.
        self.transcript.update_idletasks()
        self.transcript.yview_moveto(1.0)
        self.root.after(30, lambda: self.transcript.yview_moveto(1.0))

    def _render_entry(self, entry):
        who = entry.get("who", "?")
        deleted = entry.get("deleted", False)
        kind = entry.get("kind", "text")
        tag_color = "#1a5fb4" if who == "You" else "#1a1a1a"

        bg = self.transcript.cget("bg")  # same colour as the chat area = no visible box
        frame = tk.Frame(self.transcript, bg=bg)
        tk.Label(frame, text=f"{who}: ", fg="#888", bg=bg, font=("Segoe UI", 10)).pack(side="left", anchor="n")

        if deleted:
            tk.Label(frame, text="This message was deleted", fg="#999999", bg=bg,
                     font=("Segoe UI", 10, "italic")).pack(side="left")
        elif kind == "text":
            suffix = " (edited)" if entry.get("edited") else ""
            tk.Label(frame, text=entry.get("text", "") + suffix, fg=tag_color, bg=bg,
                     font=("Segoe UI", 10), justify="left", wraplength=520).pack(side="left")
        elif kind == "image":
            self._build_image_widget(frame, entry)
        elif kind == "voice":
            self._build_voice_widget(frame, entry)
        else:
            tk.Label(frame, text="[unsupported message]", fg=tag_color, bg=bg).pack(side="left")

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
                                 "- install Pillow to view: pip install pillow]",
                     bg=self.transcript.cget("bg")).pack(side="left")
            return
        try:
            img = Image.open(io.BytesIO(entry["data"]))
            img.thumbnail((260, 260))
            photo = ImageTk.PhotoImage(img)
        except Exception:
            tk.Label(frame, text="[could not display image]", bg=self.transcript.cget("bg")).pack(side="left")
            return
        self._media_photo_refs.append(photo)
        lbl = tk.Label(frame, image=photo, cursor="hand2", bd=1, relief="solid", bg=self.transcript.cget("bg"))
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
                                      "unread": False, "last_seen": time.time(),
                                      "in_video_call": d.get("in_video_call", False)}
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
            if existing.get("in_video_call", False) != d.get("in_video_call", False):
                existing["in_video_call"] = d.get("in_video_call", False)
                changed = True
        if changed:
            self._refresh_contact_list()
        self._update_status()
        self._update_video_room_label()

    def _expire_stale_contacts(self):
        now = time.time()
        changed = False
        for c in self.contacts.values():
            if c["reachable"] == "yes" and now - c.get("last_seen", 0) > CONTACT_OFFLINE_AFTER:
                c["reachable"] = "no"
                c["in_video_call"] = False
                changed = True
        if changed:
            self._refresh_contact_list()
            self._update_status()
            self._update_video_room_label()
        self._contact_expiry_id = self.root.after(CONTACT_EXPIRY_CHECK_MS, self._expire_stale_contacts)

    # -- Video calling --------------------------------------------------------
    #
    # Design, kept deliberately simple for a LAN demo rather than a real
    # conferencing product:
    #   - "in_video_call" is just a boolean every device announces about
    #     itself in its existing discovery heartbeat (piggybacking on the
    #     heartbeat that already drives online/offline for chat), so the
    #     room's participant count updates the same way the contact list does.
    #   - Everyone who's "in the room" connects directly to everyone else
    #     who's in the room (a full mesh) - fine for a handful of devices on
    #     one LAN, not meant to scale to a large call.
    #   - Video (JPEG frames), audio (PCM chunks) and keepalives all go over
    #     one persistent TCP connection per peer, as length-prefixed packets
    #     whose first byte is the type (V / A / K).

    def _on_video_peer_seen(self, device_id, hostname):
        # Called from the listener thread the moment a peer's stream connects,
        # so their tile can appear even before the next discovery tick.
        def apply():
            if device_id in self.contacts:
                self.contacts[device_id]["in_video_call"] = True  # so we connect back right away
            if self.call_window is not None and self.call_window.winfo_exists():
                self._ensure_call_tile(device_id, hostname)
        self.root.after(0, apply)

    def _open_call_window(self):
        if self.call_window is not None and self.call_window.winfo_exists():
            self.call_window.lift()
            self.call_window.focus_force()
            return

        win = tk.Toplevel(self.root)
        win.title("Global Video Call")
        win.geometry("720x580")
        win.protocol("WM_DELETE_WINDOW", self._close_call_window)
        self.call_window = win

        top = ttk.Frame(win, padding=8)
        top.pack(fill="x")
        self.call_status_var = tk.StringVar(value="Not in the call yet.")
        ttk.Label(top, textvariable=self.call_status_var).pack(side="left")
        self.mic_level_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.mic_level_var, foreground="#1a7f37").pack(side="left", padx=10)
        self.join_btn = ttk.Button(top, text="📹 Join Call", command=self._join_video_call)
        self.join_btn.pack(side="right")
        self.leave_btn = ttk.Button(top, text="🚪 Leave Call", command=self._leave_video_call)
        self.mic_btn = ttk.Button(top, text="🎙 Mute", command=self._toggle_mic)
        self.cam_btn = ttk.Button(top, text="📷 Camera off", command=self._toggle_camera)
        if not HAVE_CV2 or not HAVE_PIL:
            self.join_btn.config(state="disabled")
            missing = [n for n, ok in (("opencv-python", HAVE_CV2), ("pillow", HAVE_PIL)) if not ok]
            msg = "Video call unavailable - " + " and ".join(missing) + " could not be loaded."
            if CV2_ERROR:
                msg += f"\nReason: {CV2_ERROR}"
            if getattr(sys, "frozen", False):
                msg += "\n(Rebuild the .exe with the latest build_exe.bat.)"
            else:
                msg += "\n(pip install opencv-python pillow)"
            self.call_status_var.set(msg)
            self.log(msg.replace("\n", " "))
            if CV2_TB:
                self.log(CV2_TB)

        self.call_grid = ttk.Frame(win, padding=8)
        self.call_grid.pack(fill="both", expand=True)
        self.video_tiles = {}

        # If we're still in a call from an earlier window, restore the buttons.
        if self.in_video_call:
            self.join_btn.pack_forget()
            self.leave_btn.pack(side="right")
            self.cam_btn.pack(side="right", padx=4)
            self.mic_btn.pack(side="right")
            if not HAVE_AUDIO:
                self.mic_btn.config(state="disabled")
            self._update_call_buttons()
            self.call_status_var.set("You're in the call.")

        self._refresh_call_tiles()
        self._video_grid_refresh_id = self.root.after(1000 // VIDEO_FPS, self._video_grid_tick)

    def _close_call_window(self):
        if self.in_video_call:
            self._leave_video_call()
        if self._video_grid_refresh_id is not None:
            self.root.after_cancel(self._video_grid_refresh_id)
            self._video_grid_refresh_id = None
        if self.call_window is not None:
            self.call_window.destroy()
        self.call_window = None
        self.mic_btn = None
        self.cam_btn = None

    def _ensure_call_tile(self, device_id, hostname):
        if device_id in self.video_tiles or self.call_window is None or not self.call_window.winfo_exists():
            return
        # Tile is a bit bigger than the 320px frames so nothing gets cropped.
        cell = tk.Frame(self.call_grid, bg="#1e1e1e", width=330, height=260)
        cell.pack_propagate(False)
        img_label = tk.Label(cell, bg="#1e1e1e", text="(connecting...)", fg="#aaa")
        img_label.pack(fill="both", expand=True)
        name_label = tk.Label(cell, text=hostname, bg="#333", fg="white", font=("Segoe UI", 9))
        name_label.pack(fill="x", side="bottom")
        self.video_tiles[device_id] = {"cell": cell, "img": img_label, "name": name_label}
        self._relayout_call_grid()

    def _remove_call_tile(self, device_id):
        tile = self.video_tiles.pop(device_id, None)
        if tile:
            tile["cell"].destroy()
            self.video_photo_refs.pop(device_id, None)
            self._relayout_call_grid()

    def _relayout_call_grid(self):
        keys = list(self.video_tiles.keys())
        cols = max(1, int(math.ceil(math.sqrt(max(len(keys), 1)))))
        for i, did in enumerate(keys):
            r, c = divmod(i, cols)
            self.video_tiles[did]["cell"].grid(row=r, column=c, padx=4, pady=4, sticky="nsew")
        for i in range(cols):
            self.call_grid.grid_columnconfigure(i, weight=1)

    def _refresh_call_tiles(self):
        """Make the grid match who's in the room (including YOU)."""
        if self.call_window is None or not self.call_window.winfo_exists():
            return
        wanted = {did for did, c in self.contacts.items()
                  if c.get("in_video_call") and c.get("reachable") == "yes"}
        if self.in_video_call:
            wanted.add(DEVICE_ID)  # your own PC is never in self.contacts - always show yourself
        with self.video_frames_lock:
            wanted |= set(self.video_frames_in.keys())  # anyone actually streaming to us
        for did in list(self.video_tiles.keys()):
            if did not in wanted:
                self._remove_call_tile(did)
        for did in wanted:
            if did == DEVICE_ID:
                name = (self.name_var.get().strip() or "You") + " (You)"
            else:
                name = self.contacts.get(did, {}).get("hostname", "Participant")
            self._ensure_call_tile(did, name)

    def _video_grid_tick(self):
        """Repaint each tile from the latest frame bytes the listener captured."""
        if self.call_window is not None and self.call_window.winfo_exists():
            with self.video_frames_lock:
                snapshot = dict(self.video_frames_in)
            # live mic level meter, so you can SEE whether your mic is picking you up
            if self.mic_level_var is not None:
                if not self.in_video_call:
                    self.mic_level_var.set("")
                elif self.mic_stream is None:
                    self.mic_level_var.set("🎙 no microphone")
                elif not self.mic_on:
                    self.mic_level_var.set("🔇 muted")
                else:
                    bars = min(10, int(self.mic_level / 800))
                    self.mic_level_var.set("🎙 " + "▮" * bars + "▯" * (10 - bars))
            for did, jpeg_bytes in snapshot.items():
                tile = self.video_tiles.get(did)
                if tile is None:
                    continue
                if not jpeg_bytes:  # empty frame = that person turned their camera off
                    self.video_photo_refs.pop(did, None)
                    tile["img"].config(image="", text="📷 Camera off")
                    continue
                photo = self._jpeg_bytes_to_photo(jpeg_bytes)
                if photo is not None:
                    self.video_photo_refs[did] = photo  # keep alive
                    tile["img"].config(image=photo, text="")
            self._video_grid_refresh_id = self.root.after(1000 // VIDEO_FPS, self._video_grid_tick)
        else:
            self._video_grid_refresh_id = None

    @staticmethod
    def _jpeg_bytes_to_photo(jpeg_bytes):
        if HAVE_PIL:
            try:
                img = Image.open(io.BytesIO(jpeg_bytes))
                return ImageTk.PhotoImage(img)
            except Exception:
                return None
        if HAVE_CV2:
            try:
                arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
                frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if frame is None:
                    return None
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                return ImageTk.PhotoImage(image=Image.fromarray(frame)) if HAVE_PIL else None
            except Exception:
                return None
        return None

    def _join_video_call(self):
        if not HAVE_CV2 or not HAVE_PIL:
            missing = []
            if not HAVE_CV2:
                missing.append("opencv-python")
            if not HAVE_PIL:
                missing.append("pillow")
            messagebox.showinfo("Libraries required",
                                "Video calling needs both of these (camera capture needs OpenCV, "
                                "displaying the video needs Pillow):\n"
                                f"pip install {' '.join(missing)}")
            return
        if self.in_video_call:
            return
        cap = open_camera()
        self.video_capture = cap
        self.camera_on = cap is not None   # no camera -> still join, just without your video
        self.mic_on = HAVE_AUDIO
        self.in_video_call = True
        self.video_send_thread_stop.clear()
        self.join_btn.pack_forget()
        self.leave_btn.pack(side="right")
        self.cam_btn.pack(side="right", padx=4)
        self.mic_btn.pack(side="right")
        if not HAVE_AUDIO:
            self.mic_btn.config(state="disabled")
        self._update_call_buttons()
        if cap is None:
            self.call_status_var.set("You're in the call - no camera found, you can still see and hear everyone.")
            self.log("No working camera found (is it used by another app, or blocked in "
                     "Windows Settings > Privacy > Camera?). Joined without video.")
        else:
            self.call_status_var.set("You're in the call.")
        self._ensure_call_tile(DEVICE_ID, (self.name_var.get().strip() or "You") + " (You)")

        self._start_audio()
        self.video_send_thread = threading.Thread(target=self._video_send_loop, daemon=True)
        self.video_send_thread.start()
        threading.Thread(target=self._audio_send_loop, daemon=True).start()
        self._video_reconnect_id = self.root.after(VIDEO_PEER_RECONNECT_MS, self._reconcile_video_peers_tick)
        if not self._scan_running:  # look for the others right now instead of waiting for the next scan
            threading.Thread(target=self._scan_worker, daemon=True).start()
        self._update_video_room_label()

    def _leave_video_call(self):
        if not self.in_video_call:
            return
        self.in_video_call = False
        self.video_send_thread_stop.set()
        self._stop_audio()
        t = self.video_send_thread
        if t is not None:
            t.join(timeout=1.5)
        self.video_send_thread = None
        if self.video_capture is not None:
            try:
                self.video_capture.release()
            except Exception:
                pass
            self.video_capture = None
        with self.video_peers_lock:
            for sock in self.video_peers_out.values():
                try:
                    sock.close()
                except Exception:
                    pass
            self.video_peers_out.clear()
        with self.video_frames_lock:
            self.video_frames_in.pop(DEVICE_ID, None)
        self._remove_call_tile(DEVICE_ID)
        if self._video_reconnect_id is not None:
            self.root.after_cancel(self._video_reconnect_id)
            self._video_reconnect_id = None
        if self.call_window is not None and self.call_window.winfo_exists():
            self.leave_btn.pack_forget()
            self.cam_btn.pack_forget()
            self.mic_btn.pack_forget()
            self.join_btn.pack(side="right")
            self.call_status_var.set("Left the call.")
        self._update_video_room_label()

    # ---- mic / camera buttons ----

    def _toggle_mic(self):
        self.mic_on = not self.mic_on
        self._update_call_buttons()

    def _toggle_camera(self):
        self.camera_on = not self.camera_on
        self._update_call_buttons()

    def _update_call_buttons(self):
        if self.mic_btn is None or self.cam_btn is None:
            return
        try:
            self.mic_btn.config(text="🎙 Mute" if self.mic_on else "🔇 Unmute")
            self.cam_btn.config(text="📷 Camera off" if self.camera_on else "📷 Camera on")
        except tk.TclError:
            pass

    # ---- call audio ----

    def _start_audio(self):
        if not HAVE_AUDIO:
            self.log("Audio libraries missing - the call will be video only.")
            return
        with self.audio_lock:
            self.audio_bufs.clear()
        while not self.audio_q.empty():
            try:
                self.audio_q.get_nowait()
            except queue.Empty:
                break
        self.mic_level = 0
        self._mic_rate = AUDIO_RATE
        self._out_rate = AUDIO_RATE

        # ---- microphone: try the default device, then every input device,
        # first at 16 kHz and then at the device's own rate (we resample).
        mic_error = None
        candidates = [None]
        try:
            for i, d in enumerate(sd.query_devices()):
                if d["max_input_channels"] > 0:
                    candidates.append(i)
        except Exception as e:
            mic_error = e
        for dev in candidates:
            for rate in (AUDIO_RATE, None):
                try:
                    info = sd.query_devices(kind="input") if dev is None else sd.query_devices(dev)
                    r = rate or int(info["default_samplerate"])
                    self._mic_rate = r
                    stream = sd.InputStream(device=dev, samplerate=r, channels=1, dtype="int16",
                                            blocksize=int(r * 0.04), callback=self._mic_callback)
                    stream.start()
                    self.mic_stream = stream
                    self.log(f"Microphone: {info['name']} @ {r} Hz")
                    break
                except Exception as e:
                    mic_error = e
            if self.mic_stream is not None:
                break
        if self.mic_stream is None:
            self.mic_on = False
            self.log(f"No working microphone: {mic_error}")
            if self.mic_btn is not None:
                self.mic_btn.config(state="disabled")
                self._update_call_buttons()
            messagebox.showwarning(
                "Microphone unavailable",
                "Could not open a microphone, so others will not hear you "
                "(you can still see and hear them).\n\n"
                "Check: Windows Settings > Privacy & security > Microphone > "
                "'Let desktop apps access your microphone' is ON, and that a "
                "microphone is plugged in / set as the default input.\n\n"
                f"Details: {mic_error}")

        # ---- speakers
        for rate in (AUDIO_RATE, None):
            try:
                info = sd.query_devices(kind="output")
                r = rate or int(info["default_samplerate"])
                self._out_rate = r
                stream = sd.OutputStream(samplerate=r, channels=1, dtype="int16",
                                         blocksize=int(r * 0.04), callback=self._audio_out_callback)
                stream.start()
                self.out_stream = stream
                self.log(f"Speakers: {info['name']} @ {r} Hz")
                break
            except Exception as e:
                self.log(f"Could not open speakers at {rate or 'default'} Hz: {e}")

    def _stop_audio(self):
        for name in ("mic_stream", "out_stream"):
            stream = getattr(self, name)
            setattr(self, name, None)
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass

    def _mic_callback(self, indata, frames, time_info, status):
        samples = indata[:, 0]
        self.mic_level = int(np.abs(samples).max()) if len(samples) else 0  # for the level meter
        if not self.mic_on:
            return
        if self._mic_rate != AUDIO_RATE:  # device isn't 16 kHz -> resample down/up to 16 kHz
            x = samples.astype(np.float32)
            n = max(1, int(len(x) * AUDIO_RATE / self._mic_rate))
            samples = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.int16)
        try:
            self.audio_q.put_nowait(samples.tobytes())
        except queue.Full:
            pass

    def _audio_send_loop(self):
        while not self.video_send_thread_stop.is_set():
            try:
                data = self.audio_q.get(timeout=0.5)
            except queue.Empty:
                continue
            self._broadcast_frame(b"A", data)

    def _on_audio_in(self, device_id, pcm):
        # Called from the listener threads: queue the PCM so the output stream can mix it.
        if not self.in_video_call:
            return
        with self.audio_lock:
            buf = self.audio_bufs.setdefault(device_id, bytearray())
            buf.extend(pcm)
            if len(buf) > AUDIO_RATE * 2:  # more than 1 s queued -> drop the oldest, keep ~0.5 s
                del buf[:len(buf) - AUDIO_RATE]

    def _audio_out_callback(self, outdata, frames, time_info, status):
        # samples we need at 16 kHz to fill this output block
        src = frames if self._out_rate == AUDIO_RATE else max(1, int(round(frames * AUDIO_RATE / self._out_rate)))
        need = src * 2
        mix = np.zeros(src, dtype=np.int32)
        with self.audio_lock:
            for buf in self.audio_bufs.values():
                if not buf:
                    continue
                chunk = bytes(buf[:need])
                del buf[:need]
                if len(chunk) < need:
                    chunk += b"\x00" * (need - len(chunk))
                mix += np.frombuffer(chunk, dtype=np.int16)
        if src != frames:  # speakers aren't 16 kHz -> resample to their rate
            mix = np.interp(np.linspace(0, src - 1, frames), np.arange(src), mix.astype(np.float32))
        outdata[:, 0] = np.clip(mix, -32768, 32767).astype(np.int16)

    # ---- sending to peers ----

    def _broadcast_frame(self, kind, body):
        """Send one packet (kind byte + body) to every connected peer."""
        payload = kind + body
        with self.video_peers_lock:
            peers = dict(self.video_peers_out)
        for did, sock in peers.items():
            try:
                with self.video_send_lock:
                    _send_framed(sock, payload)
            except Exception:
                with self.video_peers_lock:
                    self.video_peers_out.pop(did, None)
                try:
                    sock.close()
                except Exception:
                    pass

    def _reconcile_video_peers_tick(self):
        if self.in_video_call:
            threading.Thread(target=self._reconcile_video_peers, daemon=True).start()
            self._video_reconnect_id = self.root.after(VIDEO_PEER_RECONNECT_MS,
                                                        self._reconcile_video_peers_tick)
        else:
            self._video_reconnect_id = None

    def _reconcile_video_peers(self):
        """Open outgoing connections to any in-call peer we're not yet streaming to."""
        wanted = {did: c["ip"] for did, c in self.contacts.items()
                  if c.get("in_video_call") and c.get("reachable") == "yes"}
        with self.video_peers_lock:
            existing = set(self.video_peers_out.keys())
        for did, ip in wanted.items():
            if did in existing:
                continue
            try:
                sock = socket.create_connection((ip, VIDEO_PORT), timeout=5)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                header = json.dumps({"device_id": DEVICE_ID,
                                     "hostname": self.name_var.get().strip() or socket.gethostname()}
                                    ).encode("utf-8")
                _send_framed(sock, header)
                with self.video_peers_lock:
                    self.video_peers_out[did] = sock
            except Exception:
                pass
        # Drop connections to peers no longer in the call.
        with self.video_peers_lock:
            for did in list(self.video_peers_out.keys()):
                if did not in wanted:
                    try:
                        self.video_peers_out[did].close()
                    except Exception:
                        pass
                    del self.video_peers_out[did]

    def _video_send_loop(self):
        # Kick off an immediate peer connect so we don't wait a full
        # VIDEO_PEER_RECONNECT_MS before the first frame can go anywhere.
        self._reconcile_video_peers()
        frame_interval = 1.0 / VIDEO_FPS
        last_keepalive = 0.0
        while not self.video_send_thread_stop.is_set():
            start = time.monotonic()

            # keepalive so quiet peers (camera off + muted) aren't timed out
            if start - last_keepalive > 1.0:
                self._broadcast_frame(b"K", b"")
                last_keepalive = start

            # camera switched ON -> (re)open it
            if self.camera_on and self.video_capture is None:
                cap = open_camera()
                if cap is not None:
                    self.video_capture = cap
                else:
                    self.camera_on = False
                    self.log("Could not open the camera.")
                    self.root.after(0, self._update_call_buttons)

            # camera switched OFF -> release it and tell everyone
            if not self.camera_on:
                if self.video_capture is not None:
                    self.video_capture.release()
                    self.video_capture = None
                with self.video_frames_lock:
                    self.video_frames_in[DEVICE_ID] = b""
                self._broadcast_frame(b"V", b"")
                time.sleep(0.25)
                continue

            ok, frame = self.video_capture.read()
            if not ok:
                time.sleep(frame_interval)
                continue
            h, w = frame.shape[:2]
            scale = VIDEO_MAX_DIM / max(h, w)
            if scale < 1:
                frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, VIDEO_JPEG_QUALITY])
            if not ok:
                continue
            jpeg_bytes = buf.tobytes()
            with self.video_frames_lock:
                self.video_frames_in[DEVICE_ID] = jpeg_bytes  # so our own tile shows our own feed too
            self._broadcast_frame(b"V", jpeg_bytes)

            elapsed = time.monotonic() - start
            time.sleep(max(0.0, frame_interval - elapsed))

    # -- Sending ------------------------------------------------------------

    def send_current(self):
        if self.is_recording:
            return  # finish (stop) the recording first
        if self.pending_voice:
            data, duration = self.pending_voice
            if self._send_media("voice", data, duration=duration):
                self._clear_pending_voice()
            else:
                return  # couldn't send (nobody online) - keep the recording
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
            self._clear_pending_voice()
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
        # Do NOT send yet - keep it until the user presses Send.
        self.pending_voice = (data, duration)
        secs = int(duration)
        self.voice_status_var.set(f"🎙 Voice ready {secs // 60}:{secs % 60:02d} - press Send")
        self.discard_btn.pack(side="left", padx=(0, 4))

    def _clear_pending_voice(self):
        self.pending_voice = None
        self.voice_status_var.set("")
        self.discard_btn.pack_forget()

    def _discard_pending_voice(self):
        self._clear_pending_voice()

    # -- Emoji picker -----------------------------------------------------------

    def toggle_emoji_picker(self):
        if self._emoji_win is not None and self._emoji_win.winfo_exists():
            self._emoji_win.destroy()
            self._emoji_win = None
            return
        win = tk.Toplevel(self.root)
        win.title("Emoji")
        win.resizable(False, False)
        win.transient(self.root)
        self._emoji_win = win

        def on_close():
            self._emoji_win = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", on_close)
        win.bind("<Escape>", lambda e: on_close())
        cols = 8
        for i, emo in enumerate(EMOJIS):
            tk.Button(win, text=emo, font=("Segoe UI Emoji", 14), width=2, relief="flat",
                      cursor="hand2", command=lambda e=emo: self._insert_emoji(e)
                      ).grid(row=i // cols, column=i % cols, padx=1, pady=1)
        win.update_idletasks()
        x = self.emoji_btn.winfo_rootx()
        y = max(self.emoji_btn.winfo_rooty() - win.winfo_reqheight() - 6, 0)
        win.geometry(f"+{x}+{y}")

    def _insert_emoji(self, emo):
        self.msg_entry.insert("insert", emo)
        self.msg_entry.focus_set()

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
                return False
            self._append_chat(GLOBAL_KEY, entry)
            threading.Thread(target=self._broadcast_media_worker,
                             args=(targets, kind, data, filename, duration, anonymous, display_name, msg_id),
                             daemon=True).start()
            return True

        contact = self.contacts.get(key)
        if not contact or contact["reachable"] != "yes":
            messagebox.showerror("Not reachable", "This device is offline right now.")
            return False
        try:
            if kind == "image":
                send_image(contact["ip"], data, filename or "image.jpg", anonymous=anonymous,
                           display_name=display_name, scope="private", msg_id=msg_id)
            else:
                send_voice(contact["ip"], data, duration or 0, anonymous=anonymous,
                           display_name=display_name, scope="private", msg_id=msg_id)
            self._append_chat(key, entry)
            return True
        except Exception as e:
            messagebox.showerror("Send failed", str(e))
            return False

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
                                  "unread": False, "last_seen": time.time(), "in_video_call": False}
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
        if self.in_video_call:
            self._leave_video_call()
        if self._video_grid_refresh_id is not None:
            self.root.after_cancel(self._video_grid_refresh_id)
            self._video_grid_refresh_id = None
        for timer_id in (self._contact_expiry_id, self._fast_scan_id):
            if timer_id is not None:
                self.root.after_cancel(timer_id)
        self.listener.stop()
        self.listener.join(timeout=1.5)
        self.discovery_listener.stop()
        self.discovery_listener.join(timeout=1.5)
        self.video_listener.stop()
        self.video_listener.join(timeout=1.5)
        self.root.destroy()


def _selftest():
    """`LAN_Chat.exe --selftest` writes selftest.txt next to the exe, then exits.
    build_exe.bat uses it to check that OpenCV really made it into the exe."""
    lines = [
        f"python={sys.version.split()[0]} frozen={getattr(sys, 'frozen', False)}",
        "cv2=" + (f"OK {cv2.__version__}" if HAVE_CV2 else f"FAIL {CV2_ERROR}"),
        "pillow=" + ("OK" if HAVE_PIL else "FAIL"),
        "audio=" + ("OK" if HAVE_AUDIO else "FAIL"),
    ]
    if CV2_TB:
        lines.append(CV2_TB)
    base = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    with open(os.path.join(base, "selftest.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    if "--selftest" in sys.argv:
        _selftest()
        return
    root = tk.Tk()
    ChatApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()