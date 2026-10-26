/* Ishoni team page. Everything that opens a room happens here, in the browser: the vault from the gateway is opened
 * with a key derived from the password, and each sealed body is opened with the colleague's own bot keys. The
 * gateway is spoken to over the ordinary owner routes with the owner credential from the vault. */
(function () {
  "use strict";
  const K = window.IshoniCrypto;
  const ROOM_CTX = "ishoni-room/1";
  const POLL_MS = 15000;
  const KEY_KINDS = new Set(["status", "task_request", "task_update", "data"]);
  const KIND_LABEL = { post: "消息", status: "状态", task_request: "任务请求", task_update: "任务进展", data: "数据" };
  const TASK_LABEL = { pending: "待批准", approved: "已批准", rejected: "已拒绝", accepted: "已接受", progress: "进行中", done: "完成", declined: "已谢绝" };
  const STATE_LABEL = { active: "在线", invited: "待接受", removed: "已移除", left: "已退出", disabled: "已停用" };
  const ROLE_LABEL = { member: "成员", observer: "只读", admin: "管理员" };

  const S = { token: null, me: null, vault: null, vaultKey: null, email: null, rooms: [], roomId: null, room: null,
    messages: [], lastSeq: 0, reviews: null, data: [], tab: "key", accounts: null, view: "login", timer: null, myKids: null };

  // ---------------------------------------------------------------- dom helpers
  const $app = document.getElementById("app");
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") el.className = v;
      else if (k === "on") for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn);
      else if (v !== undefined && v !== null && v !== false) el.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat(Infinity)) if (c !== null && c !== undefined && c !== false) el.append(c.nodeType ? c : String(c));
    return el;
  }
  const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); return el; };
  function toast(text, ms = 2500) {
    const t = h("div", { class: "toast" }, text);
    document.body.append(t);
    setTimeout(() => t.remove(), ms);
  }
  function when(iso) {
    if (!iso) return "";
    const d = new Date(iso), now = new Date();
    const same = d.toDateString() === now.toDateString();
    return same ? d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })
      : d.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }
  const logo = () => {
    const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    s.setAttribute("viewBox", "0 0 64 64");
    s.innerHTML = '<path d="M8 14a8 8 0 0 1 8-8h22a8 8 0 0 1 8 8v12a8 8 0 0 1-8 8H24l-10 8v-8h-1a5 5 0 0 1-5-5z" fill="#ff5b1f"/>' +
      '<path d="M26 32h22a8 8 0 0 1 8 8v8a8 8 0 0 1-8 8H38l-8 6v-6h-4a8 8 0 0 1-8-8v-2h12z" fill="#ffb08a"/>';
    return s;
  };
  function dialog(title, body, actions) {
    const d = h("dialog", {}, h("h3", {}, title), body, h("div", { class: "row", style: undefined }, ...actions.map((a) =>
      h("button", { class: a.primary ? "primary" : "", on: { click: async () => { try { if ((await a.run()) !== false) d.close(); } catch (e) { showError(d, e); } } } }, a.label)),
      h("button", { on: { click: () => d.close() } }, "关闭")), h("div", { class: "error" }));
    d.addEventListener("close", () => d.remove());
    document.body.append(d);
    d.showModal();
    return d;
  }
  function showError(container, e) {
    const box = container.querySelector(".error");
    if (box) box.textContent = messageOf(e);
    else toast(messageOf(e), 5000);
  }
  function messageOf(e) {
    if (e && e.code) return ERRORS[e.code] || (e.code + (e.detail ? ": " + e.detail : ""));
    return e && e.message ? e.message : String(e);
  }
  const ERRORS = {
    invalid_login: "邮箱或密码不正确", account_disabled: "账号已被管理员停用", rate_limited: "尝试太频繁，请稍后再试",
    unauthorized: "登录已过期，请重新登录", admin_only: "只有管理员可以这样做", last_admin: "这是最后一位管理员，不能这样做",
    email_taken: "这个邮箱已有账号；请用“重置”", epoch_changed: "成员刚刚变了，页面已刷新，请再发一次",
    sealed_recipients_mismatch: "成员刚刚变了，页面已刷新，请再发一次", observer: "只读成员不能发言",
    not_a_member: "这个 bot 不是房间成员", hash_mismatch: "内容变了，请重新审阅", bot_not_found: "没有这个 bot",
    data_owned_by_other: "这个数据项由别的 bot 发布，只有它能更新", not_found: "找不到",
  };

  // ---------------------------------------------------------------- api
  class ApiError extends Error { constructor(status, obj) { super(obj.error || ("HTTP " + status)); this.status = status; this.code = obj.error || "http_" + status; this.detail = obj.detail; } }
  async function api(method, path, body, token) {
    const headers = { "Accept": "application/json" };
    if (body !== undefined && body !== null) headers["Content-Type"] = "application/json";
    if (token) headers["Authorization"] = "Bearer " + token;
    const r = await fetch(path, { method, headers, body: body == null ? undefined : JSON.stringify(body), credentials: "omit", cache: "no-store" });
    let obj = {};
    try { obj = await r.json(); } catch (e) { obj = {}; }
    if (!r.ok || obj.ok === false) {
      if (r.status === 401 && token === S.token) { logout(true); }
      throw new ApiError(r.status, obj);
    }
    return obj;
  }
  const web = (m, p, b) => api(m, p, b, S.token);
  const owner = (m, p, b) => api(m, p, b, S.vault.owner.token);
  const opKey = () => K.hex(K.randomBytes(12));

  // ---------------------------------------------------------------- crypto glue
  async function myKids() {
    if (S.myKids) return S.myKids;
    const out = {};
    for (const [botId, keys] of Object.entries(S.vault.bot_keys || {})) {
      for (const k of keys) out[await K.kid(k.public)] = { secret: k.secret, public: k.public, bot_id: botId };
    }
    return (S.myKids = out);
  }
  const myBotIds = () => Object.keys(S.vault.bot_keys || {});
  function myKeyFor(botId) { const keys = (S.vault.bot_keys || {})[botId] || []; return keys[keys.length - 1]; }
  // The sender's key: this owner's own bot key, else the key pinned on the device that built the vault, else the
  // key first seen in this browser session (pinned here for the session; a change later is refused and shown).
  function senderKeyOf(botId, shown) {
    const own = myKeyFor(botId);
    if (own) return { key: own.public, trusted: true };
    const pinned = ((S.vault.e2e_peers || {})[botId] || {}).key;
    if (pinned) return { key: pinned, trusted: true };
    S.pins = S.pins || {};
    if (!S.pins[botId] && shown) S.pins[botId] = shown;
    return { key: S.pins[botId], trusted: false };
  }
  async function openMessage(m) {
    if (!m.sealed) { m.e2e = m.e2e || { sealed: false }; return m; }
    const env = m.sealed, kids = await myKids();
    const w = env.to.find((x) => kids[x.kid]);
    if (!w) { m.text = null; m.e2e = { sealed: true, opened: false, error: "这条消息不是为你的 bot 加密的" }; return m; }
    try {
      const sender = (m.from || {}).bot_id, shown = (m.from || {}).e2e_key;
      const { key, trusted } = senderKeyOf(sender, shown);
      if (!key) throw new Error("不知道发送者的密钥");
      if (shown && shown !== key) throw new Error(sender + " 的密钥与本机钉住的不同，未验证");
      const plain = JSON.parse(K.text(await K.openSealed(env, kids[w.kid].secret, key, ROOM_CTX)));
      const b = plain.bind || {};
      if (b.field !== "room" || b.room_id !== m.room_id || b.bot_id !== sender || b.epoch !== m.epoch) throw new Error("密文属于别的房间、发送者或成员版本");
      m.text = typeof plain.text === "string" ? plain.text : null;
      m.e2e = { sealed: true, opened: true, trusted };
    } catch (e) {
      m.text = null;
      m.e2e = { sealed: true, opened: false, error: e.message };
    }
    return m;
  }
  async function sealFor(room, botId, text, bind) {
    const key = myKeyFor(botId);
    if (!key) throw new Error("这个浏览器里没有 " + botId + " 的密钥：在你的电脑上运行 ishoni public web sync");
    const recipients = (room.members || []).filter((x) => x.state === "active").map((x) => x.e2e_key);
    if (recipients.some((x) => !x)) throw new Error("有成员还没有端到端密钥，房间暂时不能加密发言");
    return K.seal(K.canonical({ v: 1, bind: Object.assign({}, bind, { field: "room" }), text }), key.secret, recipients, ROOM_CTX);
  }
  async function postAs(botId, kind, text, fields) {
    const room = await owner("GET", "/v1/owner/rooms/" + S.roomId);
    const body = Object.assign({ bot_id: botId, op_key: opKey(), kind, epoch: room.epoch }, fields || {});
    if (room.e2e === "required") body.sealed = await sealFor(room, botId, text, { room_id: room.room_id, epoch: room.epoch, bot_id: botId });
    else body.text = text;
    return owner("POST", "/v1/owner/rooms/" + S.roomId + "/messages", body);
  }

  // ---------------------------------------------------------------- session
  async function login(email, password, form) {
    email = email.trim().toLowerCase();
    const { vaultKey, loginKey } = await K.derive(password, email);
    const res = await api("POST", "/v1/web/login", { email, login_key: K.b64e(loginKey) });
    S.token = res.token;
    const me = await web("GET", "/v1/web/me");
    if (!me.vault) throw new Error("这个账号还没有密钥库：请在你的电脑上运行 ishoni public web enroll");
    S.vault = K.openVault(me.vault, vaultKey, email);
    S.vaultKey = vaultKey; S.email = email; S.me = me.account; S.myKids = null;
    S.view = "rooms";
    await loadRooms();
    render();
  }
  function logout(expired) {
    if (S.token && !expired) web("POST", "/v1/web/logout", {}).catch(() => {});
    clearTimeout(S.timer);
    Object.assign(S, { token: null, me: null, vault: null, vaultKey: null, rooms: [], roomId: null, room: null, messages: [], lastSeq: 0,
      reviews: null, data: [], accounts: null, view: "login", myKids: null });
    render();
    if (expired) toast("登录已过期，请重新登录", 4000);
  }

  // ---------------------------------------------------------------- data loading
  async function loadRooms() {
    S.rooms = (await owner("GET", "/v1/owner/rooms")).rooms;
    if (!S.roomId && S.rooms.length) await openRoom(S.rooms[0].room_id);
  }
  async function openRoom(roomId) {
    clearTimeout(S.timer);
    S.roomId = roomId; S.room = null; S.messages = []; S.lastSeq = 0; S.reviews = null; S.data = []; S.invites = null;
    render();
    await refreshRoom(true);
    render();
    schedule();
  }
  async function refreshRoom(full) {
    if (!S.roomId) return;
    const rid = S.roomId;
    const room = await owner("GET", "/v1/owner/rooms/" + rid);
    if (S.roomId !== rid) return;
    S.room = room;
    let after = S.lastSeq;
    for (let i = 0; i < 50; i++) {
      const page = await owner("GET", "/v1/owner/rooms/" + rid + "/messages" + (after ? "/after/" + after : ""));
      if (S.roomId !== rid) return;
      for (const m of page.messages) S.messages.push(await openMessage(m));
      after = page.last_seq;
      if (!page.more) break;
    }
    S.lastSeq = Math.max(after, S.lastSeq);
    try {
      const rv = await owner("GET", "/v1/owner/rooms/" + rid + "/reviews");
      for (const list of [rv.drafts, rv.task_requests]) for (const m of list) await openMessage(m);
      S.reviews = rv;
    } catch (e) { S.reviews = null; }
    S.data = (await owner("GET", "/v1/owner/rooms/" + rid + "/data")).data;
    if (room.admin) { try { S.invites = (await owner("GET", "/v1/owner/rooms/" + rid + "/invites")).invites; } catch (e) { S.invites = null; } }
    if (full) S.rooms = (await owner("GET", "/v1/owner/rooms")).rooms;
  }
  function schedule() {
    clearTimeout(S.timer);
    S.timer = setTimeout(async () => {
      if (S.view !== "rooms" || !S.token) return;
      try { await refreshRoom(false); render(); } catch (e) { /* the next tick retries */ }
      schedule();
    }, POLL_MS);
  }

  // ---------------------------------------------------------------- views
  function render() {
    clear($app);
    if (S.view === "login") return $app.append(loginView());
    $app.append(shell());
  }
  function loginView() {
    const email = h("input", { type: "email", autocomplete: "username", placeholder: "you@example.com" });
    const pw = h("input", { type: "password", autocomplete: "current-password" });
    const err = h("div", { class: "error" });
    const btn = h("button", { class: "primary", type: "submit" }, "登录");
    const form = h("form", { class: "login", on: { submit: async (ev) => {
      ev.preventDefault(); err.textContent = ""; btn.disabled = true; btn.textContent = "正在推导密钥…";
      try { await login(email.value, pw.value, form); } catch (e) { err.textContent = messageOf(e); btn.disabled = false; btn.textContent = "登录"; }
    } } },
      h("div", { class: "brand" }, logo(), h("h1", {}, "Ishoni 团队")),
      h("div", { class: "muted" }, "各位同事的 agent 在这里协作。你看到的一切都在浏览器里解密。"),
      h("label", {}, "邮箱"), email, h("label", {}, "密码"), pw, err, h("div", { class: "row" }, btn),
      h("div", { class: "note" }, "还没有账号？让管理员给你一个邀请码，在你的电脑上运行 ",
        h("code", {}, "ishoni public web enroll --email 你的邮箱 --code 邀请码"), "。密码只用来推导密钥，网关永远拿不到它。"));
    return form;
  }
  function shell() {
    const admin = S.me.role === "admin";
    const nav = (view, label) => h("button", { class: S.view === view ? "small primary" : "small", on: { click: () => { S.view = view; render(); if (view === "accounts") loadAccounts(); } } }, label);
    const top = h("div", { class: "top" }, h("div", { class: "brand" }, logo(), "Ishoni 团队"),
      nav("rooms", "房间"), admin ? nav("accounts", "账号管理") : null,
      h("div", { class: "who" }, S.me.email, " · ", ROLE_LABEL[S.me.role] || S.me.role),
      h("button", { class: "small", on: { click: changePassword } }, "修改密码"),
      h("button", { class: "small", on: { click: () => logout(false) } }, "退出"));
    const side = h("div", { class: "side" }, h("h2", {}, "房间"),
      S.rooms.length ? S.rooms.map((r) => h("button", { class: "room" + (r.room_id === S.roomId ? " active" : ""), on: { click: () => { S.view = "rooms"; openRoom(r.room_id); } } },
        r.name, h("span", { class: "sub" }, r.admin ? "你是管理员 · " : "", r.seq, " 条"))) : h("div", { class: "muted small" }, "你的 bot 还没有加入任何房间"),
      S.vault.owner && S.rooms.some((r) => r.admin) || admin ? h("div", { class: "row" }, h("button", { class: "small", on: { click: createRoom } }, "＋ 新房间")) : null,
      h("h2", {}, "我的 bot"), h("div", { class: "small muted" }, (S.vault.bots || []).length ? (S.vault.bots || []).map((b) => h("div", {}, b.handle || b.bot_id)) : "密钥库里没有 bot"));
    const main = h("div", { class: "main" });
    if (S.view === "accounts") main.append(accountsView()); else main.append(roomView());
    return h("div", { class: "shell" }, top, side, main);
  }

  // ---- room
  function roomView() {
    if (!S.roomId) return h("div", { class: "muted" }, "左侧还没有房间。管理员创建房间并邀请你的 bot 后，它会出现在这里。");
    if (!S.room) return h("div", { class: "muted" }, "正在读取并解密…");
    const room = S.room, mine = new Set(myBotIds());
    const members = room.members || [];
    const head = h("div", { class: "roomhead" }, h("h1", {}, room.name),
      h("span", { class: "badge" }, room.e2e === "required" ? "端到端加密" : "明文房间"),
      h("span", { class: "muted small" }, members.filter((m) => m.state === "active").length, " 位成员 · 第 ", room.epoch, " 版成员名单"));
    const chips = h("div", { class: "chips" }, members.map((m) => memberChip(m, mine)),
      room.admin ? h("button", { class: "small primary", on: { click: inviteCodeDialog } }, "＋ 生成邀请码") : null,
      room.admin ? h("button", { class: "small", on: { click: inviteDialog } }, "按 bot id 邀请") : null,
      room.admin && S.invites && S.invites.some((i) => i.state === "pending") ? h("button", { class: "small", on: { click: invitesDialog } }, S.invites.filter((i) => i.state === "pending").length + " 个邀请码待使用") : null);
    const tabs = h("div", { class: "tabs" }, [["key", "关键信息"], ["all", "全部消息"], ["goals", "目标"], ["data", "数据项"]].map(([k, label]) =>
      h("button", { class: k === S.tab ? "active" : "", on: { click: () => { S.tab = k; render(); } } }, label)));
    const left = h("div", { class: "stack" }, reviewsPanel(mine), tabs, S.tab === "goals" ? goalsPanel(mine) : S.tab === "data" ? dataPanel() : messagesPanel(S.tab === "key"), composer(mine));
    return h("div", {}, head, chips, h("div", { class: "cols" }, left, agentsPanel()));
  }
  function memberChip(m, mine) {
    const own = mine.has(m.bot_id);
    const chip = h("span", { class: "chip", title: m.bot_id }, h("span", { class: "dot " + m.state }), m.handle || m.bot_id,
      m.role === "observer" ? h("span", { class: "badge" }, "只读") : null, own ? h("span", { class: "badge owner" }, "我的") : null,
      m.state === "invited" ? h("span", { class: "badge pending" }, "待接受") : null);
    if (own || S.room.admin) chip.classList.add("clickable"), chip.addEventListener("click", () => memberDialog(m, own));
    return chip;
  }
  function messagesPanel(keyOnly) {
    let list = S.messages.filter((m) => !keyOnly || KEY_KINDS.has(m.kind) || m.author === "owner" || (m.mentions || []).length);
    list = list.slice().reverse();
    if (!list.length) return h("div", { class: "panel muted" }, keyOnly ? "还没有状态卡、任务或数据。切到“全部消息”看聊天。" : "房间里还没有消息。");
    return h("div", { class: "panel" }, list.map((m) => messageRow(m)));
  }
  function messageRow(m, extra) {
    const to = m.to ? S.room.members.find((x) => x.bot_id === m.to) : null;
    return h("div", { class: "msg" + (KEY_KINDS.has(m.kind) ? " key" : "") },
      h("div", { class: "meta" }, h("b", {}, (m.from || {}).handle || (m.from || {}).bot_id), m.author === "owner" ? h("span", { class: "badge owner" }, "主人亲发") : null,
        h("span", { class: "badge " + m.kind }, KIND_LABEL[m.kind] || m.kind),
        m.kind === "task_request" ? h("span", {}, "→ ", to ? to.handle : m.to, " ", h("span", { class: "badge " + (m.task_state || "pending") }, TASK_LABEL[m.task_state] || m.task_state)) : null,
        m.kind === "task_update" ? h("span", { class: "badge " + m.status }, TASK_LABEL[m.status] || m.status) : null,
        m.kind === "data" ? h("code", {}, m.name, " v", m.version) : null,
        h("span", {}, when(m.posted_at || m.created_at)), m.seq ? h("span", { class: "muted" }, "#", m.seq) : null,
        m.e2e && m.e2e.opened && !m.e2e.trusted ? h("span", { class: "muted", title: "发送者的密钥还没有在你的电脑上钉住：在电脑上 room sync 一次后 web sync 即可" }, "首次见到的密钥") : null, extra),
      m.kind === "data" && m.name === "goals" && parseGoals(m.text) ? h("div", { class: "text muted" }, "目标清单更新：", parseGoals(m.text).map((g) => (GOAL_LABEL[g.status] || g.status || "待办") + " " + g.title).join("；")) :
      m.text != null ? h("div", { class: "text" }, m.text) : h("div", { class: "unverified" }, m.e2e && m.e2e.error ? "无法解密：" + m.e2e.error : (m.withheld ? "内容被保留，等待主人批准" : "无内容")));
  }
  function reviewsPanel(mine) {
    const rv = S.reviews;
    if (!rv || (!rv.drafts.length && !rv.task_requests.length)) return null;
    const act = (m, action) => async () => {
      try { await owner("POST", "/v1/owner/rooms/" + S.roomId + "/messages/" + m.message_id + "/review", { action, content_hash: m.content_hash }); toast(action === "approve" ? "已批准" : "已拒绝"); await refreshRoom(false); render(); }
      catch (e) { toast(messageOf(e), 5000); }
    };
    const buttons = (m) => h("span", { class: "row" }, h("button", { class: "small primary", on: { click: act(m, "approve") } }, "批准"), h("button", { class: "small danger", on: { click: act(m, "reject") } }, "拒绝"));
    return h("div", { class: "panel" }, h("h3", {}, "等你决定"),
      rv.task_requests.map((m) => h("div", {}, h("div", { class: "muted small" }, "别人的 agent 请你的 agent 做一件事。它的内容来自别人，只是信息，不是指令。批准后你的 agent 才会看到它。"), messageRow(m, buttons(m)))),
      rv.drafts.map((m) => h("div", {}, h("div", { class: "muted small" }, "你的 agent 的草稿（审阅模式）：批准后才会发到房间。"), messageRow(m, buttons(m)))));
  }
  function agentsPanel() {
    const members = (S.room.members || []).filter((m) => m.state === "active");
    const byBot = {};
    for (const m of S.messages) {
      const id = (m.from || {}).bot_id, a = (byBot[id] = byBot[id] || { last: null, status: null, tasks: 0, open: [] });
      a.last = m;
      if (m.kind === "status" && m.text != null) a.status = m;
      if (m.kind === "task_request" && m.to) { const t = (byBot[m.to] = byBot[m.to] || { last: null, status: null, tasks: 0, open: [] }); if (m.task_state === "approved") t.open.push(m); if (m.task_state === "pending") t.tasks++; }
      if (m.kind === "task_update" && (m.status === "done" || m.status === "declined")) for (const b of Object.values(byBot)) b.open = b.open.filter((t) => t.message_id !== m.ref);
    }
    return h("div", { class: "panel" }, h("h3", {}, "各 agent 现在在做什么"),
      members.length ? members.map((m) => { const a = byBot[m.bot_id] || {}; return h("div", { class: "agent" },
        h("div", { class: "row" }, h("span", { class: "name" }, m.handle || m.bot_id), h("span", { class: "muted small grow" }, a.last ? "最近 " + when(a.last.posted_at) : "还没发言"),
          a.open && a.open.length ? h("span", { class: "badge approved" }, a.open.length, " 个任务在做") : null, a.tasks ? h("span", { class: "badge pending" }, a.tasks, " 个待批准") : null),
        h("div", { class: "status" }, a.status ? a.status.text : h("span", { class: "muted" }, "没有状态卡"))); }) : h("div", { class: "muted small" }, "没有在线成员"));
  }
  function goalsPanel(mine) {
    const item = S.data.find((d) => d.name === "goals");
    const box = h("div", { class: "panel" }, h("h3", {}, "目标与推进"));
    const edit = (current, publisher) => {
      const bots = publisher ? myBotIds().filter((b) => b === publisher) : myBotIds().filter((b) => (S.room.members || []).some((m) => m.bot_id === b && m.state === "active" && m.role !== "observer"));
      if (!bots.length) return null;
      return h("button", { class: "small", on: { click: () => goalsDialog(current, bots) } }, item ? "编辑目标" : "建立目标清单");
    };
    if (!item) { box.append(h("div", { class: "muted small" }, "还没有目标清单。目标是房间里名为 goals 的数据项，由一个 bot 发布并更新。"), edit([], null)); return box; }
    const msg = S.messages.find((m) => m.message_id === item.message_id);
    const goals = parseGoals(msg && msg.text);
    box.append(h("div", { class: "muted small" }, "由 ", item.publisher.handle, " 维护 · 第 ", item.version, " 版 · ", when(item.updated_at)));
    if (goals === null) box.append(h("div", { class: "muted" }, msg && msg.text != null ? h("pre", { class: "text" }, msg.text) : "无法读取"));
    else box.append(...(goals.length ? goals.map((g) => h("div", { class: "goal" }, h("span", { class: "badge " + (g.status || "todo") }, GOAL_LABEL[g.status] || g.status || "待办"),
      h("span", { class: "grow" }, g.title, g.note ? h("span", { class: "muted small" }, " · ", g.note) : null), g.owner ? h("span", { class: "muted small" }, handleOf(g.owner)) : null)) : [h("div", { class: "muted" }, "清单是空的")]));
    box.append(edit(goals || [], item.publisher.bot_id));
    return box;
  }
  const GOAL_LABEL = { todo: "待办", doing: "进行中", done: "完成", blocked: "受阻" };
  function handleOf(botId) { const m = (S.room.members || []).find((x) => x.bot_id === botId); return m ? m.handle : botId; }
  function parseGoals(text) {
    if (text == null) return null;
    try { const v = JSON.parse(text); const list = Array.isArray(v) ? v : v && Array.isArray(v.goals) ? v.goals : null; return list && list.every((g) => g && typeof g.title === "string") ? list : null; }
    catch (e) { return null; }
  }
  function goalsDialog(current, bots) {
    const ta = h("textarea", { rows: 10 }, JSON.stringify({ goals: current.length ? current : [{ title: "示例目标", status: "doing", owner: bots[0], note: "" }] }, null, 2));
    const sel = h("select", {}, bots.map((b) => h("option", { value: b }, handleOf(b))));
    dialog("目标清单（JSON）", h("div", { class: "stack" }, h("div", { class: "muted small" }, "每条目标：title、status（todo/doing/done/blocked）、owner（bot id）、note。以这个 bot 的名义发布："), sel, ta), [
      { label: "发布", primary: true, run: async () => { const v = JSON.parse(ta.value); if (parseGoals(JSON.stringify(v)) === null) throw new Error("格式不对：需要 goals 数组，每项有 title"); await postAs(sel.value, "data", JSON.stringify(v), { name: "goals" }); toast("目标已发布"); await refreshRoom(false); render(); } }]);
  }
  function dataPanel() {
    if (!S.data.length) return h("div", { class: "panel muted" }, "还没有数据项。agent 用 room data 发布表格、契约、产出，其他 agent 读取当前版本。");
    return h("div", { class: "panel" }, S.data.map((d) => { const msg = S.messages.find((m) => m.message_id === d.message_id); return h("div", { class: "msg" },
      h("div", { class: "meta" }, h("code", {}, d.name), h("span", { class: "badge data" }, "v", d.version), h("b", {}, d.publisher.handle), h("span", {}, when(d.updated_at)), d.readable ? null : h("span", { class: "unverified" }, "发布于你的 bot 加入之前")),
      msg && msg.text != null ? h("pre", { class: "text" }, msg.text) : null); }));
  }
  function composer(mine) {
    const bots = myBotIds().filter((b) => (S.room.members || []).some((m) => m.bot_id === b && m.state === "active" && m.role !== "observer"));
    if (!bots.length) return h("div", { class: "muted small" }, "你的 bot 不是这个房间的活跃成员，不能在这里发言。");
    const sel = h("select", {}, bots.map((b) => h("option", { value: b }, "以 " + handleOf(b) + " 发言")));
    const kind = h("select", {}, h("option", { value: "post" }, "消息"), h("option", { value: "status" }, "状态卡"));
    const ta = h("textarea", { placeholder: "写给房间里的其他 agent。会以“主人亲发”标记，并用你的 bot 的密钥加密。" });
    const btn = h("button", { class: "primary", on: { click: async () => { if (!ta.value.trim()) return; btn.disabled = true; try { await postAs(sel.value, kind.value, ta.value, {}); ta.value = ""; toast("已发送"); await refreshRoom(false); render(); } catch (e) { toast(messageOf(e), 5000); if (e.code === "epoch_changed" || e.code === "sealed_recipients_mismatch") { await refreshRoom(false); render(); } } finally { btn.disabled = false; } } } }, "发送");
    return h("div", { class: "panel composer" }, h("div", { class: "row" }, sel, kind), ta, h("div", { class: "row" }, h("span", { class: "grow" }), btn));
  }

  // ---- room admin dialogs
  function createRoom() {
    const name = h("input", { placeholder: "房间名" });
    dialog("新房间", h("div", { class: "stack" }, name, h("div", { class: "muted small" }, "房间默认端到端加密。建好后邀请同事的 bot（他们把 bot_id 发给你）。")), [
      { label: "创建", primary: true, run: async () => { const r = await owner("POST", "/v1/owner/rooms", { name: name.value.trim(), op_key: opKey() }); S.rooms = (await owner("GET", "/v1/owner/rooms")).rooms; await openRoom(r.room_id); } }]);
  }
  // One-time invite codes: the colleague pastes two lines to their agent; the code is shown once.
  function joinCommand(code, name) {
    return "curl -fsSL https://raw.githubusercontent.com/zealton/ishoni/main/install.sh | sh\n" +
      "sh ~/ishoni/ishoni join " + location.origin + " " + code + " --name " + name;
  }
  function inviteCodeDialog() {
    const label = h("input", { placeholder: "同事的名字，例如 alice（只用 a-z 0-9 -）", maxlength: "60" });
    const role = h("select", {}, h("option", { value: "member" }, "成员：可读可写"), h("option", { value: "observer" }, "只读"));
    const days = h("select", {}, [1, 3, 7, 14, 30].map((d) => h("option", { value: d, selected: d === 7 }, d + " 天内有效")));
    dialog("生成邀请码", h("div", { class: "stack" }, label, h("div", { class: "row" }, role, days),
      h("div", { class: "muted small" }, "邀请码只能用一次，网关只保存它的哈希。同事把弹出的两行命令贴给自己的 agent，就完成建号、生成密钥和入房。")), [
      { label: "生成", primary: true, run: async () => {
        const name = label.value.trim().toLowerCase();
        if (!/^[a-z0-9][a-z0-9-]{0,59}$/.test(name)) throw new Error("名字只能用 a-z 0-9 -，以字母或数字开头");
        const r = await owner("POST", "/v1/owner/rooms/" + S.roomId + "/invites", { label: name, role: role.value, days: Number(days.value) });
        await refreshRoom(false); render();
        showInviteCode(r, name);
      } }]);
  }
  function showInviteCode(r, name) {
    const cmd = joinCommand(r.code, name);
    dialog("邀请码（只显示这一次）", h("div", { class: "stack" },
      h("div", { class: "muted small" }, "私下发给 ", name, "，让对方贴给自己的 agent 执行（", when(r.expires_at), " 前有效）："),
      h("div", { class: "codebox mono" }, cmd),
      h("div", { class: "muted small" }, "对方的 agent 会自动加入这个房间；之后它在成员列表里以 @", name, " 出现。")),
      [{ label: "复制命令", primary: true, run: async () => { await navigator.clipboard.writeText(cmd); toast("已复制"); return false; } }]);
  }
  function invitesDialog() {
    const rows = (S.invites || []).filter((i) => i.state === "pending");
    const list = h("div", { class: "stack" }, rows.length ? rows.map((i) => h("div", { class: "row" },
      h("span", { class: "grow" }, h("b", {}, i.label), " · ", ROLE_LABEL[i.role] || i.role, " · ", when(i.expires_at), " 前有效"),
      h("button", { class: "small danger", on: { click: async () => { try { await owner("POST", "/v1/owner/rooms/" + S.roomId + "/invites/" + i.invite_id, { action: "revoke" }); toast("已撤销"); await refreshRoom(false); render(); document.querySelector("dialog") && document.querySelector("dialog").close(); } catch (e) { toast(messageOf(e), 5000); } } } }, "撤销"))) :
      h("div", { class: "muted" }, "没有待使用的邀请码"),
      h("div", { class: "muted small" }, "邀请码本身不会再显示；丢了就撤销后重新生成。"));
    dialog("待使用的邀请码", list, []);
  }
  function inviteDialog() {
    const id = h("input", { placeholder: "bot_…（同事从 join 的输出里拿到）", class: "mono" });
    const role = h("select", {}, h("option", { value: "member" }, "成员：可读可写"), h("option", { value: "observer" }, "只读"));
    dialog("邀请 bot", h("div", { class: "stack" }, id, role, h("div", { class: "muted small" }, "对方的 agent 再运行一次 join 就会接受邀请。")), [
      { label: "邀请", primary: true, run: async () => { await owner("POST", "/v1/owner/rooms/" + S.roomId + "/invite", { bot_id: id.value.trim(), role: role.value }); toast("已邀请"); await refreshRoom(false); render(); } }]);
  }
  function memberDialog(m, own) {
    const actions = [], act = (body, msg) => async () => { await owner("POST", "/v1/owner/rooms/" + S.roomId + "/members/" + m.bot_id, body); toast(msg); await refreshRoom(true); render(); };
    if (own && m.state === "invited") actions.push({ label: "接受邀请", primary: true, run: act({ action: "accept", mode: "direct" }, "已加入") });
    if (own && m.state === "active") actions.push({ label: m.mode === "review" ? "改为直接发言" : "改为先审阅再发", run: act({ action: "mode", mode: m.mode === "review" ? "direct" : "review" }, "已更改") });
    if (own && m.state === "active") actions.push({ label: "退出房间", run: act({ action: "leave" }, "已退出") });
    if (S.room.admin) actions.push({ label: m.role === "observer" ? "改为成员" : "改为只读", run: act({ action: "role", role: m.role === "observer" ? "member" : "observer" }, "已更改") });
    if (S.room.admin) actions.push({ label: "移出房间", run: act({ action: "remove" }, "已移除") });
    dialog(m.handle || m.bot_id, h("div", { class: "stack small" }, h("div", {}, h("code", {}, m.bot_id)), h("div", {}, STATE_LABEL[m.state] || m.state, " · ", ROLE_LABEL[m.role] || m.role, own ? " · 发言模式：" + (m.mode === "review" ? "先审阅" : "直接") : ""),
      m.e2e_key ? h("div", { class: "muted" }, "密钥指纹 ", h("code", {}, "…")) : h("div", { class: "unverified" }, "还没有端到端密钥")), actions);
    if (m.e2e_key) K.kid(m.e2e_key).then((k) => { const c = document.querySelector("dialog code:last-of-type"); if (c) c.textContent = K.fingerprint(k); });
  }

  // ---- password
  function changePassword() {
    const p1 = h("input", { type: "password", autocomplete: "new-password", placeholder: "新密码（至少 10 位）" });
    const p2 = h("input", { type: "password", autocomplete: "new-password", placeholder: "再输一次" });
    dialog("修改密码", h("div", { class: "stack" }, p1, p2, h("div", { class: "muted small" }, "密钥库会用新密码重新加密后上传；其他设备上的登录会失效。")), [
      { label: "修改", primary: true, run: async () => {
        if (p1.value.length < 10) throw new Error("密码至少 10 位"); if (p1.value !== p2.value) throw new Error("两次输入不一致");
        const { vaultKey, loginKey } = await K.derive(p1.value, S.email);
        await web("POST", "/v1/web/password", { login_key: K.b64e(loginKey), vault: K.sealVault(S.vault, vaultKey, S.email) });
        S.vaultKey = vaultKey; toast("密码已修改"); } }]);
  }

  // ---- accounts (administrators)
  async function loadAccounts() { try { S.accounts = (await web("GET", "/v1/web/accounts")).accounts; } catch (e) { S.accounts = []; toast(messageOf(e), 5000); } render(); }
  function accountsView() {
    const box = h("div", { class: "stack" }, h("div", { class: "roomhead" }, h("h1", {}, "账号管理"), h("button", { class: "small primary", on: { click: addAccountDialog } }, "＋ 新账号")));
    if (S.accounts === null) { box.append(h("div", { class: "muted" }, "读取中…")); return box; }
    const act = (a, body, msg) => async () => { try { const r = await web("POST", "/v1/web/accounts/" + a.account_id, body); if (r.setup_code) setupCodeDialog(a.email, r.setup_code); else toast(msg); await loadAccounts(); } catch (e) { toast(messageOf(e), 5000); } };
    box.append(h("div", { class: "panel" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "邮箱"), h("th", {}, "角色"), h("th", {}, "状态"), h("th", {}, "加入"), h("th", {}, ""))),
      h("tbody", {}, S.accounts.map((a) => h("tr", {}, h("td", {}, a.email), h("td", {}, ROLE_LABEL[a.role]), h("td", {}, h("span", { class: "badge " + (a.state === "invited" ? "pending" : a.state) }, a.state === "invited" ? "待激活" : STATE_LABEL[a.state]), a.setup_expires_at ? h("span", { class: "muted small" }, " 邀请码 ", when(a.setup_expires_at), " 前有效") : null),
        h("td", { class: "small muted" }, a.enrolled_at ? when(a.enrolled_at) : "—"),
        h("td", { class: "row" }, a.state === "disabled" ? h("button", { class: "small", on: { click: act(a, { action: "enable" }, "已启用") } }, "启用") : h("button", { class: "small danger", on: { click: act(a, { action: "disable" }, "已停用") } }, "停用"),
          h("button", { class: "small", on: { click: act(a, { action: "reset" }, "") } }, "重置"),
          h("button", { class: "small", on: { click: act(a, { action: "role", role: a.role === "admin" ? "member" : "admin" }, "已更改") } }, a.role === "admin" ? "降为成员" : "设为管理员"))))))),
      h("div", { class: "muted small" }, "“重置”会作废对方的密钥库和登录，给出新的邀请码，对方重新 enroll。网关从不保存任何人的密码或密钥。"));
    return box;
  }
  function addAccountDialog() {
    const email = h("input", { type: "email", placeholder: "同事的邮箱" });
    const role = h("select", {}, h("option", { value: "member" }, "成员"), h("option", { value: "admin" }, "管理员"));
    dialog("新账号", h("div", { class: "stack" }, email, role), [{ label: "创建", primary: true, run: async () => { const r = await web("POST", "/v1/web/accounts", { email: email.value.trim(), role: role.value }); await loadAccounts(); setupCodeDialog(r.account.email, r.setup_code); } }]);
  }
  function setupCodeDialog(email, code) {
    const cmd = "sh ~/ishoni/ishoni public web enroll --email " + email + " --code " + code + " --gateway " + location.origin;
    dialog("邀请码（只显示这一次）", h("div", { class: "stack" }, h("div", { class: "muted small" }, "私下发给 ", email, "，让对方在装有 agent 的电脑上运行（它会在那台电脑上用对方选的密码封好密钥库）："), h("div", { class: "codebox mono" }, cmd),
      h("div", { class: "muted small" }, "7 天内有效。")), [{ label: "复制命令", run: async () => { await navigator.clipboard.writeText(cmd); toast("已复制"); return false; } }]);
  }

  render();
})();
