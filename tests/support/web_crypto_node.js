// Interop helper for tests/test_web_crypto.py: reads one JSON job on stdin, answers one JSON on stdout.
const path = require("path");
const K = require(path.resolve(process.argv[2]));
(async () => {
  const chunks = [];
  for await (const c of process.stdin) chunks.push(c);
  const job = JSON.parse(Buffer.concat(chunks).toString("utf-8"));
  const out = {};
  // 1. open what Python sealed, seal an answer for Python's key
  const plain = await K.openSealed(job.sealed, job.my_secret, job.sender_public, job.ctx);
  out.opened = K.text(plain);
  out.reply = await K.seal(K.utf8(job.reply_text), job.my_secret, [job.sender_public, job.extra_public], job.ctx);
  // 2. the vault: derive the same keys from the same password, open Python's vault, seal one back
  const { vaultKey, loginKey } = await K.derive(job.password, job.email);
  out.login_key = K.b64e(loginKey);
  out.vault = K.openVault(job.vault, vaultKey, job.email);
  out.vault_back = K.sealVault(out.vault, vaultKey, job.email);
  // 3. primitives against Python
  out.kid = await K.kid(job.sender_public);
  out.public = K.b64e(K.publicKey(K.b64d(job.my_secret, 32)));
  out.canonical = K.text(K.canonical(job.canonical_in));
  out.hkdf = K.b64e(await K.hkdf(K.utf8("ikm"), new Uint8Array(0), K.utf8("info"), 42));
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
