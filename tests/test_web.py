"""The team web page (bot_web.py, bot_web_client.py): accounts, password vaults the gateway cannot open, sessions,
the static client, and the owner's own reading and writing of a room (the routes the browser uses).

Real bot_gateway.py subprocesses with synthetic homes on loopback, the CLI for the device side, and the HTTP API
for what the browser does.
"""
import itertools
import json
import subprocess
import tempfile
import unittest
import urllib.request
from pathlib import Path

from support.public_harness import C, FriendsGateway, cli

import bot_e2e as E  # noqa: E402  (on sys.path through the harness)
import bot_web_client as WC  # noqa: E402
from bot_room_client import ROOM_CTX, seal_body  # noqa: E402

LIMITS = {"read_timeout_seconds": 5, "enroll_per_hour_per_ip": 1000, "enroll_per_hour": 1000,
          "bot_enroll_per_hour": 1000, "bot_enroll_per_hour_per_ip": 1000, "room_posts_per_hour_per_member": 1000,
          "web_logins_per_hour_per_ip": 1000, "web_logins_per_hour_per_account": 1000}
COUNTER = itertools.count(1)
PASSWORD = "correct horse battery staple"


class WebCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="ishoni web ")
        cls.root = Path(cls._tmp.name)
        cls.gw = FriendsGateway(cls.root, limits=LIMITS).start()

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls._tmp.cleanup()

    def setUp(self):
        self.n = next(COUNTER)
        self.homes = {}
        self.pw_file = self.root / ("pw-%d" % self.n)
        C.create_private_file(self.pw_file, PASSWORD + "\n")

    def home(self, role):
        return self.homes.setdefault(role, self.root / ("%s-%d" % (role, self.n)))

    def y(self, role, *args, expect=0):
        code, obj, err = cli(self.home(role), *args)
        self.assertEqual(expect, code, "%s %s\n%s\n%s" % (role, args, obj, err))
        return obj if expect == 0 else (obj, err)

    def local(self, *args):
        r = subprocess.run(self.gw.prefix + ["--gateway-home", str(self.gw.home)] + list(args), capture_output=True,
                           text=True, env=self.gw.env, timeout=60)
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        return json.loads(r.stdout)

    def owner_file(self, label):
        path = self.root / ("%s-%d.token" % (label, self.n))
        self.local("owner", "add", "--label", label, "--token-file", str(path))
        return path

    def colleague(self, name, email=None):
        """A colleague's device: owner login, bot with key and binding (join), and a web account enrolled."""
        token_file = self.owner_file(name)
        joined = self.y(name, "join", self.gw.url, "--token-file", str(token_file), "--handle", "%s-%d" % (name, self.n))
        email = email or "%s-%d@example.com" % (name, self.n)
        code = self.local("web", "account", "add", "--email", email)["setup_code"]
        enrolled = self.y(name, "web", "enroll", "--email", email, "--code", code, "--password-file", str(self.pw_file))
        self.assertEqual(("active", email), (enrolled["account"]["state"], enrolled["account"]["email"]))
        return joined["bot_id"], email

    def login(self, email, password=PASSWORD):
        _, login_key = WC.derive(password, email)
        return self.gw.call("POST", "/v1/web/login", {"email": email, "login_key": E.b64e(login_key)})

    def test_the_vault_opens_only_with_the_password_and_the_gateway_holds_ciphertext(self):
        bot, email = self.colleague("alice")
        status, res = self.login(email)
        self.assertEqual((200, True), (status, res["ok"]))
        session = res["token"]
        me = self.gw.ok("GET", "/v1/web/me", None, session)
        self.assertEqual((email, "member", 1), (me["account"]["email"], me["account"]["role"], me["vault"]["version"]))
        self.assertEqual(WC.KDF["iterations"], me["kdf"]["iterations"])
        vault_key, _ = WC.derive(PASSWORD, email)
        plain = WC.open_vault(me["vault"], vault_key, email)
        creds = json.loads((self.home("alice") / "public" / "credentials.json").read_text())
        entry = creds["gateways"][self.gw.url]
        self.assertEqual(entry["owner"]["token"], plain["owner"]["token"], "the owner credential travels in the vault")
        self.assertEqual(entry["bot_keys"][bot][-1]["secret"], plain["bot_keys"][bot][-1]["secret"])
        self.assertEqual([bot], [b["bot_id"] for b in plain["bots"]])
        stored = self.gw.sql("SELECT vault FROM web_vaults")[0]["vault"]
        self.assertNotIn(plain["owner"]["token"], stored, "the gateway stores ciphertext")
        self.assertNotIn(plain["bot_keys"][bot][-1]["secret"], stored)
        with self.assertRaises(E.E2EError):
            WC.open_vault(me["vault"], WC.derive("wrong password here", email)[0], email)
        # the CLI opens a saved copy the same way
        saved = self.root / ("vault-%d.json" % self.n)
        saved.write_text(json.dumps(me))
        opened = self.y("alice", "web", "open", "--email", email, "--file", str(saved), "--password-file",
                        str(self.pw_file))
        self.assertEqual(plain["owner"]["token"], opened["vault"]["owner"]["token"])
        # a wrong password and an unknown address get the same answer; the login key never reaches the database
        self.assertEqual((401, "invalid_login"), self.login(email, "not the password")[0:1] + (self.login(email, "not the password")[1]["error"],))
        self.assertEqual("invalid_login", self.login("nobody-%d@example.com" % self.n)[1]["error"])
        _, login_key = WC.derive(PASSWORD, email)
        row = self.gw.sql("SELECT login_hash FROM web_accounts WHERE email=?", (email,))[0]
        self.assertNotEqual(E.b64e(login_key), row["login_hash"])
        # logout ends the session
        self.gw.ok("POST", "/v1/web/logout", {}, session)
        self.assertEqual(401, self.gw.call("GET", "/v1/web/me", None, session)[0])

    def test_sync_and_a_password_change_replace_the_vault(self):
        bot, email = self.colleague("bob")
        synced = self.y("bob", "web", "sync", "--email", email, "--password-file", str(self.pw_file))
        self.assertEqual(2, synced["version"])
        session = self.login(email)[1]["token"]
        other = self.login(email)[1]["token"]
        new_pw = "a different long password"
        vault_key, login_key = WC.derive(new_pw, email)
        vault = WC.seal_vault({"v": 1, "owner": {"token": "ybo_x"}}, vault_key, email)
        changed = self.gw.ok("POST", "/v1/web/password", {"login_key": E.b64e(login_key), "vault": vault}, session)
        self.assertEqual(3, changed["version"])
        self.assertEqual(401, self.gw.call("GET", "/v1/web/me", None, other)[0], "other sessions ended")
        self.assertEqual("invalid_login", self.login(email)[1]["error"])
        me = self.gw.ok("GET", "/v1/web/me", None, self.login(email, new_pw)[1]["token"])
        self.assertEqual("ybo_x", WC.open_vault(me["vault"], vault_key, email)["owner"]["token"])

    def test_administrators_manage_accounts_and_cannot_lock_themselves_out(self):
        admin_email = "lark-%d@example.com" % self.n
        code = self.local("web", "account", "add", "--email", admin_email, "--admin")["setup_code"]
        self.y("admin", "owner", "login", self.gw.url, "--token-file", str(self.gw.home / "owner.token"))
        self.y("admin", "web", "enroll", "--email", admin_email, "--code", code, "--password-file", str(self.pw_file))
        # a spent code is gone; an expired or wrong one is refused the same way
        _, login_key = WC.derive(PASSWORD, admin_email)
        vault = WC.seal_vault({"v": 1}, WC.derive(PASSWORD, admin_email)[0], admin_email)
        status, res = self.gw.call("POST", "/v1/web/enroll", {"email": admin_email, "code": code,
                                                             "login_key": E.b64e(login_key), "vault": vault})
        self.assertEqual((401, "invalid_setup"), (status, res["error"]))
        session = self.login(admin_email)[1]["token"]
        _, bob_email = self.colleague("bob")
        bob_session = self.login(bob_email)[1]["token"]
        self.assertEqual("admin_only", self.gw.call("GET", "/v1/web/accounts", None, bob_session)[1]["error"])
        listed = self.gw.ok("GET", "/v1/web/accounts", None, session)["accounts"]
        self.assertEqual({admin_email: "admin", bob_email: "member"}, {a["email"]: a["role"] for a in listed})
        bob = [a for a in listed if a["email"] == bob_email][0]
        # disable ends Bob's sessions and refuses his login with a distinct error; enable restores him
        self.gw.ok("POST", "/v1/web/accounts/%s" % bob["account_id"], {"action": "disable"}, session)
        self.assertEqual(401, self.gw.call("GET", "/v1/web/me", None, bob_session)[0])
        self.assertEqual((403, "account_disabled"), self.login(bob_email)[0:1] + (self.login(bob_email)[1]["error"],))
        self.gw.ok("POST", "/v1/web/accounts/%s" % bob["account_id"], {"action": "enable"}, session)
        self.assertEqual(200, self.login(bob_email)[0])
        # reset: a new code, no vault, no sessions; Bob enrols again from his device
        reset = self.gw.ok("POST", "/v1/web/accounts/%s" % bob["account_id"], {"action": "reset"}, session)
        self.assertEqual("invited", reset["account"]["state"])
        self.assertEqual("invalid_login", self.login(bob_email)[1]["error"])
        self.assertEqual([], self.gw.sql("SELECT 1 FROM web_vaults WHERE account_id=?", (bob["account_id"],)))
        self.y("bob", "web", "enroll", "--email", bob_email, "--code", reset["setup_code"], "--password-file",
               str(self.pw_file))
        self.assertEqual(200, self.login(bob_email)[0])
        # the only administrator cannot be disabled or demoted; adding a second one through the page works
        me = [a for a in listed if a["email"] == admin_email][0]
        for body in ({"action": "disable"}, {"action": "role", "role": "member"}):
            self.assertEqual("last_admin", self.gw.call("POST", "/v1/web/accounts/%s" % me["account_id"], body,
                                                        session)[1]["error"])
        added = self.gw.ok("POST", "/v1/web/accounts", {"email": "Carol-%d@Example.com" % self.n, "role": "admin"},
                           session)
        self.assertEqual(("carol-%d@example.com" % self.n, "admin", "invited"),
                         (added["account"]["email"], added["account"]["role"], added["account"]["state"]))
        self.assertTrue(added["setup_code"].startswith("ishoni-setup-"))
        self.assertEqual("email_taken", self.gw.call("POST", "/v1/web/accounts", {"email": bob_email}, session)[1]["error"])
        self.assertEqual(sorted([admin_email, bob_email, "carol-%d@example.com" % self.n]),
                         sorted(a["email"] for a in self.local("web", "account", "list")["accounts"]))

    def test_the_page_is_served_with_a_strict_policy(self):
        for path, kind in (("/web/", "text/html"), ("/web", "text/html"), ("/web/index.html", "text/html"),
                           ("/web/app.js", "text/javascript"), ("/web/crypto.js", "text/javascript"),
                           ("/web/app.css", "text/css")):
            with urllib.request.urlopen(self.gw.url + path, timeout=10) as r:
                self.assertEqual(200, r.status, path)
                self.assertTrue(r.headers["Content-Type"].startswith(kind), (path, r.headers["Content-Type"]))
                self.assertEqual("no-store", r.headers["Cache-Control"])
                body = r.read()
            if kind == "text/html":
                self.assertIn("script-src 'self'", r.headers["Content-Security-Policy"])
                self.assertIn(b"crypto.js", body)
        for path in ("/web/../gateway.json", "/web/nope.js", "/web/app.js/x"):
            status, obj = C.api_call(self.gw.url, "GET", path)
            self.assertEqual(404, status, path)

    def test_an_owner_reads_its_bots_rooms_and_writes_as_itself(self):
        a, email_a = self.colleague("alice")
        b, email_b = self.colleague("bob")
        self.y("admin", "owner", "login", self.gw.url, "--token-file", str(self.gw.home / "owner.token"))
        room = self.y("admin", "room", "create", "Project %d" % self.n)["room_id"]
        for bot in (a, b):
            self.y("admin", "room", "invite", room, bot)
        for who, bot in (("alice", a), ("bob", b)):
            self.y(who, "room", "accept", room, bot)
        posted = self.y("alice", "room", "post", a, room, "--text", "parser done")
        # what the browser does with the owner credential from the vault: read the room as the owner of a member
        creds = json.loads((self.home("bob") / "public" / "credentials.json").read_text())["gateways"][self.gw.url]
        owner_b, key_b = creds["owner"]["token"], creds["bot_keys"][b][-1]
        seen = self.gw.ok("GET", "/v1/owner/rooms/%s/messages" % room, None, owner_b)
        self.assertEqual(([1], [b], False), ([m["seq"] for m in seen["messages"]], [x["bot_id"] for x in seen["bots"]],
                                             seen["admin"]))
        env = seen["messages"][0]["sealed"]
        plain = json.loads(E.open_sealed(env, key_b["secret"], seen["messages"][0]["from"]["e2e_key"], ROOM_CTX))
        self.assertEqual(("parser done", room, a), (plain["text"], plain["bind"]["room_id"], plain["bind"]["bot_id"]))
        # a stranger's owner credential sees nothing; the administrator without a bot sees the metadata
        self.assertEqual(404, self.gw.call("GET", "/v1/owner/rooms/%s/messages" % room, None,
                                           C.read_token_file(self.owner_file("eve"), "owner"))[0])
        self.assertEqual(([1], True), ([m["seq"] for m in self.gw.ok("GET", "/v1/owner/rooms/%s/messages" % room,
                                                                      None, self.gw.owner_token)["messages"]], True))
        # Bob's owner writes into the room as Bob's bot, sealed in the browser for the current members
        view = self.gw.ok("GET", "/v1/owner/rooms/%s" % room, None, owner_b)
        recipients = [m["e2e_key"] for m in view["members"] if m["state"] == "active"]
        bind = {"room_id": room, "epoch": view["epoch"], "bot_id": b}
        body = dict(seal_body("Bob here: looks good, ship it", None, key_b["secret"], recipients, bind,
                              E.seed_for(key_b["secret"], "web", room, "op-%d" % self.n)),
                    bot_id=b, op_key="op-%d-web" % self.n, kind="post", epoch=view["epoch"])
        res = self.gw.ok("POST", "/v1/owner/rooms/%s/messages" % room, body, owner_b)
        self.assertEqual(("posted", 2), (res["state"], res["seq"]))
        synced = self.y("alice", "room", "sync", a, room, "--after", "1")["messages"]
        self.assertEqual([("Bob here: looks good, ship it", "owner", b)],
                         [(m["text"], m.get("author"), m["from"]["bot_id"]) for m in synced])
        # not as a bot Bob does not own, not as a bot that is not a member
        self.assertEqual(404, self.gw.call("POST", "/v1/owner/rooms/%s/messages" % room, dict(body, bot_id=a),
                                           owner_b)[0])
        # data items the owner reads
        self.y("bob", "room", "data", b, room, "schema", "--text", "id,name")
        items = self.gw.ok("GET", "/v1/owner/rooms/%s/data" % room, None, owner_b)["data"]
        self.assertEqual([("schema", 1, True)], [(d["name"], d["version"], d["readable"]) for d in items])
        got = self.gw.ok("GET", "/v1/owner/rooms/%s/data/schema" % room, None, owner_b)
        self.assertIn("sealed", got)
        # after Bob's bot is removed, Bob's owner still reads what was written before, nothing after
        self.y("admin", "room", "remove", room, b)
        self.y("alice", "room", "post", a, room, "--text", "after bob")
        later = self.gw.ok("GET", "/v1/owner/rooms/%s/messages" % room, None, owner_b)
        self.assertEqual([1, 2, 3], [m["seq"] for m in later["messages"]])
        self.assertNotIn(4, [m["seq"] for m in later["messages"]])


if __name__ == "__main__":
    unittest.main()
