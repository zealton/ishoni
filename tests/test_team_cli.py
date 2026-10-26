"""The short team commands (plugins/ishoni/scripts/ishoni_team.py): `ishoni join`, `ishoni admin ...`,
`ishoni muse ...` and the `room` alias, through the launcher against a real gateway subprocess. `admin deploy` runs
against a fake `railway` command that answers the way the Railway CLI does and points at the local gateway.
"""
import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from support.public_harness import C, FriendsGateway, plain_env

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "plugins" / "ishoni" / "scripts" / "ishoni"
LIMITS = {"read_timeout_seconds": 5, "messages_per_minute_per_actor": 1000, "messages_per_hour_per_actor": 5000,
          "enroll_per_hour_per_ip": 1000, "enroll_per_hour": 1000, "bot_enroll_per_hour": 1000,
          "bot_enroll_per_hour_per_ip": 1000, "room_posts_per_hour_per_member": 1000,
          "web_logins_per_hour_per_ip": 1000, "web_logins_per_hour_per_account": 1000}
COUNTER = itertools.count(1)


class TeamCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="ishoni team ")
        cls.root = Path(cls._tmp.name)
        cls.gw = FriendsGateway(cls.root, limits=LIMITS).start()

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls._tmp.cleanup()

    def setUp(self):
        self.n = next(COUNTER)
        self.home = lambda role: self.root / ("%s-%d" % (role, self.n))

    def ishoni(self, home, *args, expect=0, env=None, stdin=None):
        run_env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", HOME=str(self.root / "fakehome"))
        if env:
            run_env.update(env)
        r = subprocess.run(["sh", str(LAUNCHER), "--home", str(home)] + [str(a) for a in args], capture_output=True,
                           text=True, env=run_env, timeout=180, input=stdin)
        self.assertEqual(expect, r.returncode, "%s\n%s\n%s" % (args, r.stdout, r.stderr))
        return r.stdout, r.stderr

    def ishoni_json(self, home, *args, **kw):
        out, _ = self.ishoni(home, *args, "--json", **kw)
        return json.loads(out)

    def admin(self):
        """The administrator's device, logged in with the gateway's owner credential."""
        home = self.home("admin")
        out, _ = self.ishoni(home, "admin", "login", self.gw.url, "--token-file", str(self.gw.home / "owner.token"))
        self.assertIn("Logged in", out)
        return home

    def test_the_whole_team_flow_in_short_commands(self):
        admin = self.admin()
        out, _ = self.ishoni(admin, "admin", "create", "Project %d" % self.n, "--handle", "lark%d" % self.n)
        room = re.search(r"room_[a-z0-9]{16}", out).group(0)
        self.assertIn("Your own agent @lark%d" % self.n, out)
        team = json.loads((admin / "team.json").read_text())
        self.assertEqual((self.gw.url, "lark%d" % self.n), (team["gateway"], team["handle"]))
        self.assertIn(room, [r["room_id"] for r in team["rooms"]])
        listed, _ = self.ishoni(admin, "admin", "rooms")
        self.assertIn(room, listed)
        # the invite: a code and the two lines a colleague pastes to their agent (and the Muse line)
        out, _ = self.ishoni(admin, "admin", "invite", room, "Alice Liddell", "--muse")
        code = re.search(r"ishoni-invite-[A-Za-z0-9_-]+", out).group(0)
        self.assertIn("install.sh | sh", out)
        self.assertIn("sh ~/ishoni/ishoni join %s %s --name alice-liddell" % (self.gw.url, code), out)
        self.assertIn("--agent muse", out)
        minted = self.ishoni_json(admin, "admin", "invite", room, "Bob")
        self.assertEqual(["curl -fsSL https://raw.githubusercontent.com/zealton/ishoni/main/install.sh | sh",
                          "sh ~/ishoni/ishoni join %s %s --name bob" % (self.gw.url, minted["code"])], minted["lines"])
        self.assertEqual("sh ~/ishoni/ishoni join %s %s --name bob" % (self.gw.url, minted["code"]),
                         minted["tell_the_colleague"][1], "the long command's hint shows the same lines")
        # the colleague: one command, a handle derived from the name, the routine printed
        alice = self.home("alice")
        out, _ = self.ishoni(alice, "join", self.gw.url, code, "--name", "Alice Liddell", "--agent", "grok")
        self.assertIn("Joined %s as @alice-liddell" % self.gw.url, out)
        self.assertIn("room sync @alice-liddell %s --mark-read" % room, out)
        self.assertIn("SKILL.md", out)
        team = json.loads((alice / "team.json").read_text())
        self.assertEqual(("alice-liddell", "grok", room), (team["handle"], team["agent"], team["rooms"][0]["room_id"]))
        self.assertFalse((alice / "admin.token").exists())
        # `room ...` is `public room ...`, in the colleague's own data home
        posted, _ = self.ishoni(alice, "room", "post", "@alice-liddell", room, "--text", "hi from Alice")
        self.assertEqual("posted", json.loads(posted)["state"])
        synced, _ = self.ishoni(admin, "room", "sync", "lark%d" % self.n, room, "--after", "0")
        self.assertEqual(["hi from Alice"], [m["text"] for m in json.loads(synced)["messages"]])
        stored = self.gw.sql("SELECT content FROM room_messages WHERE room_id=?", (room,))
        self.assertNotIn("hi from Alice", json.dumps(stored), "the gateway holds ciphertext")
        members, _ = self.ishoni(admin, "admin", "members", room)
        self.assertIn("@alice-liddell", members)
        invites, _ = self.ishoni(admin, "admin", "invites", room)
        self.assertIn("used", invites)
        self.assertIn("-> @alice-liddell", invites)
        # a second colleague with the same name gets the next free handle
        bob1, bob2 = self.home("bob1"), self.home("bob2")
        self.ishoni(bob1, "join", self.gw.url, minted["code"], "--name", "Robert")
        minted2 = self.ishoni_json(admin, "admin", "invite", room, "Bob")
        out, _ = self.ishoni(bob2, "join", self.gw.url, minted2["code"], "--name", "Robert")
        self.assertIn("as @robert-2", out)
        # a wrong code, a spent code
        _, err = self.ishoni(self.home("x"), "join", self.gw.url, "not-a-code", expect=2)
        self.assertIn("looks wrong", err)
        _, err = self.ishoni(self.home("x"), "join", self.gw.url, code, "--name", "Eve", expect=2)
        self.assertIn("spent invite code", err)
        # revoke and remove
        minted3 = self.ishoni_json(admin, "admin", "invite", room, "Carol")
        out, _ = self.ishoni(admin, "admin", "revoke", room, minted3["invite_id"])
        self.assertIn("Revoked", out)
        out, _ = self.ishoni(admin, "admin", "remove", room, team["bot_id"])
        self.assertIn("Removed", out)
        members, _ = self.ishoni(admin, "admin", "members", room)
        self.assertNotIn("@alice-liddell", members)

    def test_muse_up_keeps_the_bridge_running_from_a_bridge_only_home(self):
        admin = self.admin()
        out, _ = self.ishoni(admin, "admin", "create", "Muse %d" % self.n, "--handle", "adm%d" % self.n)
        room = re.search(r"room_[a-z0-9]{16}", out).group(0)
        minted = self.ishoni_json(admin, "admin", "invite", room, "Bob")
        bob = self.home("bob")
        port = free_port()
        # join --agent muse: the bridge comes up with the host we provide (no tunnel in a test)
        out, _ = self.ishoni(bob, "join", self.gw.url, minted["code"], "--name", "Bob", "--agent", "muse",
                             "--tunnel", "none", "--public-host", "bridge.example.com", "--port", str(port))
        try:
            self.assertIn("Muse bridge is up: https://bridge.example.com -> 127.0.0.1:%d" % port, out)
            self.assertIn("api-key://ishoni?spec=", out)
            self.assertIn("custom.ishoni", out)
            self.assertIn(room, out, "the note names the room")
            self.assertIn("@adm%d 是" % self.n, out, "the note names the other members")
            key = (bob / "muse" / "bridge.token").read_text().strip()
            self.assertIn(key, out)
            self.assertEqual(0o600, (bob / "muse" / "bridge.token").stat().st_mode & 0o777)
            # the bridge home holds the binding and the key, not the owner credential
            bridge_creds = json.loads((bob / "muse" / "home" / "public" / "credentials.json").read_text())
            entry = bridge_creds["gateways"][self.gw.url]
            self.assertNotIn("owner", entry)
            self.assertEqual(["native"], [w["adapter"] for w in entry["workers"].values()])
            self.assertTrue(all(entry["bot_keys"].values()))
            # the bridge answers REST with the bridge key only, and opens sealed bodies
            self.ishoni(admin, "room", "post", "adm%d" % self.n, room, "--text", "hello Muse")
            status, listed = rest(port, "/v1/tools", token=key)
            self.assertEqual(200, status)
            self.assertIn("ishoni_room_sync", [t["name"] for t in listed["tools"]])
            self.assertEqual(401, rest(port, "/v1/tools", token="wrong-" + key)[0])
            status, synced = rest(port, "/v1/tools/ishoni_room_sync", {"room": room, "after": 0}, token=key)
            self.assertEqual(["hello Muse"], [m["text"] for m in synced["messages"]])
            status, posted = rest(port, "/v1/tools/ishoni_room_post", {"room": room, "text": "Muse here"}, token=key)
            self.assertEqual((200, "posted"), (status, posted["state"]))
            seen, _ = self.ishoni(admin, "room", "sync", "adm%d" % self.n, room, "--after", "0")
            self.assertIn("Muse here", [m["text"] for m in json.loads(seen)["messages"]])
            st = json.loads(self.ishoni(bob, "muse", "status")[0])
            self.assertEqual((True, True, "bridge.example.com"), (st["running"], st["bridge_ready"], st["public_host"]))
            again, _ = self.ishoni(bob, "muse", "link")
            self.assertIn(key, again)
            # the supervisor restarts a bridge that dies
            bridge_pid = int(re.search(r"\(pid (\d+)\)", (bob / "muse" / "run.log").read_text()).group(1))
            os.kill(bridge_pid, 9)
            for _ in range(60):
                time.sleep(0.5)
                if rest(port, "/v1/tools", token=key)[0] == 200 and \
                        (bob / "muse" / "run.log").read_text().count("bridge listening") >= 2:
                    break
            self.assertEqual(200, rest(port, "/v1/tools", token=key)[0], "restarted after the bridge was killed")
        finally:
            out, _ = self.ishoni(bob, "muse", "down")
        self.assertIn("stopped", out)
        self.ishoni(bob, "muse", "status", expect=1)
        with self.assertRaises((urllib.error.URLError, ConnectionError, OSError)):
            urllib.request.urlopen("http://127.0.0.1:%d/v1/tools" % port, timeout=2)
        # a cloudflared quick tunnel: the hostname is read from the tunnel's log, and the key stays the same
        fake = self.root / ("cloudflared-%d" % self.n)
        fake.write_text("#!/bin/sh\necho 'INF |  https://brave-words-%d.trycloudflare.com  |' >&2\n"
                        "trap 'exit 0' TERM\nwhile true; do sleep 1; done\n" % self.n)
        fake.chmod(0o755)
        out, _ = self.ishoni(bob, "muse", "up", "--tunnel", "cloudflare", env={"ISHONI_CLOUDFLARED": str(fake)})
        try:
            self.assertIn("https://brave-words-%d.trycloudflare.com -> 127.0.0.1:%d" % (self.n, port), out)
            self.assertIn("new hostname whenever it restarts", out)
            self.assertEqual(key, (bob / "muse" / "bridge.token").read_text().strip())
            self.assertEqual(200, rest(port, "/v1/tools", token=key)[0])
        finally:
            self.ishoni(bob, "muse", "down")
        _, err = self.ishoni(self.home("nobody"), "muse", "up", expect=2)
        self.assertIn("join", err)

    def test_admin_deploy_drives_the_railway_cli_and_ends_logged_in(self):
        """A fake `railway` answers like the real CLI: a domain that is this test's gateway, `ssh -- cat` of the
        owner credential, `ssh -- rm`, and `ssh -- python3 ... web account add` run against the gateway's home."""
        bin_dir = self.root / ("bin-%d" % self.n)
        bin_dir.mkdir()
        log = self.root / ("railway-%d.log" % self.n)
        token_copy = self.root / ("owner-copy-%d.token" % self.n)
        token_copy.write_text((self.gw.home / "owner.token").read_text())
        gateway_py = REPO / "public_bots" / "bot_gateway.py"
        (bin_dir / "railway").write_text('''#!/bin/sh
echo "railway $*" >> "%(log)s"
case "$1" in
  whoami) echo "you@example.com" ;;
  status) echo "Project: ishoni-gateway\\nService: gateway" ;;
  domain) echo "%(url)s" ;;
  volume) echo "/data" ;;
  up) echo "deploying" ;;
  ssh)
    shift; shift; shift; shift
    case "$1" in
      cat) cat "%(token)s" ;;
      rm) rm -f "%(token)s" ;;
      python3) shift; shift; shift; shift; exec %(py)s "%(gateway_py)s" --gateway-home "%(gw_home)s" "$@" ;;
    esac ;;
esac
exit 0
''' % {"log": log, "url": self.gw.url, "token": token_copy, "py": sys.executable, "gateway_py": gateway_py,
       "gw_home": self.gw.home})
        (bin_dir / "railway").chmod(0o755)
        pw = self.root / ("pw-%d.txt" % self.n)
        pw.write_text("correct horse battery staple\n")
        pw.chmod(0o600)
        admin = self.home("deployer")
        email = "admin%d@example.com" % self.n
        out, err = self.ishoni(admin, "admin", "deploy", "--email", email, "--password-file", str(pw),
                               env={"PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "")})
        self.assertIn("logged in as the administrator", out)
        self.assertIn("web page ready", out)
        calls = log.read_text()
        self.assertIn("railway variable set ISHONI_PUBLIC_URL=%s" % self.gw.url, calls)
        self.assertIn("railway up --service gateway --detach", calls)
        self.assertIn("railway ssh --service gateway -- rm /data/gw/owner.token", calls)
        self.assertFalse(token_copy.exists(), "the credential was deleted from the volume")
        self.assertEqual((self.gw.home / "owner.token").read_text().strip(), (admin / "admin.token").read_text().strip())
        self.assertEqual(0o600, (admin / "admin.token").stat().st_mode & 0o777)
        team = json.loads((admin / "team.json").read_text())
        self.assertEqual((self.gw.url, email), (team["gateway"], team["web_email"]))
        # logged in: the next commands work, and the web vault is refreshed by `create` with the same password
        out, _ = self.ishoni(admin, "admin", "create", "Deployed %d" % self.n, "--handle", "dep%d" % self.n,
                             "--password-file", str(pw))
        self.assertIn("Web vault refreshed", out)
        rows = self.gw.sql("SELECT role, state FROM web_accounts WHERE email=?", (email,))
        self.assertEqual([("admin", "active")], [(r["role"], r["state"]) for r in rows])
        vault = self.gw.sql("SELECT version FROM web_vaults")
        self.assertTrue(any(r["version"] >= 2 for r in vault), "a second vault version after create")
        # a colleague's web account from the administrator's computer, with the admin's web password
        out, _ = self.ishoni(admin, "admin", "account", "colleague%d@example.com" % self.n, "--password-file", str(pw))
        self.assertIn("ishoni-setup-", out)
        self.assertIn("public web enroll --email colleague%d@example.com" % self.n, out)


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def rest(port, path, body=None, token=""):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                 data=None if body is None else json.dumps(body).encode("utf-8"),
                                 method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "null")
    except (urllib.error.URLError, ConnectionError, OSError):
        return 0, None


if __name__ == "__main__":
    unittest.main()
