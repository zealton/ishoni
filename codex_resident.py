"""Keep one existing dispatcher loaded on a same-user Codex Unix app-server.

Only the daemon uses this connection. It resumes the configured UUID without
permission/model/workspace overrides, never starts turns or answers approvals,
and discards notifications instead of persisting conversation content. The
official queue client still owns delivery receipts and reconciliation.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import select
import socket
import struct
import tempfile
import threading
import time

MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class ResidentError(RuntimeError):
    pass


class UnixWebSocket:
    """Bounded RFC 6455 text transport; no third-party dependency or network endpoint."""
    def __init__(self, path, stop, timeout):
        self.stop = stop
        self.timeout = timeout
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.buffer = bytearray()
        self.fragments = bytearray()
        self.fragmented = False
        try:
            self.socket.settimeout(min(timeout, 0.5))
            self.socket.connect(path)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            request = ("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                       "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                       "Sec-WebSocket-Key: " + key + "\r\n\r\n")
            self.socket.sendall(request.encode("ascii"))
            deadline = time.monotonic() + timeout
            while b"\r\n\r\n" not in self.buffer:
                self._more(deadline)
                if len(self.buffer) > 16384:
                    raise ResidentError("handshake_invalid")
            header, _, rest = self.buffer.partition(b"\r\n\r\n")
            self.buffer[:] = rest
            lines = header.decode("ascii").split("\r\n")
            headers = {}
            for line in lines[1:]:
                name, value = line.split(":", 1)
                name = name.lower().strip()
                if name in headers:
                    raise ResidentError("handshake_invalid")
                headers[name] = value.strip()
            expected = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
            if lines[0].split()[:2] != ["HTTP/1.1", "101"] or \
                    headers.get("upgrade", "").lower() != "websocket" or \
                    "upgrade" not in headers.get("connection", "").lower().replace(" ", "").split(",") or \
                    headers.get("sec-websocket-accept") != expected or \
                    "sec-websocket-extensions" in headers or "sec-websocket-protocol" in headers:
                raise ResidentError("handshake_invalid")
        except BaseException:
            self.close()
            raise

    def close(self):
        self.socket.close()

    def _more(self, deadline):
        while not self.stop.is_set():
            if time.monotonic() >= deadline:
                raise ResidentError("resident_timeout")
            try:
                chunk = self.socket.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                raise ResidentError("resident_disconnected")
            self.buffer.extend(chunk)
            return
        raise ResidentError("resident_stopped")

    def _take(self, count, deadline):
        while len(self.buffer) < count:
            self._more(deadline)
        result = bytes(self.buffer[:count])
        del self.buffer[:count]
        return result

    def send(self, value, opcode=1):
        payload = json.dumps(value, separators=(",", ":")).encode("utf-8") if opcode == 1 else value
        mask = os.urandom(4)
        length = len(payload)
        if length > MAX_RESPONSE_BYTES:
            raise ResidentError("response_too_large")
        size = bytes([0x80 | length]) if length < 126 else \
            (b"\xfe" + struct.pack("!H", length) if length < 65536 else b"\xff" + struct.pack("!Q", length))
        self.socket.sendall(bytes([0x80 | opcode]) + size + mask +
                            bytes(char ^ mask[index % 4] for index, char in enumerate(payload)))

    def receive(self, deadline):
        while True:
            first, second = self._take(2, deadline)
            final, opcode = bool(first & 0x80), first & 15
            if first & 0x70 or second & 0x80 or opcode not in (0, 1, 8, 9, 10):
                raise ResidentError("protocol_error")
            length = second & 127
            if opcode >= 8 and (not final or length > 125):
                raise ResidentError("protocol_error")
            if length == 126:
                length = struct.unpack("!H", self._take(2, deadline))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._take(8, deadline))[0]
            if length > MAX_RESPONSE_BYTES or len(self.fragments) + length > MAX_RESPONSE_BYTES:
                raise ResidentError("response_too_large")
            payload = self._take(length, deadline)
            if opcode == 8:
                raise ResidentError("resident_disconnected")
            if opcode == 9:
                self.send(payload, opcode=10)
                return None
            if opcode == 10:
                return None
            if (opcode == 0) != self.fragmented:
                raise ResidentError("protocol_error")
            self.fragments.extend(payload)
            self.fragmented = not final
            if not final:
                continue
            raw = bytes(self.fragments)
            self.fragments.clear()
            try:
                value = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeError):
                raise ResidentError("protocol_error")
            if not isinstance(value, dict):
                raise ResidentError("protocol_error")
            return value


class ResidentDispatcher:
    """A single daemon-owned connection, with bounded reconnect and metadata heartbeat."""
    def __init__(self, config):
        from codex_queue_client import CodexAppClient
        self.client = CodexAppClient(config)  # validates exact UUID, explicit socket, fixed account
        if self.client.socket_path is None or config.get("dispatcher_host_id", "local") != "local":
            raise ValueError("resident dispatcher requires a local Unix socket")
        self.home = Path(config["bridge_home"]).expanduser().resolve()
        self.stop = threading.Event()
        self.thread = None

    def _status(self, state, reason=None):
        value = {"state": state, "thread_id": self.client.thread_id, "updated_at": time.time(), "reason": reason}
        fd, name = tempfile.mkstemp(prefix=".resident-", dir=str(self.home))
        try:
            with os.fdopen(fd, "w") as out:
                json.dump(value, out)
            os.replace(name, str(self.home / "resident.json"))
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def start(self):
        self.thread = threading.Thread(target=self._run, name="codex-resident", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=self.client.timeout + 1)

    def _response(self, ws, request_id):
        deadline = time.monotonic() + self.client.timeout
        while True:
            value = ws.receive(deadline)
            if value is not None and "method" not in value and value.get("id") == request_id:
                if "error" in value or not isinstance(value.get("result"), dict):
                    raise ResidentError("resume_rejected" if request_id == 2 else "initialize_rejected")
                return value["result"]
            # Server requests (including approvals) are never answered by this transport.

    def _connected(self, ws):
        ws.send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "ishoni_resident", "version": "1"},
            "capabilities": {"experimentalApi": True}}})
        self._response(ws, 1)
        ws.send({"method": "initialized"})
        ws.send({"id": 2, "method": "thread/resume", "params": {
            "threadId": self.client.thread_id, "excludeTurns": True}})
        resumed = self._response(ws, 2)
        if not isinstance(resumed.get("thread"), dict) or resumed["thread"].get("id") != self.client.thread_id:
            raise ResidentError("thread_mismatch")
        self._status("connected")
        heartbeat = time.monotonic()
        ping_at = heartbeat
        received_at = heartbeat
        while not self.stop.is_set():
            now = time.monotonic()
            if now - heartbeat >= 5:
                self._status("connected")
                heartbeat = now
            if now - ping_at >= 10:
                ws.send(b"ishoni", opcode=9)
                ping_at = now
            if now - received_at >= 30:
                raise ResidentError("resident_timeout")
            if not ws.buffer and not select.select([ws.socket], [], [], 0.5)[0]:
                continue
            # A pong or any other frame proves liveness. Idle/unresponsive peers reconnect.
            value = ws.receive(time.monotonic() + 30)
            received_at = time.monotonic()
            if value is not None and value.get("method") in ("thread/closed", "thread/archived") and \
                    value.get("params", {}).get("threadId") == self.client.thread_id:
                raise ResidentError("thread_unloaded")

    def _run(self):
        from codex_queue_client import CodexAppError, unix_socket_problem
        delay = 1
        try:
            while not self.stop.is_set():
                ws = None
                connected_at = time.monotonic()
                try:
                    self._status("connecting")
                    problem = unix_socket_problem(self.client.socket_path, min(0.5, self.client.timeout))
                    if problem is not None:
                        raise CodexAppError(problem[1], False, problem[0])
                    ws = UnixWebSocket(self.client.socket_path, self.stop, self.client.timeout)
                    self._connected(ws)
                except Exception as exc:
                    reason = exc.code if isinstance(exc, CodexAppError) else \
                        (str(exc) if isinstance(exc, ResidentError) else "resident_io_error")
                    self._status("reconnecting", reason)
                finally:
                    if ws is not None:
                        ws.close()
                if time.monotonic() - connected_at >= 60:
                    delay = 1
                if self.stop.wait(delay):
                    break
                delay = min(delay * 2, 60)
        finally:
            self._status("stopped")


def resident_status(home, thread_id):
    """Metadata only. A stale receipt never means the dispatcher is still loaded."""
    try:
        path = Path(home) / "resident.json"
        if path.stat().st_size > 4096:
            raise ValueError("oversized receipt")
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("thread_id") != thread_id or value.get("state") not in (
                "connecting", "connected", "reconnecting", "stopped"):
            raise ValueError("invalid receipt")
        age = time.time() - float(value["updated_at"])
        if age < 0 or age > (15 if value["state"] == "connected" else 75):
            return {"state": "stale"}
        return {"state": value["state"], "reason": value.get("reason")}
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return {"state": "unknown"}
