"""Offline checks for official-CLI delivery and conservative reconciliation.

The Unix socket tests bind real AF_UNIX sockets in temporary directories and run a fake `codex`
executable as a real subprocess; no Codex runtime, production socket or model turn is involved."""

import datetime
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import uuid

from codex_bridge import Bridge
from codex_queue_client import APP_EXECUTABLE, CodexAppClient, CodexAppError, _user_markers, unix_socket_problem


class CodexQueueClientTests(unittest.TestCase):
    def setUp(self):
        self.client = CodexAppClient({"dispatcher_thread_id": "dispatcher-fixed"})
        self.args = {"threadId": "dispatcher-fixed", "hostId": "local", "prompt": "[TAILMSG:message_123]"}
        self.runtime = patch("codex_queue_client.Path.is_file", return_value=True)
        self.access = patch("codex_queue_client.os.access", return_value=True)
        self.runtime.start()
        self.access.start()
        self.addCleanup(self.runtime.stop)
        self.addCleanup(self.access.stop)

    def test_fixed_local_thread_only(self):
        for key, value in (("threadId", "another-thread"), ("hostId", "remote")):
            with self.subTest(key=key), patch("codex_queue_client.subprocess.run") as run:
                with self.assertRaises(CodexAppError) as caught:
                    self.client.call_tool("send_message_to_thread", dict(self.args, **{key: value}))
                self.assertFalse(caught.exception.delivery_unknown)
                run.assert_not_called()

    def test_app_closed_does_not_attempt_queue(self):
        with patch("codex_queue_client.subprocess.run", return_value=SimpleNamespace(returncode=1)) as run:
            with self.assertRaises(CodexAppError) as caught:
                self.client.call_tool("send_message_to_thread", self.args)
            self.assertFalse(caught.exception.delivery_unknown)
            self.assertEqual("app_unavailable", caught.exception.code)
            self.assertEqual(1, run.call_count)

    def test_app_process_matching_uses_exact_executable(self):
        for executable, expected in ((APP_EXECUTABLE, True), (APP_EXECUTABLE + " Helper", False), (APP_EXECUTABLE + "/other", False)):
            with self.subTest(executable=executable), patch("codex_queue_client.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=" 54388 " + executable + "\n")):
                self.assertEqual(expected, self.client._app_running())

    def test_codex_path_config_alias(self):
        self.assertEqual("/custom/codex", CodexAppClient({"dispatcher_thread_id": "dispatcher-fixed", "codex_path": "/custom/codex"}).cli_path)

    def test_success_uses_official_queue_without_permission_overrides(self):
        with patch.object(self.client, "_app_running", return_value=True), patch(
            "codex_queue_client.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="Queued message 33333333-3333-4333-8333-333333333333 for thread dispatcher-fixed.\n")
        ) as run:
            result = self.client.call_tool("send_message_to_thread", self.args)
            self.assertEqual("enqueued", result["status"])
            self.assertEqual([self.client.cli_path, "queue", "--thread", "dispatcher-fixed", "--message", self.args["prompt"]], run.call_args.args[0])
            self.assertFalse(run.call_args.kwargs["check"])

    def test_zero_exit_without_matching_receipt_remains_unknown(self):
        for output in ("", "Queued message queue-id-123 for thread another-thread.\n"):
            with self.subTest(output=output), patch.object(self.client, "_app_running", return_value=True), patch("codex_queue_client.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=output)):
                with self.assertRaises(CodexAppError) as caught:
                    self.client.call_tool("send_message_to_thread", self.args)
                self.assertTrue(caught.exception.delivery_unknown)

    def test_nonzero_exit_remains_unknown(self):
        with patch.object(self.client, "_app_running", return_value=True), patch(
            "codex_queue_client.subprocess.run", return_value=SimpleNamespace(returncode=1)
        ):
            with self.assertRaises(CodexAppError) as caught:
                self.client.call_tool("send_message_to_thread", self.args)
            self.assertTrue(caught.exception.delivery_unknown)

    def test_timeout_remains_unknown(self):
        with patch.object(self.client, "_app_running", return_value=True), patch(
            "codex_queue_client.subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 30)
        ):
            with self.assertRaises(CodexAppError) as caught:
                self.client.call_tool("send_message_to_thread", self.args)
            self.assertTrue(caught.exception.delivery_unknown)

    def test_spawn_failure_proves_no_delivery(self):
        with patch.object(self.client, "_app_running", return_value=True), patch(
            "codex_queue_client.subprocess.run", side_effect=FileNotFoundError()
        ):
            with self.assertRaises(CodexAppError) as caught:
                self.client.call_tool("send_message_to_thread", self.args)
            self.assertFalse(caught.exception.delivery_unknown)

    def test_only_user_markers_are_returned(self):
        thread = {"turns": [{"items": [
            {"type": "userMessage", "content": [{"type": "text", "text": "private body [TAILMSG:message_123]"}]},
            {"type": "agentMessage", "text": "[TAILMSG:agent_fake]"},
            {"type": "commandExecution", "aggregatedOutput": "[TAILMSG:tool_fake]"},
        ]}]}
        markers = _user_markers(thread, 100)
        self.assertEqual(["[TAILMSG:message_123]"], markers)
        self.assertNotIn("private", str(markers))

    def test_recent_window_does_not_claim_complete_history(self):
        thread = {"turns": [{"items": [{"type": "userMessage", "content": [{"type": "text", "text": "[TAILMSG:old_message]"}]}]}, {"items": []}]}
        self.assertEqual([], _user_markers(thread, 1))

    def test_read_setup_failure_releases_process_and_streams(self):
        for setup in ("constructor", "registration"):
            with self.subTest(setup=setup):
                process = MagicMock()
                process.poll.return_value = None
                selector = MagicMock()
                if setup == "constructor":
                    factory = patch("codex_queue_client.selectors.DefaultSelector", side_effect=OSError("selector unavailable"))
                else:
                    selector.register.side_effect = OSError("registration failed")
                    factory = patch("codex_queue_client.selectors.DefaultSelector", return_value=selector)
                with patch("codex_queue_client.subprocess.Popen", return_value=process), factory:
                    with self.assertRaises(CodexAppError) as caught:
                        self.client.call_tool("read_thread", {"threadId": "dispatcher-fixed"})
                self.assertFalse(caught.exception.delivery_unknown)
                process.terminate.assert_called_once_with()
                process.wait.assert_called_once_with(timeout=2)
                process.stdin.close.assert_called_once_with()
                process.stdout.close.assert_called_once_with()
                if setup == "registration":
                    selector.close.assert_called_once_with()


FAKE_CODEX = r"""#!%(python)s
# Fake `codex` CLI for offline tests: logs argv and CODEX_HOME, then acts as told by the mode file.
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "calls.jsonl"), "a") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "codex_home": os.environ.get("CODEX_HOME")}) + "\n")
mode = open(os.path.join(here, "mode")).read().strip()
args = sys.argv[1:]
if args[:1] == ["queue"]:
    thread = args[args.index("--thread") + 1]
    if mode == "fail":
        sys.exit(1)
    if mode == "sleep":
        time.sleep(30)
    if mode == "noreceipt":
        print("something else")
    else:
        print("Queued message %%s for thread %%s." %% ("queue_" + "0" * 12, "other-thread" if mode == "wrongthread" else thread))
    sys.exit(0)
if args[:3] == ["app-server", "--listen", "stdio://"]:
    store = os.path.join(os.environ.get("CODEX_HOME") or "/nonexistent", "fake-thread.json")
    for line in sys.stdin:
        request = json.loads(line)
        with open(os.path.join(here, "requests.jsonl"), "a") as log:
            log.write(json.dumps(request) + "\n")
        if request.get("id") == 1:
            print(json.dumps({"id": 1, "result": {}}), flush=True)
        elif request.get("id") == 2:
            if not os.path.isfile(store):
                print(json.dumps({"id": 2, "error": {"message": "thread not found"}}), flush=True)
            else:
                print(json.dumps({"id": 2, "result": {"thread": json.load(open(store))}}), flush=True)
            break
    sys.exit(0)
sys.exit(2)
"""


class UnixSocketFixture(unittest.TestCase):
    """Real temporary AF_UNIX sockets and a fake executable CLI; never a production endpoint."""

    def setUp(self):
        # AF_UNIX paths are short; mkdtemp in the system temp dir keeps them under the limit.
        self.root = Path(tempfile.mkdtemp(prefix="ishoni-sock-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.root), True)
        os.chmod(str(self.root), 0o700)
        self.thread = str(uuid.uuid4())
        self.bin = self.root / "cli bin"
        self.bin.mkdir()
        self.cli = self.bin / "codex"
        self.cli.write_text(FAKE_CODEX % {"python": sys.executable})
        self.cli.chmod(0o755)
        self.mode("ok")
        self.codex_home = self.root / "codex home"
        self.codex_home.mkdir()
        self.socket_path = self.root / "app.sock"
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.socket_path))
        self.listener.listen(8)
        self.addCleanup(self.listener.close)
        self.remote = "unix://" + str(self.socket_path)
        self.prompt = "[TAILMSG:message_0001]\nlocal metadata only"

    def mode(self, value):
        (self.bin / "mode").write_text(value)

    def calls(self):
        log = self.bin / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def client(self, **overrides):
        config = {"dispatcher_thread_id": self.thread, "codex_path": str(self.cli), "codex_remote": self.remote,
                  "codex_home": str(self.codex_home), "timeout": 5}
        config.update(overrides)
        return CodexAppClient(config)

    def send(self, client=None):
        return (client or self.client()).call_tool(
            "send_message_to_thread", {"threadId": self.thread, "hostId": "local", "prompt": self.prompt})



class UnixSocketTransportTests(UnixSocketFixture):
    def test_real_socket_and_cli_receive_exact_argv_and_codex_home(self):
        with patch.object(CodexAppClient, "_app_running") as desktop:
            result = self.send()
        desktop.assert_not_called()
        self.assertEqual("enqueued", result["status"])
        self.assertEqual("codex_queue_unix", result["transport"])
        self.assertEqual([{"argv": ["queue", "--thread", self.thread, "--message", self.prompt, "--remote", self.remote],
                           "codex_home": str(self.codex_home)}], self.calls())

    def test_runtime_missing_or_stale_is_known_unsent_without_running_cli(self):
        self.listener.close()   # the socket file stays behind: a stale socket
        missing = self.root / "missing.sock"
        regular = self.root / "regular"
        regular.write_text("not a socket")
        for path, code in ((self.socket_path, "socket_stale"), (missing, "socket_missing"),
                           (regular, "socket_invalid")):
            with self.subTest(code=code):
                with self.assertRaises(CodexAppError) as caught:
                    self.send(self.client(codex_remote="unix://" + str(path)))
                self.assertFalse(caught.exception.delivery_unknown)
                self.assertEqual(code, caught.exception.code)
        self.assertEqual([], self.calls())

    def test_probe_creation_failure_is_known_unsent(self):
        with patch("codex_queue_client.socket.socket", side_effect=OSError(24, "Too many open files")):
            with self.assertRaises(CodexAppError) as caught:
                self.send()
        self.assertFalse(caught.exception.delivery_unknown)
        self.assertEqual("socket_probe_failed", caught.exception.code)
        self.assertEqual([], self.calls())

    def test_unexpected_check_failure_before_send_is_known_unsent(self):
        with patch("codex_queue_client.unix_socket_problem", side_effect=RuntimeError("unexpected")):
            with self.assertRaises(CodexAppError) as caught:
                self.send()
        self.assertFalse(caught.exception.delivery_unknown)
        self.assertEqual([], self.calls())

    def test_foreign_owner_and_shared_directory_are_refused(self):
        real_stat = os.stat
        def foreign(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if str(path) == str(self.socket_path):
                return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1)
            return info
        with patch("codex_queue_client.os.stat", side_effect=foreign):
            self.assertEqual("socket_foreign", unix_socket_problem(str(self.socket_path))[0])
        os.chmod(str(self.root), 0o777)
        self.addCleanup(os.chmod, str(self.root), 0o700)
        self.assertEqual("socket_directory_unsafe", unix_socket_problem(str(self.socket_path))[0])
        os.chmod(str(self.root), 0o1777)
        self.assertIsNone(unix_socket_problem(str(self.socket_path)))

    def test_symlinked_socket_checks_the_endpoint(self):
        link = self.root / "control.sock"
        link.symlink_to(self.socket_path)
        self.assertIsNone(unix_socket_problem(str(link)))

    def test_attempt_failures_after_start_are_uncertain(self):
        for mode, code in (("fail", "queue_failed"), ("noreceipt", "queue_receipt_unknown"),
                           ("wrongthread", "queue_receipt_unknown"), ("sleep", "queue_timeout")):
            with self.subTest(mode=mode):
                self.mode(mode)
                with self.assertRaises(CodexAppError) as caught:
                    self.send(self.client(timeout=1))
                self.assertTrue(caught.exception.delivery_unknown)
                self.assertEqual(code, caught.exception.code)

    def test_invalid_remotes_and_names_are_rejected_before_any_call(self):
        for remote in ("ws://127.0.0.1:4500", "wss://example.com", "unix://", "unix://relative.sock",
                       "unix:/tmp/codex.sock", "unix:///", "unix:////server/sock", "unix:///tmp/../tmp/x.sock",
                       "unix:///tmp//x.sock", "unix:///tmp/x.sock?a", "unix:///tmp/x.sock#a", "unix:///tmp/x\n",
                       "unix:///tmp/x\x00", "", False, ["unix:///tmp/x.sock"]):
            with self.subTest(remote=remote), self.assertRaises(CodexAppError) as caught:
                self.client(codex_remote=remote)
            self.assertEqual("invalid_config", caught.exception.code)
        for thread in ("dispatcher-name", self.thread.upper(), "{%s}" % self.thread):
            with self.subTest(thread=thread), self.assertRaises(CodexAppError):
                self.client(dispatcher_thread_id=thread)
        for overrides in ({"codex_path": None}, {"codex_path": "codex"}, {"codex_home": "relative/home"}):
            with self.subTest(overrides=overrides), self.assertRaises(CodexAppError):
                self.client(**overrides)
        self.assertEqual([], self.calls())

    def test_other_threads_and_hosts_are_refused(self):
        client = self.client()
        for key, value in (("threadId", str(uuid.uuid4())), ("hostId", "remote")):
            with self.subTest(key=key), self.assertRaises(CodexAppError) as caught:
                client.call_tool("send_message_to_thread", {"threadId": self.thread, "hostId": "local",
                                                            "prompt": self.prompt, key: value})
            self.assertFalse(caught.exception.delivery_unknown)
        self.assertEqual([], self.calls())

    def test_reconciliation_reads_the_configured_codex_home_without_the_socket(self):
        thread = {"id": self.thread, "turns": [{"items": [
            {"type": "userMessage", "content": [{"type": "text", "text": self.prompt}]}]}]}
        (self.codex_home / "fake-thread.json").write_text(json.dumps(thread))
        self.listener.close()
        os.unlink(str(self.socket_path))
        result = self.client().call_tool("read_thread", {"threadId": self.thread})
        self.assertEqual(["[TAILMSG:message_0001]"], result["tailmsgMarkers"])
        self.assertEqual({"argv": ["app-server", "--listen", "stdio://"], "codex_home": str(self.codex_home)},
                         self.calls()[-1])
        other = self.root / "another account"
        other.mkdir()
        with self.assertRaises(CodexAppError):
            self.client(codex_home=str(other)).call_tool("read_thread", {"threadId": self.thread})


class SessionNameTests(UnixSocketFixture):
    """Metadata-only native name read: thread/read with includeTurns false; Thread.name only."""

    def requests(self):
        log = self.bin / "requests.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def store(self, **thread):
        thread.setdefault("id", self.thread)
        (self.codex_home / "fake-thread.json").write_text(json.dumps(thread))

    def test_name_is_read_without_turns_and_preview_is_never_used(self):
        self.store(name="Triage inbox", preview="PRIVATE first user message", status={"type": "notLoaded"},
                   turns=[{"items": [{"type": "userMessage", "content": [{"type": "text", "text": "PRIVATE"}]}]}])
        result = self.client().read_thread_name()
        self.assertEqual({"thread_id": self.thread, "name": "Triage inbox"}, result)
        reads = [r for r in self.requests() if r.get("method") == "thread/read"]
        self.assertEqual([{"threadId": self.thread, "includeTurns": False}], [r["params"] for r in reads])
        self.assertEqual([], [c for c in self.calls() if c["argv"][0] == "queue"])   # no queue, no turn

    def test_missing_name_falls_back_to_none_not_preview(self):
        self.store(preview="PRIVATE first user message")
        self.assertEqual({"thread_id": self.thread, "name": None}, self.client().read_thread_name())
        self.store(name="   ", preview="PRIVATE")
        self.assertIsNone(self.client().read_thread_name()["name"])

    def test_bridge_session_name_command(self):
        self.store(name="Renamed later")
        config = Path(self.root) / "bridge.json"
        config.write_text(json.dumps({"bridge_home": str(self.root / "state"), "tailmsg_home": str(self.root),
                                      "dispatcher_thread_id": self.thread, "codex_path": str(self.cli),
                                      "codex_remote": self.remote, "codex_home": str(self.codex_home)}))
        script = Path(__file__).resolve().with_name("codex_bridge.py")
        run = lambda: subprocess.run([sys.executable, str(script), "--config", str(config), "session-name"],
                                     capture_output=True, text=True, timeout=30)
        r = run()
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertEqual({"thread_id": self.thread, "name": "Renamed later"}, json.loads(r.stdout))
        self.assertFalse((self.root / "state").exists())               # no queue database is created
        (self.codex_home / "fake-thread.json").unlink()
        r = run()
        self.assertEqual(1, r.returncode)
        self.assertEqual("codex_bridge: read_failed", r.stderr.strip())


class UnixSocketBridgeTests(UnixSocketFixture):
    """The durable queue with the real client: retry when unsent, never resend when uncertain."""

    def setUp(self):
        super().setUp()
        self.tailmsg = self.root / "tailmsg"
        (self.tailmsg / "inbox").mkdir(parents=True)
        (self.tailmsg / "config.json").write_text(json.dumps({"allow": ["peer-node"], "default_agent": "codex"}))
        self.now = 1700000000.0
        self.bridge = Bridge({"bridge_home": str(self.root / "state"), "tailmsg_home": str(self.tailmsg),
                              "dispatcher_thread_id": self.thread, "codex_path": str(self.cli),
                              "codex_remote": self.remote, "codex_home": str(self.codex_home), "timeout": 1,
                              "retry_seconds": 10}, clock=lambda: self.now)
        self.addCleanup(self.bridge.close)
        received = datetime.datetime.fromtimestamp(self.now, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        record = {"envelope": {"v": 1, "id": "message_0001", "from": "peer-node.example.ts.net", "agent": "codex",
                               "text": "PRIVATE body"},
                  "received": {"at": received, "peer": {"node": "peer-node.example.ts.net"}, "agent": "codex",
                               "wake": {"effective": "now"}}}
        (self.tailmsg / "inbox" / "message_0001.json").write_text(json.dumps(record))
        self.bridge.scan()

    def row(self):
        return self.bridge.status()[0]

    def test_stale_socket_retries_then_delivers_once_runtime_returns(self):
        self.listener.close()
        self.assertEqual(1, self.bridge.dispatch_due()["retry"])
        self.assertEqual(("retry", "socket_stale"), (self.row()["state"], self.row()["error"]))
        self.assertEqual([], self.calls())
        os.unlink(str(self.socket_path))
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.socket_path))
        listener.listen(8)
        self.addCleanup(listener.close)
        self.now += 10
        self.assertEqual(1, self.bridge.dispatch_due()["enqueued"])
        argv = self.calls()[0]["argv"]
        self.assertEqual(["queue", "--thread", self.thread], argv[:3])
        self.assertNotIn("PRIVATE", json.dumps(self.calls()))

    def test_failed_attempt_is_uncertain_and_never_resent(self):
        self.mode("fail")
        self.assertEqual(1, self.bridge.dispatch_due()["uncertain"])
        self.assertEqual(("uncertain", "queue_failed"), (self.row()["state"], self.row()["error"]))
        self.mode("ok")
        for _ in range(3):
            self.now += 10
            self.assertEqual(1, self.bridge.dispatch_due()["uncertain"])
        queued = [call for call in self.calls() if call["argv"][0] == "queue"]
        self.assertEqual(1, len(queued))
        self.assertTrue(all(call["codex_home"] == str(self.codex_home) for call in self.calls()))
        thread = {"id": self.thread, "turns": [{"items": [
            {"type": "userMessage", "content": [{"type": "text", "text": "[TAILMSG:message_0001]"}]}]}]}
        (self.codex_home / "fake-thread.json").write_text(json.dumps(thread))
        self.now += 10
        self.assertEqual(1, self.bridge.dispatch_due()["delivered"])
        self.assertEqual(1, len([call for call in self.calls() if call["argv"][0] == "queue"]))


if __name__ == "__main__":
    unittest.main()
