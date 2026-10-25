"""Fixed-session Tailmsg delivery through the installed official Codex CLI.

CodexAppClient(config).call_tool('send_message_to_thread', arguments) uses the
official `codex queue` command. The Codex desktop app (macOS) or an existing
local app-server reached through `--remote unix:///absolute/socket` (Linux) owns
and executes the real session; this queue client never creates/resumes a model
session, never selects `--last`, and never changes permissions. The Linux daemon
separately keeps the fixed thread loaded through codex_resident.py.
`read_thread` uses a short-lived official app-server for a read-only thread/read
request, returning only Tailmsg markers from user messages for reconciliation.
`read_thread_name()` is a separate metadata-only thread/read (includeTurns false)
that returns the fixed session's optional native name (Thread.name), never its
preview, turns or status.

Configuration: dispatcher_thread_id (or caller_thread_id), codex_cli_path
(optional on macOS), codex_remote (optional absolute unix:/// socket URL owned
by this user; requires a UUID thread and codex_path), codex_home (optional
CODEX_HOME for both queue and reconciliation, so reads use the same account and
session store as the target runtime), timeout (seconds, default 30). Only the
fixed local dispatcher can be addressed. No send is retried here.
CodexAppError.delivery_unknown is False when the runtime was unavailable before
any attempt and True after a queue subprocess times out, fails or returns no
valid receipt; reconcile before any retry.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import selectors
import socket
import stat
import subprocess
import time
from typing import Any, Mapping, Optional, Tuple
import uuid


DEFAULT_CLI_PATH = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
APP_EXECUTABLE = "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
NAME_READ_TIMEOUT = 10.0     # seconds for the metadata-only name read
MAX_NAME_CHARS = 200
MARKER_RE = re.compile(r"\[TAILMSG:[A-Za-z0-9_-]{8,64}\]")
QUEUE_RECEIPT_RE = re.compile(r"^Queued message ([A-Za-z0-9_-]{8,128}) for thread ([A-Za-z0-9_-]{8,128})\.$", re.MULTILINE)


UNIX_REMOTE_PREFIX = "unix://"


class CodexAppError(RuntimeError):
    def __init__(self, message: str, delivery_unknown: bool = True, code: str = "queue_error"):
        super().__init__(message)
        self.delivery_unknown = bool(delivery_unknown)
        self.code = code


def _user_markers(thread: Mapping[str, Any], turn_limit: int) -> list[str]:
    """Agent output and tool output cannot be evidence of user-message delivery."""
    found: set[str] = set()
    for turn in thread.get("turns", [])[-turn_limit:]:
        for item in turn.get("items", []):
            if item.get("type") != "userMessage":
                continue
            for content in item.get("content", []):
                if content.get("type") in ("text", "inputText") and isinstance(content.get("text"), str):
                    found.update(MARKER_RE.findall(content["text"]))
    return sorted(found)


def unix_socket_path(remote: Any) -> str:
    """Return the socket path of an explicit `unix:///absolute/path` URL or raise invalid_config.

    Network endpoints (ws://, wss://), the CLI's implicit default (`unix://`), relative or
    non-normalized paths, queries, fragments and control characters are all rejected."""
    if not isinstance(remote, str) or not remote.startswith(UNIX_REMOTE_PREFIX + "/"):
        raise CodexAppError("codex_remote must be an explicit unix:///absolute/socket URL.", False, "invalid_config")
    path = remote[len(UNIX_REMOTE_PREFIX):]
    if any(ord(char) < 32 or char in "\x7f?#" for char in path) or path.startswith("//") or \
            path == "/" or path.endswith("/") or os.path.normpath(path) != path:
        raise CodexAppError("codex_remote must be an explicit unix:///absolute/socket URL.", False, "invalid_config")
    return path


def _private_directory(path: str) -> bool:
    """Only this user (or root, or a sticky shared directory) may replace entries in it."""
    info = os.stat(path)
    if info.st_uid not in (os.getuid(), 0):
        return False
    return not (info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)) or bool(info.st_mode & stat.S_ISVTX)


def unix_socket_problem(path: str, timeout: float = 2.0) -> Optional[Tuple[str, str]]:
    """None when a same-user app-server is listening on `path`, else (code, explanation).

    The endpoint may be a symlink into a private runtime directory, so the endpoint's type and
    owner are checked. A connect probe sends no bytes; refusal means a stale socket file, i.e.
    nothing could have been delivered yet."""
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return "socket_missing", "The Codex app-server socket does not exist; start the app-server."
    except OSError:
        return "socket_unavailable", "The Codex app-server socket cannot be inspected."
    if not stat.S_ISSOCK(info.st_mode):
        return "socket_invalid", "The configured Codex remote is not a Unix socket."
    if info.st_uid != os.getuid():
        return "socket_foreign", "The Codex app-server socket belongs to another user; only a same-user socket is used."
    try:
        directories = {os.path.dirname(path), os.path.dirname(os.path.realpath(path))}
        if not all(_private_directory(directory) for directory in directories):
            return "socket_directory_unsafe", "Other users can replace the Codex socket; use a private directory."
    except OSError:
        return "socket_unavailable", "The Codex app-server socket directory cannot be inspected."
    try:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError:
        # For example EMFILE: no probe was possible, so nothing was sent either.
        return "socket_probe_failed", "A connection probe could not be created (local resource limit?)."
    try:
        probe.settimeout(timeout)
        probe.connect(path)
    except ConnectionRefusedError:
        return "socket_stale", "Nothing is listening on the Codex app-server socket (stale socket); start or restart the app-server."
    except OSError:
        return "socket_unreachable", "The Codex app-server socket did not accept a connection."
    finally:
        probe.close()
    return None


class CodexAppClient:
    def __init__(self, config: Mapping[str, Any] | None = None, **kwargs: Any):
        settings = dict(config or {})
        settings.update(kwargs)
        self.thread_id = settings.get("dispatcher_thread_id") or settings.get("caller_thread_id")
        configured_cli = settings.get("codex_cli_path") or settings.get("codex_path")
        self.cli_path = str(configured_cli or DEFAULT_CLI_PATH)
        if not isinstance(self.thread_id, str) or not self.thread_id.strip():
            raise CodexAppError("Configure the fixed dispatcher thread ID.", False, "invalid_config")
        self.remote = settings.get("codex_remote")
        self.socket_path = None
        if self.remote is not None:
            self.socket_path = unix_socket_path(self.remote)
            # A session name could resolve to another session; only an exact existing UUID is addressed.
            try:
                exact = str(uuid.UUID(self.thread_id)) == self.thread_id
            except ValueError:
                exact = False
            if not exact:
                raise CodexAppError("The Unix socket transport needs the dispatcher's lowercase session UUID.", False, "invalid_config")
            if not configured_cli or not os.path.isabs(self.cli_path):
                raise CodexAppError("The Unix socket transport needs an absolute codex_path.", False, "invalid_config")
        self.codex_home = settings.get("codex_home")
        if self.codex_home is not None and (not isinstance(self.codex_home, str) or not os.path.isabs(self.codex_home)
                                            or any(ord(char) < 32 for char in self.codex_home)):
            raise CodexAppError("codex_home must be an absolute directory path.", False, "invalid_config")
        try:
            self.timeout = float(settings.get("timeout", settings.get("timeout_seconds", 30)))
        except (TypeError, ValueError) as error:
            raise CodexAppError("Invalid Codex queue timeout.", False, "invalid_config") from error
        if not math.isfinite(self.timeout) or not 0 < self.timeout <= 300:
            raise CodexAppError("Codex queue timeout must be between 0 and 300 seconds.", False, "invalid_config")

    def _validate(self, arguments: Mapping[str, Any]) -> None:
        if arguments.get("threadId") != self.thread_id:
            raise CodexAppError("Only the configured dispatcher thread may be addressed.", False, "thread_mismatch")
        if arguments.get("hostId", "local") != "local":
            raise CodexAppError("The dispatcher must be on the local host.", False, "host_mismatch")
        if not Path(self.cli_path).is_file() or not os.access(self.cli_path, os.X_OK):
            raise CodexAppError("The installed official Codex CLI is unavailable.", False, "runtime_unavailable")

    def _app_running(self) -> bool:
        try:
            result = subprocess.run(
                ["/bin/ps", "-ww", "-U", str(os.getuid()), "-o", "pid=,comm="],
                capture_output=True, text=True,
                timeout=min(2, self.timeout), check=False,
            )
            if result.returncode != 0:
                return False
            for line in result.stdout.splitlines():
                fields = line.strip().split(None, 1)
                if len(fields) == 2 and fields[1] == APP_EXECUTABLE:
                    return True
            return False
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _env(self) -> dict[str, str] | None:
        """Pin CODEX_HOME when configured so queue and reconciliation use the runtime's account."""
        if self.codex_home is None:
            return None
        return dict(os.environ, CODEX_HOME=self.codex_home)

    def _send(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        prompt = arguments.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise CodexAppError("A nonempty queue notification is required.", False, "invalid_request")
        command = [self.cli_path, "queue", "--thread", self.thread_id, "--message", prompt]
        if self.socket_path is not None:
            try:
                problem = unix_socket_problem(self.socket_path, min(2.0, self.timeout))
            except Exception:
                # Still before the queue command: nothing can have been delivered.
                problem = ("socket_probe_failed", "The Codex app-server socket could not be checked.")
            if problem is not None:
                raise CodexAppError(problem[1] + " Delivery was not attempted.", False, problem[0])
            command += ["--remote", self.remote]
        elif not self._app_running():
            raise CodexAppError("Codex desktop is not available; delivery was not attempted.", False, "app_unavailable")
        try:
            result = subprocess.run(
                command, stdin=subprocess.DEVNULL, env=self._env(),
                capture_output=True, text=True, timeout=self.timeout, check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise CodexAppError("Official Codex queue timed out; delivery is unknown.", True, "queue_timeout") from error
        except OSError as error:
            raise CodexAppError("Official Codex queue could not start.", False, "runtime_unavailable") from error
        if result.returncode != 0:
            # The CLI can lose its response after the desktop accepted a queue
            # entry. A nonzero exit is therefore never proof of non-delivery.
            raise CodexAppError("Official Codex queue failed; delivery is unknown.", True, "queue_failed")
        receipts = QUEUE_RECEIPT_RE.findall(result.stdout)
        if len(receipts) != 1 or receipts[0][1] != self.thread_id:
            raise CodexAppError("Official Codex queue returned no valid acceptance receipt; delivery is unknown.", True, "queue_receipt_unknown")
        return {"ok": True, "threadId": self.thread_id, "hostId": "local", "status": "enqueued",
                "transport": "codex_queue_unix" if self.socket_path is not None else "codex_queue", "queueId": receipts[0][0]}

    def _read(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        try:
            turn_limit = int(arguments.get("turnLimit", 100))
        except (TypeError, ValueError) as error:
            raise CodexAppError("Invalid thread read limit.", False, "invalid_request") from error
        if not 1 <= turn_limit <= 1000:
            raise CodexAppError("Thread read limit must be between 1 and 1000.", False, "invalid_request")
        thread = self._thread_read(include_turns=True, timeout=self.timeout)
        return {"ok": True, "threadId": self.thread_id, "hostId": "local", "tailmsgMarkers": _user_markers(thread, turn_limit)}

    def read_thread_name(self) -> dict[str, Any]:
        """Metadata-only read of the fixed session: its optional native name (Thread.name) and nothing else.

        Uses thread/read with includeTurns false, separate from marker reconciliation. Thread.preview
        (the first user message), turns and status are never returned: a short-lived reader's status
        such as notLoaded says nothing about the runtime that owns the session."""
        if not Path(self.cli_path).is_file() or not os.access(self.cli_path, os.X_OK):
            raise CodexAppError("The installed official Codex CLI is unavailable.", False, "runtime_unavailable")
        thread = self._thread_read(include_turns=False, timeout=min(self.timeout, NAME_READ_TIMEOUT))
        name = thread.get("name")
        if not isinstance(name, str) or not name.strip():
            name = None
        elif len(name) > MAX_NAME_CHARS:
            name = name[:MAX_NAME_CHARS]
        return {"thread_id": self.thread_id, "name": name}

    def _thread_read(self, include_turns: bool, timeout: float) -> dict[str, Any]:
        try:
            process = subprocess.Popen(
                [self.cli_path, "app-server", "--listen", "stdio://"], env=self._env(),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            raise CodexAppError("Official Codex read-only app-server could not start.", False, "runtime_unavailable") from error
        selector = None
        buffer = bytearray()
        pending: list[dict[str, Any]] = []
        deadline = time.monotonic() + timeout

        def send(value: dict[str, Any]) -> None:
            process.stdin.write((json.dumps(value) + "\n").encode("utf-8"))
            process.stdin.flush()

        def response(request_id: int) -> dict[str, Any]:
            while True:
                for index, value in enumerate(pending):
                    if value.get("id") == request_id:
                        return pending.pop(index)
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise CodexAppError("Official Codex thread read timed out.", False, "read_timeout")
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise CodexAppError("Official Codex read-only app-server closed.", False, "read_failed")
                buffer.extend(chunk)
                if len(buffer) > MAX_RESPONSE_BYTES:
                    raise CodexAppError("Official Codex thread read exceeds 8 MiB.", False, "response_too_large")
                while b"\n" in buffer:
                    line, _, rest = buffer.partition(b"\n")
                    buffer[:] = rest
                    try:
                        value = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError) as error:
                        raise CodexAppError("Official Codex app-server returned invalid JSON.", False, "read_failed") from error
                    if isinstance(value, dict) and "id" in value:
                        pending.append(value)

        try:
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            send({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "tailmsg_local_reader", "title": "Tailmsg local reader", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            }})
            initialized = response(1)
            if "error" in initialized:
                raise CodexAppError("Official Codex app-server initialization failed.", False, "read_failed")
            send({"method": "initialized"})
            send({"id": 2, "method": "thread/read", "params": {"threadId": self.thread_id, "includeTurns": include_turns}})
            read_result = response(2)
            if "error" in read_result:
                raise CodexAppError("Official Codex thread read was rejected.", False, "read_failed")
            result = read_result.get("result")
            thread = result.get("thread") if isinstance(result, dict) else None
            if not isinstance(thread, dict) or thread.get("id") != self.thread_id:
                raise CodexAppError("Official Codex returned an unexpected thread.", False, "read_failed")
            return thread
        except (OSError, ValueError, BrokenPipeError) as error:
            raise CodexAppError("Official Codex read-only app-server failed.", False, "read_failed") from error
        finally:
            try:
                if selector is not None:
                    selector.close()
            finally:
                try:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                finally:
                    for stream in (process.stdin, process.stdout):
                        if stream is not None:
                            stream.close()

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(arguments, Mapping):
            raise CodexAppError("Tool arguments must be an object.", False, "invalid_request")
        self._validate(arguments)
        if name == "send_message_to_thread":
            return self._send(arguments)
        if name == "read_thread":
            return self._read(arguments)
        raise CodexAppError("This client only queues notifications and reads dispatcher markers.", False, "unsupported_tool")
