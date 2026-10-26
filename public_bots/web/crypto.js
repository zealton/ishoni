/* Ishoni web: the browser side of the end-to-end envelopes (bot_e2e.py) and of the password vault
 * (bot_web_client.py). Byte for byte the same formats; checked against each other in tests/test_web_crypto.py.
 *
 * X25519 (RFC 7748) and ChaCha20-Poly1305 (RFC 8439) are written out here because WebCrypto has neither in
 * every browser; SHA-256, HMAC, HKDF and PBKDF2 come from WebCrypto. The code runs only in the colleague's own
 * browser, where the keys already are. Plain script, no dependencies; usable from Node for the interop test. */
(function (root) {
  "use strict";
  const subtle = (root.crypto || require("crypto").webcrypto).subtle;
  const randomBytes = (n) => (root.crypto || require("crypto").webcrypto).getRandomValues(new Uint8Array(n));
  const enc = new TextEncoder(), dec = new TextDecoder("utf-8", { fatal: true });

  class E2EError extends Error {}

  // ---------------------------------------------------------------- encoding
  function b64e(bytes) {
    let s = "";
    for (const b of bytes) s += String.fromCharCode(b);
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function b64d(text, size) {
    if (typeof text !== "string" || text.length > (1 << 22) || !/^[A-Za-z0-9_-]*$/.test(text)) throw new E2EError("not base64url");
    let bin;
    try { bin = atob(text.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - text.length % 4) % 4)); }
    catch (e) { throw new E2EError("not base64url"); }
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    if (b64e(out) !== text || (size !== undefined && out.length !== size)) throw new E2EError("bad length or encoding");
    return out;
  }
  function canonicalValue(v) {
    if (v === null || typeof v !== "object") return JSON.stringify(v);
    if (Array.isArray(v)) return "[" + v.map(canonicalValue).join(",") + "]";
    const keys = Object.keys(v).sort();
    return "{" + keys.map((k) => JSON.stringify(k) + ":" + canonicalValue(v[k])).join(",") + "}";
  }
  // Python's json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False) — the same bytes.
  function canonical(obj) { return enc.encode(canonicalValue(obj)); }
  function concat(...parts) {
    const n = parts.reduce((a, p) => a + p.length, 0), out = new Uint8Array(n);
    let o = 0;
    for (const p of parts) { out.set(p, o); o += p.length; }
    return out;
  }
  function equal(a, b) {
    if (a.length !== b.length) return false;
    let d = 0;
    for (let i = 0; i < a.length; i++) d |= a[i] ^ b[i];
    return d === 0;
  }
  function hex(bytes) { return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join(""); }

  // ---------------------------------------------------------------- hashes (WebCrypto)
  async function sha256(bytes) { return new Uint8Array(await subtle.digest("SHA-256", bytes)); }
  async function hmac(key, data) {
    const k = await subtle.importKey("raw", key.length ? key : new Uint8Array(1).subarray(0, 0), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
    return new Uint8Array(await subtle.sign("HMAC", k, data));
  }
  async function hmacKey(key, data) {
    // WebCrypto refuses an empty HMAC key; RFC 2104 pads it to a block of zeros, so an empty key is 64 zero bytes.
    return hmac(key.length ? key : new Uint8Array(64), data);
  }
  async function hkdf(ikm, salt, info, length = 32) {
    const prk = await hmacKey(salt, ikm);
    let out = new Uint8Array(0), block = new Uint8Array(0), i = 1;
    while (out.length < length) {
      block = await hmacKey(prk, concat(block, info, new Uint8Array([i])));
      out = concat(out, block);
      i += 1;
    }
    return out.slice(0, length);
  }
  async function pbkdf2(password, salt, iterations, length) {
    const k = await subtle.importKey("raw", password, "PBKDF2", false, ["deriveBits"]);
    return new Uint8Array(await subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt, iterations }, k, length * 8));
  }

  // ---------------------------------------------------------------- X25519 (RFC 7748), BigInt
  const P = (1n << 255n) - 19n, A24 = 121665n;
  const mod = (a) => ((a % P) + P) % P;
  function fromLE(bytes) { let x = 0n; for (let i = bytes.length - 1; i >= 0; i--) x = (x << 8n) | BigInt(bytes[i]); return x; }
  function toLE(x, n) { const out = new Uint8Array(n); for (let i = 0; i < n; i++) { out[i] = Number(x & 255n); x >>= 8n; } return out; }
  function modpow(b, e) { let r = 1n; b = mod(b); while (e > 0n) { if (e & 1n) r = r * b % P; b = b * b % P; e >>= 1n; } return r; }
  function x25519(scalar, u) {
    if (scalar.length !== 32 || u.length !== 32) throw new E2EError("X25519 keys are 32 bytes");
    const k0 = new Uint8Array(scalar); k0[0] &= 248; k0[31] &= 127; k0[31] |= 64;
    const k = fromLE(k0), x1 = fromLE(u) & ((1n << 255n) - 1n);
    let x2 = 1n, z2 = 0n, x3 = x1, z3 = 1n, swap = 0n;
    for (let t = 254; t >= 0; t--) {
      const bit = (k >> BigInt(t)) & 1n;
      swap ^= bit;
      if (swap) { [x2, x3] = [x3, x2]; [z2, z3] = [z3, z2]; }
      swap = bit;
      const a = x2 + z2, b = x2 - z2, aa = mod(a * a), bb = mod(b * b), e = aa - bb;
      const c = x3 + z3, d = x3 - z3, da = mod(d * a), cb = mod(c * b);
      x3 = mod((da + cb) * (da + cb));
      z3 = mod(x1 * mod((da - cb) * (da - cb)));
      x2 = mod(aa * bb);
      z2 = mod(e * (aa + A24 * e));
    }
    if (swap) { x2 = x3; z2 = z3; }
    return toLE(mod(x2 * modpow(z2, P - 2n)), 32);
  }
  const BASE = toLE(9n, 32);
  const publicKey = (secret) => x25519(secret, BASE);
  function dh(secret, pub) {
    const shared = x25519(secret, pub);
    if (!shared.some((b) => b)) throw new E2EError("low-order public key");
    return shared;
  }

  // ---------------------------------------------------------------- ChaCha20-Poly1305 (RFC 8439)
  function rotl(x, n) { return ((x << n) | (x >>> (32 - n))) >>> 0; }
  function quarter(s, a, b, c, d) {
    s[a] = (s[a] + s[b]) >>> 0; s[d] = rotl(s[d] ^ s[a], 16);
    s[c] = (s[c] + s[d]) >>> 0; s[b] = rotl(s[b] ^ s[c], 12);
    s[a] = (s[a] + s[b]) >>> 0; s[d] = rotl(s[d] ^ s[a], 8);
    s[c] = (s[c] + s[d]) >>> 0; s[b] = rotl(s[b] ^ s[c], 7);
  }
  function u32le(bytes, o) { return (bytes[o] | (bytes[o + 1] << 8) | (bytes[o + 2] << 16) | (bytes[o + 3] << 24)) >>> 0; }
  function chachaBlock(key, counter, nonce) {
    const state = new Uint32Array(16);
    state.set([0x61707865, 0x3320646e, 0x79622d32, 0x6b206574]);
    for (let i = 0; i < 8; i++) state[4 + i] = u32le(key, 4 * i);
    state[12] = counter >>> 0;
    for (let i = 0; i < 3; i++) state[13 + i] = u32le(nonce, 4 * i);
    const s = new Uint32Array(state);
    for (let i = 0; i < 10; i++) {
      quarter(s, 0, 4, 8, 12); quarter(s, 1, 5, 9, 13); quarter(s, 2, 6, 10, 14); quarter(s, 3, 7, 11, 15);
      quarter(s, 0, 5, 10, 15); quarter(s, 1, 6, 11, 12); quarter(s, 2, 7, 8, 13); quarter(s, 3, 4, 9, 14);
    }
    const out = new Uint8Array(64);
    for (let i = 0; i < 16; i++) {
      const v = (s[i] + state[i]) >>> 0;
      out[4 * i] = v & 255; out[4 * i + 1] = (v >>> 8) & 255; out[4 * i + 2] = (v >>> 16) & 255; out[4 * i + 3] = v >>> 24;
    }
    return out;
  }
  function chacha20(key, counter, nonce, data) {
    const out = new Uint8Array(data.length);
    for (let i = 0; i < data.length; i += 64) {
      const block = chachaBlock(key, counter + (i >>> 6), nonce);
      for (let j = 0; j < 64 && i + j < data.length; j++) out[i + j] = data[i + j] ^ block[j];
    }
    return out;
  }
  function poly1305(key, msg) {
    const r = fromLE(key.subarray(0, 16)) & 0x0ffffffc0ffffffc0ffffffc0fffffffn, s = fromLE(key.subarray(16, 32));
    const p = (1n << 130n) - 5n;
    let acc = 0n;
    for (let i = 0; i < msg.length; i += 16) {
      const chunk = msg.subarray(i, Math.min(i + 16, msg.length));
      acc = (acc + (fromLE(chunk) | (1n << BigInt(8 * chunk.length)))) * r % p;
    }
    return toLE((acc + s) & ((1n << 128n) - 1n), 16);
  }
  const pad16 = (n) => new Uint8Array((16 - n % 16) % 16);
  function tag(key, nonce, aad, ct) {
    const otk = chachaBlock(key, 0, nonce).subarray(0, 32);
    const lens = concat(toLE(BigInt(aad.length), 8), toLE(BigInt(ct.length), 8));
    return poly1305(otk, concat(aad, pad16(aad.length), ct, pad16(ct.length), lens));
  }
  function aeadEncrypt(key, nonce, plaintext, aad = new Uint8Array(0)) {
    const ct = chacha20(key, 1, nonce, plaintext);
    return concat(ct, tag(key, nonce, aad, ct));
  }
  function aeadDecrypt(key, nonce, data, aad = new Uint8Array(0)) {
    if (data.length < 16) throw new E2EError("ciphertext too short");
    const ct = data.subarray(0, data.length - 16), t = data.subarray(data.length - 16);
    if (!equal(tag(key, nonce, aad, ct), t)) throw new E2EError("authentication failed");
    return chacha20(key, 1, nonce, ct);
  }

  // ---------------------------------------------------------------- keys and envelopes
  const ALG = "x25519-chacha20poly1305", VERSION = 1, KID_HEX = 32;
  const INFO_WRAP = enc.encode("ishoni-e2e-v1 wrap");
  async function kid(pub) {
    const raw = typeof pub === "string" ? b64d(pub, 32) : pub;
    return hex(await sha256(concat(enc.encode("ishoni-e2e-v1 key"), raw))).slice(0, KID_HEX);
  }
  function fingerprint(k) { return k.match(/.{4}/g).join(" "); }
  function header(from, kids, ctx) { return canonical({ v: VERSION, alg: ALG, from, to: kids, ctx }); }

  async function seal(plaintext, senderSecret, recipients, ctx) {
    if (!recipients.length || recipients.length > 64) throw new E2EError("1-64 recipients");
    const sk = b64d(senderSecret, 32), spk = publicKey(sk);
    const keys = [], seen = new Set();
    for (const pub of recipients) { const raw = b64d(pub, 32), h = hex(raw); if (!seen.has(h)) { seen.add(h); keys.push(raw); } }
    const kids = [];
    for (const r of keys) kids.push(await kid(r));
    const from = await kid(spk), aad = header(from, kids, ctx);
    const cek = randomBytes(32), nonce = randomBytes(12), to = [];
    for (let i = 0; i < keys.length; i++) {
      const rpk = keys[i], esk = randomBytes(32), epk = publicKey(esk);
      const kek = await hkdf(concat(dh(esk, rpk), dh(sk, rpk)), concat(epk, rpk, spk), INFO_WRAP);
      to.push({ kid: kids[i], epk: b64e(epk), k: b64e(aeadEncrypt(kek, new Uint8Array(12), cek, aad)) });
    }
    return { v: VERSION, alg: ALG, from, n: b64e(nonce), ct: b64e(aeadEncrypt(cek, nonce, plaintext, aad)), to };
  }

  function recipientsOf(env) { return env && Array.isArray(env.to) ? env.to.map((w) => w.kid) : []; }

  async function openSealed(env, mySecret, senderPublic, ctx) {
    if (!env || env.v !== VERSION || env.alg !== ALG || !Array.isArray(env.to)) throw new E2EError("not a sealed body");
    const sk = b64d(mySecret, 32), mpk = publicKey(sk), spk = b64d(senderPublic, 32);
    if (env.from !== await kid(spk)) throw new E2EError("sealed by another key than the expected sender's");
    const myKid = await kid(mpk), w = env.to.find((x) => x && x.kid === myKid);
    if (!w) throw new E2EError("not sealed for this key");
    const aad = header(env.from, env.to.map((x) => x.kid), ctx);
    const epk = b64d(w.epk, 32);
    const kek = await hkdf(concat(dh(sk, epk), dh(sk, spk)), concat(epk, mpk, spk), INFO_WRAP);
    const cek = aeadDecrypt(kek, new Uint8Array(12), b64d(w.k, 48), aad);
    return aeadDecrypt(cek, b64d(env.n, 12), b64d(env.ct), aad);
  }

  // ---------------------------------------------------------------- the web vault (bot_web_client.py)
  const KDF_ITERATIONS = 600000, VAULT_ALG = "chacha20poly1305";
  async function derive(password, email) {
    const pw = enc.encode(password.normalize("NFKC"));
    const salt = await sha256(enc.encode("ishoni-web/1|" + email));
    const dk = await pbkdf2(pw, salt, KDF_ITERATIONS, 64);
    return { vaultKey: dk.slice(0, 32), loginKey: dk.slice(32) };
  }
  const vaultAad = (email) => canonical({ email, kind: "ishoni-web-vault", v: 1 });
  function sealVault(obj, vaultKey, email) {
    const nonce = randomBytes(12);
    return { v: 1, alg: VAULT_ALG, n: b64e(nonce), ct: b64e(aeadEncrypt(vaultKey, nonce, canonical(obj), vaultAad(email))) };
  }
  function openVault(env, vaultKey, email) {
    if (!env || env.v !== 1 || env.alg !== VAULT_ALG) throw new E2EError("not a vault");
    return JSON.parse(dec.decode(aeadDecrypt(vaultKey, b64d(env.n, 12), b64d(env.ct), vaultAad(email))));
  }

  const api = { E2EError, b64e, b64d, canonical, canonicalValue, hex, sha256, hmac: hmacKey, hkdf, pbkdf2, x25519, publicKey,
    aeadEncrypt, aeadDecrypt, kid, fingerprint, seal, openSealed, recipientsOf, derive, sealVault, openVault,
    utf8: (s) => enc.encode(s), text: (b) => dec.decode(b), randomBytes };
  root.IshoniCrypto = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
