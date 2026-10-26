"""Team rooms (bot_rooms.py): one group chat for the agents of several owners, with permission isolation.

Through the CLI, with a separate synthetic home per role: the administrator's owner device, two colleagues' owner
devices (self-enrolled bots with end-to-end keys), and each bot's agent device holding only its native binding and
the bot's key. Checked here: membership needs the bot's own owner; A's agent reads and writes nothing of B's beyond
the room; a task request for B is withheld from B's agent until B's owner approves it by hash; only the publisher
updates a data item; a removed member reads nothing written after; bodies reach the gateway sealed for exactly the
active members. Real bot_gateway.py subprocesses with synthetic homes on loopback.
"""
import itertools
import os
import urllib.error
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support.public_harness import C, PUBLIC, FriendsGateway, cli, plain_env

import bot_e2e as E  # noqa: E402  (on sys.path through the harness)

LIMITS = {"read_timeout_seconds": 5, "messages_per_minute_per_actor": 1000, "messages_per_hour_per_actor": 5000,
          "enroll_per_hour_per_ip": 1000, "enroll_per_hour": 1000, "bot_enroll_per_hour": 1000,
          "bot_enroll_per_hour_per_ip": 1000, "room_posts_per_hour_per_member": 1000}
COUNTER = itertools.count(1)


class RoomCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="ishoni rooms ")
        cls.root = Path(cls._tmp.name)
        cls.gw = FriendsGateway(cls.root, limits=LIMITS).start()

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls._tmp.cleanup()

    def setUp(self):
        self.n = next(COUNTER)
        roles = ("admin", "owner-a", "owner-b", "owner-c", "agent-a", "agent-b", "agent-c")
        self.homes = {r: self.root / ("%s-%d" % (r, self.n)) for r in roles}
        self.workers = {}
        self.y("admin", "owner", "login", self.gw.url, "--token-file", str(self.gw.home / "owner.token"))

    def y(self, role, *args, expect=0):
        code, obj, err = cli(self.homes[role], *args)
        self.assertEqual(expect, code, "%s %s\n%s\n%s" % (role, args, obj, err))
        return obj if expect == 0 else (obj, err)

    def colleague(self, name):
        """A colleague's own bot (its owner device) bound to its agent device: -> bot id."""
        owner, agent = "owner-" + name, "agent-" + name
        made = self.y(owner, "enroll-bot", self.gw.url, "--handle", "%s-%d" % (name, self.n), "--name", name.upper())
        token_file = self.root / ("native-%s-%d.token" % (name, self.n))
        bound = self.y(owner, "worker", "bind", made["bot_id"], "--adapter", "native", "--token-file", str(token_file))
        self.workers[made["bot_id"]] = bound["worker_id"]
        self.y(agent, "worker", "login", self.gw.url, "--token-file", str(token_file))
        return made["bot_id"]

    def team(self):
        a, b = self.colleague("a"), self.colleague("b")
        room = self.y("admin", "room", "create", "Project %d" % self.n)["room_id"]
        for bot in (a, b):
            self.y("admin", "room", "invite", room, bot)
        self.y("owner-a", "room", "accept", room, a)
        self.y("owner-b", "room", "accept", room, b)
        return room, a, b

    def test_join_does_the_whole_colleague_setup_in_one_command(self):
        """A colleague gets an owner credential file from the administrator and runs `join` on their own device:
        bot, key, native binding and every waiting invitation, idempotently."""
        token_file = self.root / ("owner-d-%d.token" % self.n)
        r = subprocess.run(self.gw.prefix + ["--gateway-home", str(self.gw.home), "owner", "add", "--label", "D",
                                             "--token-file", str(token_file)], capture_output=True, text=True,
                           env=self.gw.env, timeout=60)
        self.assertEqual(0, r.returncode, r.stderr)
        home = self.root / ("colleague-d-%d" % self.n)
        self.homes["colleague-d"] = home
        first = self.y("colleague-d", "join", self.gw.url, "--token-file", str(token_file), "--handle",
                       "dana-%d" % self.n, "--name", "Dana's agent")
        self.assertEqual((True, True, [], [], "@dana-%d" % self.n),
                         (first["created"], first["bound_this_device"], first["joined_now"], first["rooms"],
                          first["handle"]))
        self.assertTrue(first["e2e"]["key"], "the bot got an end-to-end key")
        bot = first["bot_id"]
        room = self.y("admin", "room", "create", "Onboarding %d" % self.n)["room_id"]
        self.y("admin", "room", "invite", room, bot)
        again = self.y("colleague-d", "join", self.gw.url, "--token-file", str(token_file), "--handle",
                       "dana-%d" % self.n)
        self.assertEqual((False, False, [room], [room]),
                         (again["created"], again["bound_this_device"], [x["room_id"] for x in again["joined_now"]],
                          [x["room_id"] for x in again["rooms"]]))
        # the same device now reads and posts as the bot, sealed for the members
        posted = self.y("colleague-d", "room", "post", bot, room, "--text", "hello from Dana")
        self.assertEqual("posted", posted["state"])
        self.assertEqual(["hello from Dana"],
                         [m["text"] for m in self.y("colleague-d", "room", "sync", bot, room, "--after", "0")["messages"]])

    def test_membership_needs_the_bots_own_owner(self):
        a, b = self.colleague("a"), self.colleague("b")
        room = self.y("admin", "room", "create", "Team")["room_id"]
        self.y("admin", "room", "invite", room, a)
        # B's owner cannot accept for A's bot, and A's agent cannot read before A's owner accepted
        obj, _ = self.y("owner-b", "room", "accept", room, a, expect=2)
        self.assertIn("not_found", json.dumps(obj))
        obj, _ = self.y("agent-a", "room", "sync", a, room, expect=2)
        self.assertIn("not_found", json.dumps(obj))
        joined = self.y("owner-a", "room", "accept", room, a)
        self.assertEqual(("active", 1), (joined["member"]["state"], joined["room"]["epoch"]))
        mine = self.y("agent-a", "room", "rooms", a)["rooms"]
        self.assertEqual([(room, "active", "member")], [(r["room_id"], r["membership"], r["role"]) for r in mine])
        # B, never invited: 404 on every room route, as for a room that does not exist
        for args in (("agent-b", "room", "sync", b, room), ("owner-b", "room", "show", room)):
            obj, _ = self.y(*args, expect=2)
            self.assertIn("not_found", json.dumps(obj))

    def test_posts_are_sealed_for_exactly_the_members_and_a_removed_member_reads_nothing_after(self):
        room, a, b = self.team()
        posted = self.y("agent-a", "room", "post", a, room, "--text", "progress: parser done", "--mention", b)
        self.assertEqual(("posted", 1), (posted["state"], posted["seq"]))
        stored = self.gw.sql("SELECT content FROM room_messages WHERE id=?", (posted["message_id"],))[0]["content"]
        self.assertNotIn("parser done", stored, "the gateway stores ciphertext")
        env = json.loads(stored)["sealed"]
        keys = {E.kid(x["e2e_key"]) for x in self.gw.sql("SELECT e2e_key FROM bots WHERE id IN (?,?)", (a, b))}
        self.assertEqual(keys, set(E.recipients_of(env)), "sealed for exactly the two active members")
        seen = self.y("agent-b", "room", "sync", b, room, "--mark-read")
        self.assertEqual(["progress: parser done"], [m["text"] for m in seen["messages"]])
        self.assertEqual((True, a), (seen["messages"][0]["e2e"]["opened"], seen["messages"][0]["e2e"]["sealed_by"]))
        self.assertEqual(1, seen["read"])
        self.assertEqual(0, self.y("agent-b", "room", "rooms", b)["rooms"][0]["unread"])
        # a third colleague joins: a new epoch, and a post sealed for the old set is refused by the gateway
        c = self.colleague("c")
        self.y("admin", "room", "invite", room, c)
        self.y("owner-c", "room", "accept", room, c)
        self.assertEqual(3, self.y("owner-c", "room", "show", room)["epoch"])
        second = self.y("agent-b", "room", "post", b, room, "--text", "welcome C")
        self.assertEqual(2, second["seq"])
        self.assertEqual(["welcome C"], [m["text"] for m in self.y("agent-c", "room", "sync", c, room)["messages"]],
                         "a member reads from where it joined, never what came before")
        obj, _ = self.y("agent-c", "room", "sync", c, room, "--after", "0", expect=0), None
        self.assertEqual([2], [m["seq"] for m in obj["messages"]])
        # B is removed by the administrator: B reads nothing after, and A's next post is sealed for A and C only
        self.y("admin", "room", "remove", room, b)
        obj, _ = self.y("agent-b", "room", "sync", b, room, expect=2)
        self.assertIn("not_found", json.dumps(obj))
        third = self.y("agent-a", "room", "post", a, room, "--text", "after B left")
        stored = self.gw.sql("SELECT content FROM room_messages WHERE id=?", (third["message_id"],))[0]["content"]
        kid_b = E.kid(self.gw.sql("SELECT e2e_key FROM bots WHERE id=?", (b,))[0]["e2e_key"])
        self.assertNotIn(kid_b, E.recipients_of(json.loads(stored)["sealed"]))
        self.assertEqual(["welcome C", "after B left"],
                         [m["text"] for m in self.y("agent-c", "room", "sync", c, room)["messages"]])

    def test_a_task_request_is_withheld_until_the_targets_owner_approves_it(self):
        room, a, b = self.team()
        task = self.y("agent-a", "room", "task", a, room, "--to", b, "--text", "please re-run the export")
        self.assertEqual(("posted", "pending"), (task["state"], task["task_state"]))
        seen = self.y("agent-b", "room", "sync", b, room)["messages"][0]
        self.assertEqual("awaiting_owner_approval", seen["withheld"]["reason"])
        self.assertNotIn("re-run", json.dumps(seen), "B's agent does not see the request before B approved it")
        obj, _ = self.y("agent-b", "room", "task-update", b, room, task["message_id"], "--status", "accepted",
                        "--text", "on it", expect=2)
        self.assertIn("task_not_approved", json.dumps(obj))
        # A's owner cannot approve a task addressed to B's bot; B's owner sees it in full and approves by hash
        self.assertEqual([], self.y("owner-a", "room", "reviews", room)["task_requests"])
        waiting = self.y("owner-b", "room", "reviews", room)["task_requests"]
        self.assertEqual(("please re-run the export", "untrusted"), (waiting[0]["text"], waiting[0]["trust"]))
        obj, _ = self.y("owner-a", "room", "approve", room, task["message_id"], "--content-hash",
                        waiting[0]["content_hash"], expect=2)
        self.assertIn("not_found", json.dumps(obj))
        obj, _ = self.y("owner-b", "room", "approve", room, task["message_id"], "--content-hash", "0" * 64, expect=2)
        self.assertIn("hash_mismatch", json.dumps(obj))
        done = self.y("owner-b", "room", "approve", room, task["message_id"], "--content-hash",
                      waiting[0]["content_hash"])
        self.assertEqual("approved", done["task_state"])
        self.assertTrue(self.gw.sql("SELECT * FROM room_decisions WHERE id=?", (done["decision_id"],)))
        seen = self.y("agent-b", "room", "sync", b, room)["messages"][0]
        self.assertEqual(("please re-run the export", "approved"), (seen["text"], seen["task_state"]))
        update = self.y("agent-b", "room", "task-update", b, room, task["message_id"], "--status", "done",
                        "--text", "export re-run")
        self.assertEqual("posted", update["state"])
        # A's connector is woken for the update to its own request, with ids only
        token_file = self.root / ("connector-a-%d.token" % self.n)
        self.y("owner-a", "connector", "bind", a, self.workers[a], "--token-file", str(token_file))
        self.y("agent-a", "connector", "login", self.gw.url, "--token-file", str(token_file))
        listing = C.api_call(self.gw.url, "GET", "/v1/connector/jobs", None,
                             json.loads((self.homes["agent-a"] / "public" / "credentials.json").read_text())
                             ["gateways"][self.gw.url]["connectors"][a]["token"])[1]
        self.assertEqual([(room, 2, "task_update")], [(r["room_id"], r["seq"], r["reason"]) for r in listing["rooms"]])
        self.assertNotIn("export", json.dumps(listing))

    def test_only_the_publisher_updates_a_data_item(self):
        room, a, b = self.team()
        v1 = self.y("agent-a", "room", "data", a, room, "progress", "--text", "parser: 80%")
        self.assertEqual(("progress", 1), (v1["name"], v1["version"]))
        obj, _ = self.y("agent-b", "room", "data", b, room, "progress", "--text", "parser: 100%", expect=2)
        self.assertIn("data_owned_by_other", json.dumps(obj))
        v2 = self.y("agent-a", "room", "data", a, room, "progress", "--text", "parser: 90%")
        self.assertEqual(2, v2["version"])
        got = self.y("agent-b", "room", "data-get", b, room, "progress")
        self.assertEqual(("parser: 90%", 2), (got["text"], got["version"]))
        listed = self.y("agent-b", "room", "data-list", b, room)["data"]
        self.assertEqual([("progress", a, 2)], [(d["name"], d["publisher"]["bot_id"], d["version"]) for d in listed])

    def test_review_mode_holds_the_agents_posts_for_its_owner(self):
        a, b = self.colleague("a"), self.colleague("b")
        room = self.y("admin", "room", "create", "Reviewed")["room_id"]
        for bot in (a, b):
            self.y("admin", "room", "invite", room, bot)
        self.y("owner-a", "room", "accept", room, a, "--mode", "review")
        self.y("owner-b", "room", "accept", room, b)
        draft = self.y("agent-a", "room", "post", a, room, "--text", "draft from A")
        self.assertEqual(("draft", None), (draft["state"], draft["seq"]))
        self.assertEqual([], self.y("agent-b", "room", "sync", b, room)["messages"], "nobody sees a draft")
        drafts = self.y("owner-a", "room", "reviews", room)["drafts"]
        self.assertEqual(["draft from A"], [d["text"] for d in drafts])
        self.assertEqual([], self.y("owner-b", "room", "reviews", room)["drafts"], "B's owner never sees A's drafts")
        released = self.y("owner-a", "room", "approve", room, draft["message_id"], "--content-hash",
                          drafts[0]["content_hash"])
        self.assertEqual(("posted", 1), (released["state"], released["seq"]))
        self.assertEqual(["draft from A"], [m["text"] for m in self.y("agent-b", "room", "sync", b, room)["messages"]])
        # a retry under the same op key is the same message, never a second post
        again = self.y("agent-a", "room", "post", a, room, "--text", "draft from A", "--op-key", draft["op_key"])
        self.assertEqual((True, draft["message_id"]), (again["duplicate"], again["message_id"]))

    def test_room_credentials_reach_nothing_outside_the_room(self):
        room, a, b = self.team()
        agent_creds = json.loads((self.homes["agent-a"] / "public" / "credentials.json").read_text())
        wtoken = agent_creds["gateways"][self.gw.url]["workers"][a]["token"]
        # A's binding: no owner route, no other bot's jobs, no room it is not in
        for method, path in (("GET", "/v1/owner/rooms"), ("GET", "/v1/owner/bots/%s/reviews" % b),
                             ("GET", "/v1/rooms/room_aaaaaaaaaaaaaaaa/messages")):
            status, obj = C.api_call(self.gw.url, method, path, None, wtoken)
            self.assertIn(status, (401, 404), (path, status, obj))
        # the room administrator's credential: no route into a member's conversations or bindings
        for path in ("/v1/owner/bots/%s/reviews" % a, "/v1/owner/bots/%s/workers" % a,
                     "/v1/owner/bots/%s/conversations" % a):
            status, obj = C.api_call(self.gw.url, "GET", path, None, self.gw.owner_token)
            self.assertEqual(404, status, (path, obj))
        # a body field naming another sender is refused: the sender is the credential
        status, obj = C.api_call(self.gw.url, "POST", "/v1/rooms/%s/messages" % room,
                                 {"op_key": "forgedop0001", "kind": "post", "epoch": 2, "bot_id": b, "text": "x"},
                                 wtoken)
        self.assertEqual((400, "unknown_field"), (status, obj["error"]))
        # plain text is refused in an end-to-end room; an envelope for the wrong key set is refused
        status, obj = C.api_call(self.gw.url, "POST", "/v1/rooms/%s/messages" % room,
                                 {"op_key": "plainop00001", "kind": "post", "epoch": 2, "text": "x"}, wtoken)
        self.assertEqual("e2e_required", obj["error"])
        mine = agent_creds["gateways"][self.gw.url]["bot_keys"][a][-1]
        env = E.seal(b"x", mine["secret"], [mine["public"]], "ishoni-room/1")
        status, obj = C.api_call(self.gw.url, "POST", "/v1/rooms/%s/messages" % room,
                                 {"op_key": "wrongkeys001", "kind": "post", "epoch": 2, "sealed": env}, wtoken)
        self.assertEqual("sealed_recipients_mismatch", obj["error"])

    # ---- MCP: an agent that can only speak MCP (Muse) takes part through the stdio proxy or straight at /mcp
    def stdio(self, role, bot_id, calls):
        init = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
        msgs = [init] + [{"jsonrpc": "2.0", "id": i + 1, "method": "tools/call", "params": {"name": n, "arguments": a}}
                         for i, (n, a) in enumerate(calls)]
        proc = subprocess.run([sys.executable, str(PUBLIC / "bot_client.py"), "--home", str(self.homes[role]), "mcp",
                               "--host", bot_id], input="".join(json.dumps(m) + "\n" for m in msgs).encode("utf-8"),
                              capture_output=True, timeout=60, env=plain_env(self.root))
        self.assertEqual(0, proc.returncode, proc.stderr.decode("utf-8"))
        out = [json.loads(line) for line in proc.stdout.decode("utf-8").splitlines()]
        self.assertEqual([m["id"] for m in msgs], [m.get("id") for m in out])
        return [m["result"] for m in out[1:]]

    def test_an_mcp_host_takes_part_through_the_stdio_proxy_with_end_to_end_encryption(self):
        room, a, b = self.team()
        self.y("agent-a", "room", "post", a, room, "--text", "A: parser done", "--mention", b)
        results = self.stdio("agent-b", b, [
            ("ishoni_room_list", {}),
            ("ishoni_room_sync", {"room": room}),
            ("ishoni_room_post", {"room": room, "op_key": "mcp-post-0001", "text": "B: exporter started",
                                  "kind": "status"}),
            ("ishoni_room_post", {"room": room, "op_key": "mcp-post-0001", "text": "B: exporter started",
                                  "kind": "status"}),
            ("ishoni_room_read", {"room": room, "seq": 1}),
            ("ishoni_room_post", {"room": room, "op_key": "mcp-post-0002", "text": "x", "kind": "data"}),
        ])
        rooms, synced, posted, again, read, bad = results
        self.assertEqual([room], [r["room_id"] for r in rooms["structuredContent"]["rooms"]])
        self.assertEqual(["A: parser done"], [m["text"] for m in synced["structuredContent"]["messages"]],
                         "sealed bodies are opened on the agent's device, never shown as envelopes")
        self.assertFalse([m for m in synced["structuredContent"]["messages"] if "sealed" in m])
        self.assertEqual(("posted", 2, False), (posted["structuredContent"]["state"], posted["structuredContent"]["seq"],
                                                posted["isError"]))
        self.assertEqual(True, again["structuredContent"]["duplicate"], "a retry under the same op_key posts once")
        self.assertEqual(1, read["structuredContent"]["cursor"])
        self.assertEqual("invalid_fields", bad["structuredContent"]["error"], "gateway errors come back as tool errors")
        stored = self.gw.sql("SELECT content FROM room_messages WHERE id=?", (posted["structuredContent"]["message_id"],))
        self.assertNotIn("exporter started", stored[0]["content"], "the proxy sealed it: the gateway holds ciphertext")
        seen = self.y("agent-a", "room", "sync", a, room)["messages"]
        self.assertEqual([("status", "B: exporter started", b)], [(m["kind"], m["text"], m["e2e"]["sealed_by"])
                                                                   for m in seen if m["seq"] == 2])

    def test_the_http_bridge_serves_the_same_proxy_behind_its_own_bearer(self):
        import urllib.request
        room, a, b = self.team()
        self.y("agent-a", "room", "post", a, room, "--text", "A: parser done", "--mention", b)
        bearer = self.root / "bridge.secret"
        bearer.write_text("bridge-secret-0123456789abcdef\n")
        os.chmod(str(bearer), 0o600)
        r, w = os.pipe()
        proc = subprocess.Popen([sys.executable, str(PUBLIC / "bot_client.py"), "--home", str(self.homes["agent-b"]),
                                 "mcp", "--host", b, "--listen", "127.0.0.1:0", "--bearer-file", str(bearer),
                                 "--ready-fd", str(w)], pass_fds=(w,), stderr=subprocess.DEVNULL, env=plain_env(self.root))
        os.close(w)
        try:
            port = json.loads(os.read(r, 200).decode("utf-8"))["port"]
            url = "http://127.0.0.1:%d/mcp" % port

            def call(msg, token="bridge-secret-0123456789abcdef", method="POST"):
                req = urllib.request.Request(url, data=json.dumps(msg).encode("utf-8") if msg else None, method=method,
                                             headers={"Content-Type": "application/json",
                                                      "Authorization": "Bearer " + token})
                try:
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        return resp.status, json.loads(resp.read().decode("utf-8") or "null")
                except urllib.error.HTTPError as e:
                    return e.code, json.loads(e.read().decode("utf-8") or "null")

            init = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
                    "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
            self.assertEqual(401, call(init, token="wrong-secret-0123456789abcdef")[0], "the bridge's own secret guards it")
            self.assertEqual(405, call(None, method="GET")[0])
            status, out = call(init)
            self.assertEqual((200, 0), (status, out["id"]))
            status, out = call({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self.assertEqual(202, status)
            status, synced = call({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "ishoni_room_sync", "arguments": {"room": room}}})
            self.assertEqual(["A: parser done"], [m["text"] for m in synced["result"]["structuredContent"]["messages"]],
                             "sealed bodies are opened on this device before they go back over the bridge")
            status, posted = call({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                   "params": {"name": "ishoni_room_post",
                                              "arguments": {"room": room, "op_key": "bridge-post-0001",
                                                            "text": "B: via the bridge", "kind": "status"}}})
            self.assertEqual("posted", posted["result"]["structuredContent"]["state"])
            stored = self.gw.sql("SELECT content FROM room_messages WHERE id=?",
                                 (posted["result"]["structuredContent"]["message_id"],))
            self.assertNotIn("via the bridge", stored[0]["content"], "the gateway holds ciphertext")
            seen = self.y("agent-a", "room", "sync", a, room)["messages"]
            self.assertIn("B: via the bridge", [m["text"] for m in seen])
        finally:
            os.close(r)
            proc.terminate()
            proc.wait(timeout=10)

    def test_a_remote_mcp_client_posts_plain_text_only_where_the_room_allows_it(self):
        room, a, b = self.team()
        token = (self.root / ("native-b-%d.token" % self.n)).read_text().strip()
        res = self.gw.tool(token, "ishoni_room_post", {"room": room, "op_key": "remote-post-01", "text": "plain"})
        self.assertEqual((True, "e2e_required"), (res["isError"], res["structuredContent"]["error"]))
        plain = self.y("admin", "room", "create", "Plain %d" % self.n, "--no-e2e")["room_id"]
        for bot, owner in ((a, "owner-a"), (b, "owner-b")):
            self.y("admin", "room", "invite", plain, bot)
            self.y(owner, "room", "accept", plain, bot)
        res = self.gw.tool(token, "ishoni_room_post", {"room": plain, "op_key": "remote-post-02", "text": "plain"})
        self.assertEqual((False, "posted"), (res["isError"], res["structuredContent"]["state"]))
        seen = self.gw.tool(token, "ishoni_room_sync", {"room": plain, "after": 0})["structuredContent"]["messages"]
        self.assertEqual(["plain"], [m["text"] for m in seen])
        self.assertEqual(["plain"], [m["text"] for m in self.y("agent-a", "room", "sync", a, plain)["messages"]])
        # the room's tools are the binding's alone: an owner or caller credential has none
        res = self.gw.mcp(self.gw.owner_token, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                 "params": {"name": "ishoni_room_list", "arguments": {}}})
        self.assertIn(res[0], (401, 403), res)


class ForwardedForCase(unittest.TestCase):
    """`trust_forwarded_for`: `true` believes X-Forwarded-For from a proxy on this host only, `"private"` also from
    one on the private network (a container behind the platform's edge proxy), never from a public address."""

    def test_proxy_peer_modes(self):
        import bot_gateway as G
        self.assertFalse(G.proxy_peer("127.0.0.1", False))
        self.assertTrue(G.proxy_peer("127.0.0.1", True))
        self.assertFalse(G.proxy_peer("10.0.0.7", True))
        self.assertTrue(G.proxy_peer("10.0.0.7", "private"))
        self.assertTrue(G.proxy_peer("fd00::1", "private"))
        self.assertFalse(G.proxy_peer("203.0.113.9", "private"))
        self.assertFalse(G.proxy_peer("not-an-ip", "private"))

    def test_config_accepts_private_and_refuses_other_strings(self):
        import bot_gateway as G
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "gateway.json"
            base = {"v": 1, "bind": "127.0.0.1", "port": 7780, "public_base_url": "http://127.0.0.1:7780",
                    "limits": {}, "enrollment": {}, "mcp": {"enabled": True, "allowed_origins": []}}
            for value, ok in ((True, True), (False, True), ("private", True), ("any", False), (1, False)):
                cfg = dict(base, trust_forwarded_for=value)
                path.write_text(json.dumps(cfg), encoding="utf-8")
                path.chmod(0o600)
                if ok:
                    self.assertEqual(value, G.load_config(home)["trust_forwarded_for"])
                else:
                    with self.assertRaises(ValueError):
                        G.load_config(home)


if __name__ == "__main__":
    unittest.main()
