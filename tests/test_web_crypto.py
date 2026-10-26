"""The browser's crypto.js opens what bot_e2e.py seals and the other way round (needs `node`; skipped without)."""
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "public_bots"))

import bot_e2e as E  # noqa: E402
import bot_web_client as WC  # noqa: E402

NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node is not installed")
class WebCryptoCase(unittest.TestCase):
    def test_js_and_python_agree(self):
        py_secret, py_public = E.generate()
        js_secret, js_public = E.generate()
        _, extra_public = E.generate()
        ctx = "ishoni-room/1"
        text = "héllo 团队 — 🚀 " + "x" * 3000
        sealed = E.seal(text.encode("utf-8"), py_secret, [js_public, py_public], ctx, max_recipients=64)
        password, email = "Corr€ct Hörse ｂattery", "alice@example.com"
        vault_key, login_key = WC.derive(password, email)
        vault_obj = {"v": 1, "owner": {"token": "ybo_x"}, "bot_keys": {"bot_a": [{"secret": js_secret, "public": js_public}]},
                     "nested": {"z": [1, 2, {"b": None, "a": True}], "a": "ünïcode \"quoted\" \n"}}
        job = {"sealed": sealed, "my_secret": js_secret, "sender_public": py_public, "extra_public": extra_public,
               "ctx": ctx, "reply_text": "back at you 👋", "password": password, "email": email,
               "vault": WC.seal_vault(vault_obj, vault_key, email), "canonical_in": vault_obj}
        res = subprocess.run([NODE, str(ROOT / "tests/support/web_crypto_node.js"), str(ROOT / "public_bots/web/crypto.js")],
                             input=json.dumps(job).encode("utf-8"), capture_output=True, timeout=120)
        self.assertEqual(0, res.returncode, res.stderr.decode("utf-8", "replace"))
        out = json.loads(res.stdout)
        self.assertEqual(text, out["opened"])
        # the browser's envelope opens here, for both recipients it named
        self.assertEqual(b"back at you \xf0\x9f\x91\x8b", E.open_sealed(out["reply"], py_secret, js_public, ctx))
        self.assertIsNone(E.envelope_error(out["reply"], 1 << 20, max_recipients=64))
        with self.assertRaises(E.E2EError):
            E.open_sealed(out["reply"], py_secret, extra_public, ctx)          # wrong sender
        # the same password gives the same login key, the vault opens both ways
        self.assertEqual(E.b64e(login_key), out["login_key"])
        self.assertEqual(vault_obj, out["vault"])
        self.assertEqual(vault_obj, WC.open_vault(out["vault_back"], vault_key, email))
        with self.assertRaises(E.E2EError):
            WC.open_vault(out["vault_back"], vault_key, "bob@example.com")      # bound to the account
        # primitives
        self.assertEqual(E.kid(py_public), out["kid"])
        self.assertEqual(js_public, out["public"])
        self.assertEqual(E.canonical(vault_obj).decode("utf-8"), out["canonical"])
        self.assertEqual(E.b64e(E.hkdf(b"ikm", b"", b"info", 42)), out["hkdf"])


if __name__ == "__main__":
    unittest.main()
