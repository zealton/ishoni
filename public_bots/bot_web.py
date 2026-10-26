"""The team web page of an Ishoni gateway: accounts, password-sealed vaults and the static client (stdlib only).

The page at `/web/` is a client that runs in the browser with the same standing as a colleague's admin device. The
gateway never holds anything that opens a room: a colleague's owner credential and the end-to-end keys of their bots
travel in a **vault** that the colleague's own device seals with a key derived from their web password
(`ishoni public web enroll`), and that the browser opens after login. The gateway stores the vault as ciphertext,
a hash of a *login key* derived from the same password (so it can tell a right password from a wrong one without
learning it), and sessions. Everything the page then does (read a room, post, invite, approve) is the browser
calling the ordinary owner routes with the owner credential from the vault: the web layer adds no privilege that an
owner credential does not already have.

Key derivation, shared by the CLI (bot_web_client.py) and the browser (web/crypto.js):

    dk = PBKDF2-HMAC-SHA256(NFKC(password), SHA-256("ishoni-web/1|" + lower(email)), 600000 iterations, 64 bytes)
    vault_key = dk[:32]       (opens the vault; never leaves the device or the browser)
    login_key = dk[32:]       (sent at login; the gateway stores SHA-256(salt + login_key))

The vault is `{"v": 1, "alg": "chacha20poly1305", "n": nonce, "ct": ciphertext}` with the associated data
`{"email": ..., "kind": "ishoni-web-vault", "v": 1}` canonical, so a vault cannot be moved to another account.

Accounts are created by an administrator (the first one with the local command `bot_gateway.py web account add`,
the rest from the page or that command). Each starts `invited` with a one-time setup code that the colleague
spends when enrolling. Every table here is additive, like the room tables.
"""
import hashlib
import json
import secrets
from pathlib import Path

import bot_common as C
import bot_e2e as E

G = None                                    # the bot_gateway module, set by bot_gateway itself

WEB_TABLES = (
    "CREATE TABLE IF NOT EXISTS web_accounts (id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, role TEXT NOT NULL, "
    "state TEXT NOT NULL, login_salt TEXT, login_hash TEXT, setup_hash TEXT, setup_expires_ts INTEGER, "
    "kdf TEXT, created_ts INTEGER NOT NULL, enrolled_ts INTEGER, updated_ts INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS web_vaults (account_id TEXT PRIMARY KEY, vault TEXT NOT NULL, version INTEGER NOT "
    "NULL, updated_ts INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS web_sessions (hash TEXT PRIMARY KEY, account_id TEXT NOT NULL, created_ts INTEGER "
    "NOT NULL, expires_ts INTEGER NOT NULL)",
    "CREATE INDEX IF NOT EXISTS web_sessions_account ON web_sessions(account_id)",
)

LIMIT_DEFAULTS = {
    "web_logins_per_hour_per_ip": 30,
    "web_logins_per_hour_per_account": 20,
    "web_session_hours": 24,
    "web_vault_max_bytes": 262144,              # the sealed vault (owner credential, bot keys, pinned peer keys)
    "web_accounts_max": 500,
}
WEB_DEFAULTS = {"enabled": True}

ROLES = ("admin", "member")
STATES = ("invited", "active", "disabled")
SETUP_DAYS = 7
KDF = {"name": "pbkdf2-sha256", "iterations": 600000, "length": 64, "salt": "sha256('ishoni-web/1|' + email)"}
VAULT_ALG = "chacha20poly1305"
VAULT_AAD_KIND = "ishoni-web-vault"
ACCOUNT_ID = r"web_[a-z2-7]{16}"

ROUTES = [
    ("POST", r"/v1/web/login", "web_login", None),
    ("POST", r"/v1/web/enroll", "web_enroll", None),
    ("GET", r"/v1/web/me", "web_me", ("web",)),
    ("POST", r"/v1/web/logout", "web_logout", ("web",)),
    ("PUT", r"/v1/web/vault", "web_vault_put", ("web",)),
    ("POST", r"/v1/web/password", "web_password", ("web",)),
    ("GET", r"/v1/web/accounts", "web_accounts", ("web",)),
    ("POST", r"/v1/web/accounts", "web_account_add", ("web",)),
    ("POST", r"/v1/web/accounts/(?P<aid>%s)" % ACCOUNT_ID, "web_account_action", ("web",)),
]
ANONYMOUS_LARGE = ("web_enroll",)           # anonymous routes that carry a vault

WEB_DIR = Path(__file__).resolve().parent / "web"
STATIC = {"": ("index.html", "text/html; charset=utf-8"), "index.html": ("index.html", "text/html; charset=utf-8"),
          "app.js": ("app.js", "text/javascript; charset=utf-8"),
          "crypto.js": ("crypto.js", "text/javascript; charset=utf-8"),
          "app.css": ("app.css", "text/css; charset=utf-8")}
PAGE_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def ensure_tables(db):
    for statement in WEB_TABLES:
        db.execute(statement)


def body_limit(limits, name):
    """Routes that carry a vault take a larger body than the general limit."""
    if name in ("web_enroll", "web_vault_put", "web_password"):
        return limits["web_vault_max_bytes"] + 4096
    return None


# ---------------------------------------------------------------- static client
def static_file(path):
    """(content type, bytes) of a file of the page, or None. Only the files listed in STATIC exist."""
    rest = path[len("/web"):]
    if rest.startswith("/"):
        rest = rest[1:]
    if rest not in STATIC:
        return None
    name, kind = STATIC[rest]
    target = WEB_DIR / name
    try:
        return kind, target.read_bytes()
    except OSError:
        return None


# ---------------------------------------------------------------- accounts
def email_of(value):
    """The account's email as the colleague types it (lowercase, trimmed): one spelling for enrolment and login,
    since the vault key is derived from it."""
    address, _ = G.email_field(value)
    return address


def account_view(row, full=False):
    v = {"account_id": row["id"], "email": row["email"], "role": row["role"], "state": row["state"],
         "created_at": C.iso(row["created_ts"])}
    if row["enrolled_ts"]:
        v["enrolled_at"] = C.iso(row["enrolled_ts"])
    if full and row["state"] == "invited" and row["setup_expires_ts"]:
        v["setup_expires_at"] = C.iso(row["setup_expires_ts"])
    return v


def new_setup_code():
    return "ishoni-setup-" + secrets.token_urlsafe(24)


def code_hash(code):
    return hashlib.sha256(("setup|" + code).encode("utf-8")).hexdigest()


def login_hash(salt, login_key):
    return hashlib.sha256(bytes.fromhex(salt) + login_key).hexdigest()


def create_account(db, limits, email, role, now):
    """A new invited account -> (row, setup code). Raises Fail on a duplicate or an invalid role."""
    if role not in ROLES:
        raise G.Fail(400, "invalid_role", "role: admin or member")
    if db.execute("SELECT 1 FROM web_accounts WHERE email=?", (email,)).fetchone():
        raise G.Fail(409, "email_taken", "an account with this email exists; reset it instead")
    if db.execute("SELECT COUNT(*) FROM web_accounts").fetchone()[0] >= limits["web_accounts_max"]:
        raise G.Fail(429, "too_many_accounts")
    aid, code = C.new_id("web"), new_setup_code()
    db.execute("INSERT INTO web_accounts(id, email, role, state, setup_hash, setup_expires_ts, created_ts, updated_ts) "
               "VALUES(?,?,?,'invited',?,?,?,?)", (aid, email, role, code_hash(code),
                                                  int(now) + SETUP_DAYS * 86400, int(now), int(now)))
    return db.execute("SELECT * FROM web_accounts WHERE id=?", (aid,)).fetchone(), code


def reset_account(db, row, now):
    """Back to invited with a fresh setup code: the login key, vault and sessions are gone (the colleague enrols
    again from their admin device). -> setup code."""
    code = new_setup_code()
    db.execute("UPDATE web_accounts SET state='invited', login_salt=NULL, login_hash=NULL, setup_hash=?, "
               "setup_expires_ts=?, kdf=NULL, updated_ts=? WHERE id=?",
               (code_hash(code), int(now) + SETUP_DAYS * 86400, int(now), row["id"]))
    db.execute("DELETE FROM web_vaults WHERE account_id=?", (row["id"],))
    db.execute("DELETE FROM web_sessions WHERE account_id=?", (row["id"],))
    return code


def login_key_field(body):
    try:
        return E.b64d(body.get("login_key"), 32)
    except E.E2EError:
        raise G.Fail(400, "invalid_login_key", "login_key: 32 bytes, base64url")


def vault_field(body, limits):
    env = body.get("vault")
    if not isinstance(env, dict) or set(env) != {"v", "alg", "n", "ct"} or env["v"] != 1 or env["alg"] != VAULT_ALG:
        raise G.Fail(400, "invalid_vault", "vault is {v: 1, alg: chacha20poly1305, n, ct}")
    try:
        E.b64d(env["n"], 12)
        ct = E.b64d(env["ct"])
    except E.E2EError as e:
        raise G.Fail(400, "invalid_vault", "vault: %s" % e)
    if len(ct) < 16 or len(ct) > limits["web_vault_max_bytes"]:
        raise G.Fail(400, "invalid_vault", "vault: ciphertext is 16-%d bytes" % limits["web_vault_max_bytes"])
    return json.dumps(env, separators=(",", ":"))


def authenticate(db, token, now):
    """The subject of a web session token, or None (unknown, expired, or the account no longer active)."""
    s = db.execute("SELECT s.*, a.role, a.email, a.state FROM web_sessions s JOIN web_accounts a ON a.id=s.account_id "
                   "WHERE s.hash=?", (C.token_hash(token),)).fetchone()
    if s is None or s["expires_ts"] <= now or s["state"] != "active":
        return None
    return {"kind": "web", "account_id": s["account_id"], "role": s["role"], "email": s["email"],
            "session_hash": s["hash"], "expires_ts": s["expires_ts"]}


def web_sweep(db, now):
    db.execute("DELETE FROM web_sessions WHERE expires_ts<=?", (int(now),))


class WebMixin(object):
    """Web handlers of the gateway's Api; each runs inside the request's transaction."""

    def web_session(self, db, row, now):
        token = C.new_token("web")
        expires = int(now) + self.limits["web_session_hours"] * 3600
        db.execute("INSERT INTO web_sessions(hash, account_id, created_ts, expires_ts) VALUES(?,?,?,?)",
                   (C.token_hash(token), row["id"], int(now), expires))
        return {"ok": True, "token": token, "expires_at": C.iso(expires), "account": account_view(row)}

    def web_throttle(self, db, r, email):
        L = self.limits
        if not G.take(db, "web:ip:%s" % r.client_ip, L["web_logins_per_hour_per_ip"], 3600, r.now) or \
                not G.take(db, "web:acct:%s" % hashlib.sha256(email.encode("utf-8")).hexdigest()[:32],
                           L["web_logins_per_hour_per_account"], 3600, r.now):
            raise G.Fail(429, "rate_limited", "too many attempts; retry later")

    def web_login(self, db, r):
        G.fields(r.body, ("email", "login_key"), ("email", "login_key"))
        email, key = email_of(r.body["email"]), login_key_field(r.body)
        self.web_throttle(db, r, email)
        row = db.execute("SELECT * FROM web_accounts WHERE email=?", (email,)).fetchone()
        # One answer for an unknown address, a wrong password and an account that is not enrolled yet.
        if row is None or row["state"] != "active" or not row["login_hash"] or \
                not secrets.compare_digest(row["login_hash"], login_hash(row["login_salt"], key)):
            if row is not None and row["state"] == "disabled":
                raise G.Fail(403, "account_disabled", "an administrator disabled this account")
            raise G.Fail(401, "invalid_login", "unknown email or wrong password")
        return 200, self.web_session(db, row, r.now)

    def web_enroll(self, db, r):
        """Spend a setup code: set the login key and the first vault, open a session."""
        G.fields(r.body, ("email", "code", "login_key", "vault", "kdf"), ("email", "code", "login_key", "vault"))
        email, key = email_of(r.body["email"]), login_key_field(r.body)
        self.web_throttle(db, r, email)
        vault = vault_field(r.body, self.limits)
        code = r.body["code"]
        row = db.execute("SELECT * FROM web_accounts WHERE email=?", (email,)).fetchone()
        if row is None or row["state"] != "invited" or not isinstance(code, str) or len(code) > 128 or \
                not row["setup_hash"] or not secrets.compare_digest(row["setup_hash"], code_hash(code)):
            raise G.Fail(401, "invalid_setup", "unknown email or setup code (an administrator issues one)")
        if row["setup_expires_ts"] <= r.now:
            raise G.Fail(410, "setup_expired", "the setup code expired; ask an administrator to reset the account")
        salt = secrets.token_hex(16)
        kdf = r.body.get("kdf", KDF)
        if not isinstance(kdf, dict) or len(json.dumps(kdf)) > 512:
            raise G.Fail(400, "invalid_kdf")
        db.execute("UPDATE web_accounts SET state='active', login_salt=?, login_hash=?, setup_hash=NULL, "
                   "setup_expires_ts=NULL, kdf=?, enrolled_ts=?, updated_ts=? WHERE id=?",
                   (salt, login_hash(salt, key), json.dumps(kdf, sort_keys=True), int(r.now), int(r.now), row["id"]))
        db.execute("INSERT OR REPLACE INTO web_vaults(account_id, vault, version, updated_ts) VALUES(?,?,1,?)",
                   (row["id"], vault, int(r.now)))
        row = db.execute("SELECT * FROM web_accounts WHERE id=?", (row["id"],)).fetchone()
        return 201, self.web_session(db, row, r.now)

    def web_account(self, db, r):
        return db.execute("SELECT * FROM web_accounts WHERE id=?", (r.subject["account_id"],)).fetchone()

    def web_me(self, db, r):
        row = self.web_account(db, r)
        v = db.execute("SELECT * FROM web_vaults WHERE account_id=?", (row["id"],)).fetchone()
        out = {"ok": True, "account": account_view(row), "session_expires_at": C.iso(r.subject["expires_ts"]),
               "gateway": self.cfg["public_base_url"], "kdf": json.loads(row["kdf"]) if row["kdf"] else KDF}
        if v is not None:
            out["vault"] = dict(json.loads(v["vault"]), version=v["version"], updated_at=C.iso(v["updated_ts"]))
        return 200, out

    def web_logout(self, db, r):
        db.execute("DELETE FROM web_sessions WHERE hash=?", (r.subject["session_hash"],))
        return 200, {"ok": True}

    def web_vault_put(self, db, r):
        """A fresh vault under the same password (a new bot, a rotated key): `ishoni public web sync`."""
        G.fields(r.body, ("vault",), ("vault",))
        vault = vault_field(r.body, self.limits)
        row = self.web_account(db, r)
        prior = db.execute("SELECT version FROM web_vaults WHERE account_id=?", (row["id"],)).fetchone()
        version = (prior["version"] if prior else 0) + 1
        db.execute("INSERT OR REPLACE INTO web_vaults(account_id, vault, version, updated_ts) VALUES(?,?,?,?)",
                   (row["id"], vault, version, int(r.now)))
        return 200, {"ok": True, "version": version}

    def web_password(self, db, r):
        """A new password: the new login key and the vault sealed again under the new vault key arrive together;
        every other session ends."""
        G.fields(r.body, ("login_key", "vault"), ("login_key", "vault"))
        key, vault = login_key_field(r.body), vault_field(r.body, self.limits)
        row = self.web_account(db, r)
        salt = secrets.token_hex(16)
        db.execute("UPDATE web_accounts SET login_salt=?, login_hash=?, updated_ts=? WHERE id=?",
                   (salt, login_hash(salt, key), int(r.now), row["id"]))
        prior = db.execute("SELECT version FROM web_vaults WHERE account_id=?", (row["id"],)).fetchone()
        version = (prior["version"] if prior else 0) + 1
        db.execute("INSERT OR REPLACE INTO web_vaults(account_id, vault, version, updated_ts) VALUES(?,?,?,?)",
                   (row["id"], vault, version, int(r.now)))
        db.execute("DELETE FROM web_sessions WHERE account_id=? AND hash!=?", (row["id"], r.subject["session_hash"]))
        return 200, {"ok": True, "version": version}

    # ---- administrators
    def web_admin(self, r):
        if r.subject["role"] != "admin":
            raise G.Fail(403, "admin_only", "only an administrator manages accounts")

    def web_accounts(self, db, r):
        self.web_admin(r)
        rows = db.execute("SELECT * FROM web_accounts ORDER BY created_ts, email").fetchall()
        return 200, {"ok": True, "accounts": [account_view(x, full=True) for x in rows]}

    def web_account_add(self, db, r):
        self.web_admin(r)
        G.fields(r.body, ("email", "role"), ("email",))
        row, code = create_account(db, self.limits, email_of(r.body["email"]), r.body.get("role", "member"), r.now)
        return 201, {"ok": True, "account": account_view(row, full=True), "setup_code": code,
                     "next": "hand the code to the colleague privately; they run: ishoni public web enroll "
                             "GATEWAY --email EMAIL --code CODE"}

    def web_account_action(self, db, r):
        """disable, enable, reset (a new setup code; the vault and sessions are dropped), role."""
        self.web_admin(r)
        G.fields(r.body, ("action", "role"), ("action",))
        action = r.body["action"]
        row = db.execute("SELECT * FROM web_accounts WHERE id=?", (r.params["aid"],)).fetchone()
        if row is None:
            raise G.Fail(404, "not_found")
        out = {"ok": True}
        admins = db.execute("SELECT COUNT(*) FROM web_accounts WHERE role='admin' AND state='active'").fetchone()[0]
        last_admin = row["role"] == "admin" and row["state"] == "active" and admins <= 1
        if action == "disable":
            if last_admin:
                raise G.Fail(409, "last_admin", "this is the only active administrator")
            db.execute("UPDATE web_accounts SET state='disabled', updated_ts=? WHERE id=?", (int(r.now), row["id"]))
            db.execute("DELETE FROM web_sessions WHERE account_id=?", (row["id"],))
        elif action == "enable":
            if row["state"] != "disabled":
                raise G.Fail(409, "not_disabled")
            state = "active" if row["login_hash"] else "invited"
            db.execute("UPDATE web_accounts SET state=?, updated_ts=? WHERE id=?", (state, int(r.now), row["id"]))
        elif action == "reset":
            if last_admin and row["id"] != r.subject["account_id"]:
                raise G.Fail(409, "last_admin", "this is the only active administrator")
            out["setup_code"] = reset_account(db, row, r.now)
        elif action == "role":
            role = r.body.get("role")
            if role not in ROLES:
                raise G.Fail(400, "invalid_role", "role: admin or member")
            if role != "admin" and last_admin:
                raise G.Fail(409, "last_admin", "this is the only active administrator")
            db.execute("UPDATE web_accounts SET role=?, updated_ts=? WHERE id=?", (role, int(r.now), row["id"]))
        else:
            raise G.Fail(400, "invalid_action", "action: disable, enable, reset or role")
        row = db.execute("SELECT * FROM web_accounts WHERE id=?", (row["id"],)).fetchone()
        out["account"] = account_view(row, full=True)
        return 200, out
