#!/usr/bin/env python3
"""
LAN Receiver — single-file, receive-and-reply-only companion app.

Run this if someone wants to send you anonymous LAN messages via the
LAN Device Scanner + Messenger tool. This app does NOT scan the network
and does NOT send unsolicited messages on its own - it only listens for
incoming messages and lets you reply to whoever sent one. Closing this
window stops you from being reachable.

Everything needed is in this one file - no other .py files required,
and nothing beyond the Python standard library.
"""

import socket
import threading
import json
import tkinter as tk
from tkinter import ttk, messagebox

APP_PORT = 50505  # must match the port the sender app uses


def send_message(ip, text, anonymous=True, port=APP_PORT, timeout=3):
    """Send a chat message to a device running the matching listener."""
    payload = {
        "type": "msg",
        "anonymous": anonymous,
        "name": "Anonymous" if anonymous else socket.gethostname(),
        "text": text,
    }
    with socket.create_connection((ip, port), timeout=timeout) as s:
        s.sendall(json.dumps(payload).encode("utf-8"))


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
                    continue  # reachability check only, nothing to show
                if data.get("type") == "msg":
                    self.on_message(addr[0], data.get("name", "Anonymous"), data.get("text", ""))
            finally:
                conn.close()
        srv.close()

    def stop(self):
        self._stop = True


class ReceiverApp:
    def __init__(self, root):
        self.root = root
        root.title("LAN Receiver")
        root.geometry("460x420")
        root.minsize(400, 320)

        top = ttk.Frame(root, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="LAN Receiver", font=("Segoe UI", 13, "bold")).pack(anchor="w")
        try:
            local_ip = socket.gethostbyname(socket.gethostname())
        except Exception:
            local_ip = "unknown"
        ttk.Label(
            top,
            text=f"Listening on port {APP_PORT} at {local_ip}. Give this IP to whoever "
                 "wants to message you, or they can find it by scanning the network.",
            foreground="#666", wraplength=430, justify="left"
        ).pack(anchor="w", pady=(2, 0))

        hist_frame = ttk.Frame(root)
        hist_frame.pack(fill="both", expand=True, padx=10, pady=(5, 5))
        self.history = tk.Text(hist_frame, wrap="word", state="disabled", font=("Segoe UI", 10))
        scrollbar = ttk.Scrollbar(hist_frame, command=self.history.yview)
        self.history.configure(yscrollcommand=scrollbar.set)
        self.history.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        reply_frame = ttk.Frame(root, padding=10)
        reply_frame.pack(fill="x")
        self.target_var = tk.StringVar(value="No messages yet - nothing to reply to.")
        ttk.Label(reply_frame, textvariable=self.target_var, foreground="#666").pack(anchor="w")

        entry_row = ttk.Frame(reply_frame)
        entry_row.pack(fill="x", pady=(5, 0))
        self.reply_entry = ttk.Entry(entry_row)
        self.reply_entry.pack(side="left", fill="x", expand=True)
        self.anon_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(entry_row, text="Anonymously", variable=self.anon_var).pack(side="left", padx=5)
        ttk.Button(entry_row, text="Reply", command=self.send_reply).pack(side="left")
        self.reply_entry.bind("<Return>", lambda e: self.send_reply())

        self.last_sender_ip = None
        self._log("Waiting for messages ...")

        self.listener = MessageListener(self.on_message)
        self.listener.start()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _log(self, line):
        self.history.config(state="normal")
        self.history.insert("end", line + "\n")
        self.history.see("end")
        self.history.config(state="disabled")

    def on_message(self, sender_ip, display_name, text):
        self.root.after(0, lambda: self._handle_message(sender_ip, display_name, text))

    def _handle_message(self, sender_ip, display_name, text):
        self.last_sender_ip = sender_ip
        self.target_var.set(f"Replying to: {display_name} ({sender_ip})")
        self._log(f"{display_name}: {text}")
        self.root.bell()
        self.root.deiconify()
        self.root.lift()

    def send_reply(self):
        text = self.reply_entry.get().strip()
        if not text:
            return
        if not self.last_sender_ip:
            messagebox.showwarning("Nothing to reply to", "No message has arrived yet.")
            return
        try:
            send_message(self.last_sender_ip, text, anonymous=self.anon_var.get())
            self._log(f"You: {text}")
            self.reply_entry.delete(0, "end")
        except Exception as e:
            messagebox.showerror("Send failed", str(e))

    def _on_close(self):
        self.listener.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    ReceiverApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()