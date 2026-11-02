"""
lan_core.py — shared networking code for the LAN messenger tools.

Used by both lan_messenger.py (full scanner + sender) and
lan_listener.py (lightweight receive-only companion app), so the two
always speak the exact same wire protocol and stay compatible.

Protocol: one JSON object per TCP connection to APP_PORT, then the
connection closes.
  Reachability check:  {"type": "ping"}
  Chat message:        {"type": "msg", "anonymous": true/false,
                         "name": "Anonymous" or hostname, "text": "..."}
"""

import socket
import threading
import json

APP_PORT = 50505  # TCP port both apps listen on


def send_message(ip, text, anonymous=True, port=APP_PORT, timeout=3):
    """Send a chat message to a device running either app's listener."""
    payload = {
        "type": "msg",
        "anonymous": anonymous,
        "name": "Anonymous" if anonymous else socket.gethostname(),
        "text": text,
    }
    with socket.create_connection((ip, port), timeout=timeout) as s:
        s.sendall(json.dumps(payload).encode("utf-8"))


def check_listener_running(ip, port=APP_PORT, timeout=0.4):
    """Reachability probe: True if something at ip:port speaks this protocol."""
    try:
        with socket.create_connection((ip, port), timeout=timeout) as s:
            s.sendall(json.dumps({"type": "ping"}).encode("utf-8"))
            return True
    except Exception:
        return False


class MessageListener(threading.Thread):
    """
    Background TCP server. Calls on_message(sender_ip, display_name, text)
    for every incoming chat message; silently ignores pings (those exist
    only so senders can test reachability before messaging).
    """

    def __init__(self, on_message, port=APP_PORT):
        super().__init__(daemon=True)
        self.on_message = on_message
        self.port = port
        self._stop = False
        self._srv = None

    def run(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.port))
        srv.listen(5)
        srv.settimeout(1.0)
        self._srv = srv
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
                    sender_ip = addr[0]
                    name = data.get("name", "Anonymous")
                    text = data.get("text", "")
                    self.on_message(sender_ip, name, text)
            finally:
                conn.close()
        srv.close()

    def stop(self):
        self._stop = True