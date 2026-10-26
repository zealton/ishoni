"""`ishoni public web ...`: the device side of the team web page (bot_web.py on the gateway). Stdlib only.

A colleague's admin device holds the owner credential and the bots' end-to-end keys. `web enroll` seals a copy of
them into a vault under a key derived from the web password and uploads it with the login key (never the password
or the vault key); the browser derives the same keys from the same password and opens the vault locally. The gateway
only ever sees ciphertext, a hash of the login key, and sessions.

Derivation (shared with web/crypto.js, so the two must stay identical):
    dk = PBKDF2-HMAC-SHA256(NFKC(password), SHA-256("ishoni-web/1|" + email), 600000, 64)
    vault_key, login_key = dk[:32], dk[32:]
Vault: ChaCha20-Poly1305 of the canonical JSON vault under vault_key, nonce random, associated data the canonical
JSON {"email": email, "kind": "ishoni-web-vault", "v": 1}.
"""
import getpass
import hashlib
import json
import os
import sys
import unicodedata
from pathlib import Path

import bot_common as C
import bot_e2e as E

KDF_ITERATIONS = 600000
KDF = {"name": "pbkdf2-sha256", "iterations": KDF_ITERATIONS, "length": 64,
       "salt": "sha256('ishoni-web/1|' + email)"}
VAULT_ALG = "chacha20poly1305"
MIN_PASSWORD = 10


def email_norm(value):
    email = (value or "").strip().lower()
    if "@" not in email or len(email) > 254 or any(c.isspace() for c in email):
        raise ValueError("email: one address")
    return email


def derive(password, email):
    """-> (vault_key, login_key), 32 bytes each."""
    pw = unicodedata.normalize("NFKC", password).encode("utf-8")
    salt = hashlib.sha256(("ishoni-web/1|" + email).encode("utf-8")).digest()
    dk = hashlib.pbkdf2_hmac("sha256", pw, salt, KDF_ITERATIONS, 64)
    return dk[:32], dk[32:]


def vault_aad(email):
    return E.canonical({"email": email, "kind": "ishoni-web-vault", "v": 1})


def seal_vault(obj, vault_key, email):
    nonce = os.urandom(12)
    ct = E.aead_encrypt(vault_key, nonce, E.canonical(obj), vault_aad(email))
    return {"v": 1, "alg": VAULT_ALG, "n": E.b64e(nonce), "ct": E.b64e(ct)}


def open_vault(env, vault_key, email):
    if not isinstance(env, dict) or env.get("v") != 1 or env.get("alg") != VAULT_ALG:
        raise E.E2EError("not a vault")
    plain = E.aead_decrypt(vault_key, E.b64d(env["n"], 12), E.b64d(env["ct"]), vault_aad(email))
    return C.strict_loads(plain)


def build_vault(st, base, token):
    """What the browser needs to act as this owner: the owner credential, the bots' keys, the keys pinned for
    others, and the owner's bots by name."""
    entry = st.creds()["gateways"].get(base) or {}
    bots = C.api_ok(base, "GET", "/v1/owner/bots", None, token)[1].get("bots") or []
    return {"v": 1, "gateway": base, "owner": {"token": token, "owner_id": (entry.get("owner") or {}).get("owner_id")},
            "bot_keys": entry.get("bot_keys") or {}, "e2e_peers": entry.get("e2e_peers") or {},
            "bots": [{"bot_id": b.get("bot_id"), "handle": b.get("handle"), "display_name": b.get("display_name")}
                     for b in bots]}


def read_password(a, confirm):
    if getattr(a, "password_file", None):
        pw = C.read_private_text(Path(a.password_file).expanduser(), 4096, "password file").rstrip("\r\n")
    else:
        if not sys.stdin.isatty():
            raise ValueError("no terminal to ask for the password: pass --password-file FILE (mode 0600)")
        pw = getpass.getpass("Web password: ")
        if confirm and getpass.getpass("Again: ") != pw:
            raise ValueError("the two passwords differ")
    if confirm and len(pw) < MIN_PASSWORD:
        raise ValueError("the web password needs at least %d characters" % MIN_PASSWORD)
    return pw


def run(st, a, bc):
    """Entry from bot_client.main: `bc` is the bot_client module."""
    try:
        email = email_norm(a.email)
    except ValueError as e:
        bc.die(str(e))
    if a.web_cmd == "open":
        try:
            vault_key, _ = derive(read_password(a, False), email)
            env = C.read_json_file(Path(a.file).expanduser(), what="vault file")
            bc.out({"ok": True, "vault": open_vault(env.get("vault", env), vault_key, email)})
        except (ValueError, E.E2EError, C.StrictJSONError) as e:
            bc.die("the vault could not be opened: %s" % e)
        return
    base, token = bc.owner_session(st, a)
    try:
        password = read_password(a, confirm=a.web_cmd == "enroll")
    except ValueError as e:
        bc.die(str(e))
    vault_key, login_key = derive(password, email)
    vault = seal_vault(build_vault(st, base, token), vault_key, email)
    if a.web_cmd == "enroll":
        res = bc.call(base, "POST", "/v1/web/enroll", {"email": email, "code": a.code, "login_key": E.b64e(login_key),
                                                      "vault": vault, "kdf": KDF}, None)
        session = res.pop("token", None)
        if session:
            bc.call(base, "POST", "/v1/web/logout", {}, session)
        bc.out(dict(res, web=base + "/web/", note="the vault holds this device's owner credential and bot keys, "
                                                 "sealed under your password; the gateway cannot open it"))
    else:                                                   # sync
        res = bc.call(base, "POST", "/v1/web/login", {"email": email, "login_key": E.b64e(login_key)}, None)
        session = res["token"]
        try:
            put = bc.call(base, "PUT", "/v1/web/vault", {"vault": vault}, session)
        finally:
            bc.call(base, "POST", "/v1/web/logout", {}, session)
        bc.out({"ok": True, "version": put["version"], "web": base + "/web/"})


def add_parser(sp):
    pw = sp.add_parser("web", help="the team web page: seal this device's credentials into your password vault")
    ws = pw.add_subparsers(dest="web_cmd")
    ws.required = True
    p = ws.add_parser("enroll", help="spend the setup code an administrator gave you; choose the web password")
    p.add_argument("--email", required=True)
    p.add_argument("--code", required=True, help="the one-time setup code")
    p = ws.add_parser("sync", help="upload a fresh vault (after a new bot or key) under the same password")
    p.add_argument("--email", required=True)
    p = ws.add_parser("open", help="open a saved vault JSON locally (a check; prints the secrets it holds)")
    p.add_argument("--email", required=True)
    p.add_argument("--file", required=True)
    for sub in ws.choices.values():
        sub.add_argument("--password-file", help="read the password from a private file instead of the terminal")
        sub.add_argument("--gateway")
    return pw
