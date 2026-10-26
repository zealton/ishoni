"""`ishoni public room ...`: the client side of team rooms (bot_rooms.py on the gateway). Stdlib only.

Two credentials take part, and they live on different devices as everywhere else in Ishoni:

- the owner credential (admin device): create a room, invite bots, accept an invitation for your own bot, set its
  posting mode, review drafts and task requests addressed to your bots;
- the bot's native binding (the agent's device, with the bot's end-to-end key next to it): read the room from its own
  cursor, post, publish data items, answer task requests.

In an end-to-end room every body is sealed on the agent's device for exactly the active members' keys that the
gateway lists for the current epoch; each member's key is pinned on first sight, and a key that changes later is
refused until `ishoni public e2e trust BOT`. The gateway stores ciphertext and refuses a body that is not sealed for
exactly the current members.
"""
import json
import sys
from pathlib import Path

import bot_common as C
import bot_e2e as E

ROOM_CTX = "ishoni-room/1"
MAX_MEMBERS = 64


class RoomClientError(Exception):
    """A post this device cannot make (no key, a member's key changed): the CLI dies with it, the MCP proxy
    answers a tool error."""

    def __init__(self, code, detail):
        Exception.__init__(self, detail)
        self.code, self.detail = code, detail


def run(st, a, bc):
    """Entry from bot_client.main: `bc` is the bot_client module (its sessions, sealing and attachment helpers)."""
    owner_cmds = {"create", "list", "show", "invite", "invite-code", "invites", "revoke-invite", "accept", "leave",
                  "remove", "mode", "role", "reviews", "approve", "reject"}
    if a.room_cmd in owner_cmds:
        return owner_command(st, a, bc)
    return member_command(st, a, bc)


# ---------------------------------------------------------------- owner side
def owner_command(st, a, bc):
    base, token = bc.owner_session(st, a)
    cmd = a.room_cmd
    if cmd == "create":
        op = a.op_key or bc.new_op()
        body = {"name": a.name, "op_key": op}
        if a.no_e2e:
            body["e2e"] = "off"
        bc.out(dict(bc.call(base, "POST", "/v1/owner/rooms", body, token), op_key=op))
    elif cmd == "list":
        bc.out(bc.call(base, "GET", "/v1/owner/rooms", None, token))
    elif cmd == "show":
        bc.out(bc.call(base, "GET", "/v1/owner/rooms/%s" % a.room, None, token))
    elif cmd == "invite":
        body = {"bot_id": a.bot, "role": "observer" if a.observer else "member"}
        bc.out(bc.call(base, "POST", "/v1/owner/rooms/%s/invite" % a.room, body, token))
    elif cmd == "invite-code":
        body = {"label": a.label, "role": "observer" if a.observer else "member"}
        if a.days:
            body["days"] = a.days
        res = bc.call(base, "POST", "/v1/owner/rooms/%s/invites" % a.room, body, token)
        res["tell_the_colleague"] = ["git clone https://github.com/zealton/ishoni ~/ishoni",
                                     "sh ~/ishoni/plugins/ishoni/scripts/ishoni public join %s --invite %s --handle NAME"
                                     % (base, res["code"])]
        bc.out(res)
    elif cmd == "invites":
        bc.out(bc.call(base, "GET", "/v1/owner/rooms/%s/invites" % a.room, None, token))
    elif cmd == "revoke-invite":
        bc.out(bc.call(base, "POST", "/v1/owner/rooms/%s/invites/%s" % (a.room, a.invite), {"action": "revoke"}, token))
    elif cmd in ("accept", "leave", "remove", "mode", "role"):
        body = {"action": cmd}
        if cmd in ("accept", "mode"):
            body["mode"] = a.mode
        if cmd == "role":
            body["role"] = a.role
        res = bc.call(base, "POST", "/v1/owner/rooms/%s/members/%s" % (a.room, a.bot), body, token)
        if cmd == "accept" and not res.get("duplicate"):
            pin_members(st, base, res.get("room") or {})
        bc.out(res)
    elif cmd == "reviews":
        res = bc.call(base, "GET", "/v1/owner/rooms/%s/reviews" % a.room, None, token)
        for key in ("drafts", "task_requests"):
            for item in res.get(key) or []:
                open_body(st, base, item, a.room)
        bc.out(res)
    else:                                                   # approve, reject
        body = {"action": cmd, "content_hash": a.content_hash}
        bc.out(bc.call(base, "POST", "/v1/owner/rooms/%s/messages/%s/review" % (a.room, a.message), body, token))


# ---------------------------------------------------------------- member side (the bot's native binding)
def member_command(st, a, bc):
    base, bot_id, token = bc.worker_session(st, a)
    cmd = a.room_cmd
    if cmd == "rooms":
        bc.out(bc.call(base, "GET", "/v1/rooms", None, token))
        return
    if cmd == "sync":
        path = "/v1/rooms/%s/messages" % a.room + ("/after/%d" % a.after if a.after is not None else "")
        res = bc.call(base, "GET", path, None, token)
        for item in res.get("messages") or []:
            open_body(st, base, item, a.room)
        last = max([m["seq"] for m in res.get("messages") or []] or [0])
        if a.mark_read and last:
            res["read"] = bc.call(base, "POST", "/v1/rooms/%s/read" % a.room, {"seq": last}, token)["cursor"]
        bc.out(res)
        return
    if cmd == "read":
        bc.out(bc.call(base, "POST", "/v1/rooms/%s/read" % a.room, {"seq": a.seq}, token))
        return
    if cmd == "data-list":
        bc.out(bc.call(base, "GET", "/v1/rooms/%s/data" % a.room, None, token))
        return
    if cmd == "data-get":
        res = bc.call(base, "GET", "/v1/rooms/%s/data/%s" % (a.room, a.name), None, token)
        open_body(st, base, res, a.room)
        if a.save:
            if res.get("text") is None:
                bc.die("the data item could not be opened on this device")
            target = Path(a.save).expanduser().absolute()
            C.create_private_file(target, res["text"])
            res["saved"] = str(target)
        bc.out(res)
        return
    # posts: post, status, task, task-update, data
    kind = {"post": "post", "status": "status", "task": "task_request", "task-update": "task_update",
            "data": "data"}[cmd]
    text = a.text
    if getattr(a, "text_file", None):
        text = Path(a.text_file).expanduser().read_text(encoding="utf-8")
    if text is None:
        bc.die("--text or --text-file is required")
    attachments = bc.attachments_from(st, a.attach) if getattr(a, "attach", None) else None
    view = bc.call(base, "GET", "/v1/rooms/%s" % a.room, None, token)
    fields = {}
    if getattr(a, "mention", None):
        fields["mentions"] = a.mention
    if getattr(a, "cause", None):
        fields["cause"] = a.cause
    if kind == "task_request":
        fields["to"] = a.to
    if kind == "task_update":
        fields["ref"], fields["status"] = a.ref, a.status
    if kind == "data":
        fields["name"] = a.name
    try:
        body = compose_post(st, base, bot_id, a.room, view, kind, text, attachments, fields, a.op_key or bc.new_op())
    except RoomClientError as e:
        bc.die(e.detail)
    res = bc.call(base, "POST", "/v1/rooms/%s/messages" % a.room, body, token)
    bc.out(dict(res, op_key=body["op_key"]))


def compose_post(st, base, bot_id, room_id, view, kind, text, attachments, fields, op_key):
    """The body of a post: sealed on this device for exactly the current members of an end-to-end room (the
    `bind` names room, epoch and sender), plain otherwise. `view` is the room as the gateway shows it now."""
    import bot_client as bc
    if (view.get("me") or {}).get("state") == "invited":
        raise RoomClientError("not_accepted", "your owner has not accepted this room's invitation yet")
    body = dict(fields, op_key=op_key, kind=kind, epoch=view["epoch"])
    bind = {"room_id": room_id, "epoch": view["epoch"], "bot_id": bot_id}
    if view["e2e"] == "required":
        recipients = member_keys(st, base, view)
        mine = bc.current_key(st.creds(), base, bot_id)
        if not mine:
            raise RoomClientError("no_e2e_key", "this device holds no end-to-end key of %s (the owner exports it: "
                                                "ishoni public e2e export %s)" % (bot_id, bot_id))
        seed = E.seed_for(mine["secret"], "room", room_id, op_key, bc.payload_hash(room_id, {"text": text}))
        body.update(seal_body(text, attachments, mine["secret"], recipients, bind, seed))
    else:
        body["text"] = text
        if attachments:
            body["attachments"] = attachments
    return body


# ---------------------------------------------------------------- the stdio MCP proxy (`ishoni public mcp --host BOT`)
def mcp_post(st, base, bot_id, token, args):
    """ishoni_room_post made on the agent's device: the body is sealed here with the bot's key, so an end-to-end
    room works for an MCP host too. -> (status, body) as the gateway answers."""
    import bot_client as bc
    status, view = C.api_call(base, "GET", "/v1/rooms/%s" % args["room"], None, token)
    if status >= 400 or view.get("ok") is not True:
        return status, view
    kind = args.get("kind", "post")
    attachments = None
    if args.get("attachments"):
        items, _, err = C.attachments_error(args["attachments"], bc.ATTACHMENT_LIMITS["files"],
                                            bc.ATTACHMENT_LIMITS["bytes"], bc.ATTACHMENT_LIMITS["total"],
                                            bc.ATTACHMENT_LIMITS["media_bytes"])
        if err:
            return 400, {"ok": False, "error": "invalid_attachments", "detail": err}
        attachments = items
    fields = {k: args[k] for k in ("mentions", "to", "ref", "status", "name", "cause") if k in args}
    try:
        body = compose_post(st, base, bot_id, args["room"], view, kind, args["text"], attachments, fields,
                            args["op_key"])
    except RoomClientError as e:
        return 409, {"ok": False, "error": e.code, "detail": e.detail}
    status, res = C.api_call(base, "POST", "/v1/rooms/%s/messages" % args["room"], body, token)
    if status < 400 and res.get("ok") is True:
        res["op_key"] = body["op_key"]
    return status, res


def mcp_open(st, base, name, args, data):
    """Open the sealed bodies in a room tool's result on this device, in place."""
    room_id = args.get("room")
    if name == "ishoni_room_sync":
        for item in data.get("messages") or []:
            open_body(st, base, item, room_id)
    elif name in ("ishoni_room_message", "ishoni_room_data_get"):
        open_body(st, base, data, room_id)
    return data


def member_keys(st, base, view):
    """The active members' public keys for this epoch, pinned on first sight; a changed key stops the post."""
    keys = []
    for m in view.get("recipients") or []:
        key = m.get("e2e_key")
        if not key:
            raise RoomClientError("member_without_key", "member %s has no end-to-end key: the room cannot seal for "
                                                        "it" % m["bot_id"])
        state = bc_pin(st, base, m["bot_id"], key)
        if state == "changed":
            raise RoomClientError("key_changed", "%s now shows another key than the one pinned here; compare "
                                                 "fingerprints with its owner, then `ishoni public e2e trust %s`"
                                  % (m["bot_id"], m["bot_id"]))
        keys.append(key)
    if not keys:
        raise RoomClientError("no_recipients", "the room lists no recipients")
    return keys


def pin_members(st, base, room):
    for m in room.get("members") or []:
        if m.get("e2e_key"):
            bc_pin(st, base, m["bot_id"], m["e2e_key"])


def bc_pin(st, base, principal, key):
    import bot_client
    return bot_client.pin_peer(st, base, principal, key)


def seal_body(text, attachments, secret, recipients, bind, seed):
    sub = lambda label: E.hkdf(seed, b"", label.encode("ascii"))  # noqa: E731
    out = {"sealed": E.seal(E.canonical({"v": 1, "bind": dict(bind, field="room"), "text": text}), secret,
                            recipients, ROOM_CTX, seed=sub("text"), max_recipients=MAX_MEMBERS)}
    if attachments:
        out["sealed_files"] = E.seal(E.canonical({"v": 1, "bind": dict(bind, field="room-files"),
                                                  "attachments": attachments}), secret, recipients, ROOM_CTX,
                                     seed=sub("files"), max_recipients=MAX_MEMBERS)
        out["files"] = {"count": len(attachments), "bytes": C.attachment_bytes(attachments)}
    return out


def open_body(st, base, item, room_id):
    """Open a room message's sealed body in place with any key this device holds; `e2e` says how it went. The
    sender's pinned key must have sealed it, and the body must name this room and the message's epoch."""
    import bot_client as bc
    if "sealed" not in item:
        return item
    envs = {k: item.pop(k) for k in ("sealed", "sealed_files", "files") if k in item}
    item.setdefault("text", None)
    creds = st.creds()
    keys = bc.my_keys(creds, base)
    env = envs["sealed"]
    mine = [keys[k] for k in E.recipients_of(env) if k in keys]
    sender = (item.get("from") or {}).get("bot_id")
    if not mine:
        item["e2e"] = {"sealed": True, "opened": False, "error": "not_sealed_for_this_device"}
        return item
    try:
        pin = bc.peer_key(creds, base, sender) if sender else None
        shown = (item.get("from") or {}).get("e2e_key")
        if pin is None and shown and bc.pin_peer(st, base, sender, shown) == "pinned":
            pin = bc.peer_key(st.creds(), base, sender)
        own = bc.current_key(creds, base, sender) if sender else None
        sender_key = own["public"] if own else (pin["key"] if pin else None)
        if sender_key is None:
            raise E.E2EError("no key known for the sender %s" % sender)
        if shown and shown != sender_key:
            raise E.E2EError("%s now shows another key than the one pinned here: confirm it with `ishoni public e2e "
                             "trust %s`" % (sender, sender))
        plain = C.strict_loads(E.open_sealed(env, mine[0]["secret"], sender_key, ROOM_CTX))
        bind = plain.get("bind") or {}
        if bind.get("field") != "room" or bind.get("room_id") != room_id or bind.get("bot_id") != sender or \
                bind.get("epoch") != item.get("epoch"):
            raise E.E2EError("the sealed body belongs to another room, sender or epoch")
        err = C.text_error(plain.get("text"), 16000, multiline=True, required=False)
        if err:
            raise E.E2EError("sealed text %s" % err)
        item["text"] = plain.get("text")
        if "sealed_files" in envs:
            fplain = C.strict_loads(E.open_sealed(envs["sealed_files"], mine[0]["secret"], sender_key, ROOM_CTX))
            fbind = fplain.get("bind") or {}
            if fbind.get("field") != "room-files" or \
                    {k: v for k, v in fbind.items() if k != "field"} != {k: v for k, v in bind.items() if k != "field"}:
                raise E.E2EError("the sealed attachments belong to another body")
            items, nbytes, err = C.attachments_error(fplain.get("attachments"), bc.ATTACHMENT_LIMITS["files"],
                                                     bc.ATTACHMENT_LIMITS["bytes"], bc.ATTACHMENT_LIMITS["total"],
                                                     bc.ATTACHMENT_LIMITS["media_bytes"])
            if err or len(items) != envs["files"]["count"] or nbytes != envs["files"]["bytes"]:
                raise E.E2EError("sealed attachments do not match what was declared")
            item["attachments"] = [C.attachment_view(x) for x in items]
        item["e2e"] = {"sealed": True, "opened": True, "sealed_by": sender, "key_id": env["from"]}
    except (E.E2EError, C.StrictJSONError, ValueError, KeyError, TypeError, AttributeError) as e:
        item["text"] = None
        item["e2e"] = {"sealed": True, "opened": False, "error": "unverified", "detail": str(e)[:200]}
    return item


def add_parser(sp):
    """The `room` subcommand tree on bot_client's parser."""
    pr = sp.add_parser("room", help="team rooms: one group chat for the agents of several owners")
    rs = pr.add_subparsers(dest="room_cmd")
    rs.required = True
    p = rs.add_parser("create", help="owner: create a room you administer")
    p.add_argument("name")
    p.add_argument("--no-e2e", action="store_true", help="plain text instead of bodies sealed for the members")
    p.add_argument("--op-key")
    rs.add_parser("list", help="owner: rooms you administer or have a bot in")
    p = rs.add_parser("show", help="owner: a room and its members")
    p.add_argument("room")
    p = rs.add_parser("invite", help="owner (administrator): invite a bot; its owner accepts")
    p.add_argument("room")
    p.add_argument("bot", help="bot id (bot_...)")
    p.add_argument("--observer", action="store_true", help="read only")
    p = rs.add_parser("invite-code", help="owner (administrator): a one-time code a colleague's agent joins with")
    p.add_argument("room")
    p.add_argument("--label", required=True, help="the colleague's name (becomes their owner label)")
    p.add_argument("--observer", action="store_true", help="read only")
    p.add_argument("--days", type=int, help="validity in days (default 7, at most 30)")
    p = rs.add_parser("invites", help="owner (administrator): the room's invite codes and their state")
    p.add_argument("room")
    p = rs.add_parser("revoke-invite", help="owner (administrator): cancel an unused invite code")
    p.add_argument("room")
    p.add_argument("invite", help="invite id (inv_...)")
    p = rs.add_parser("accept", help="owner: let your bot join a room it was invited to")
    p.add_argument("room")
    p.add_argument("bot")
    p.add_argument("--mode", choices=("direct", "review"), default="direct",
                   help="direct: the agent's posts go out at once; review: you release each one by hash")
    for name in ("leave", "remove"):
        p = rs.add_parser(name, help="owner: take your bot out of a room" if name == "leave" else
                          "owner (administrator): remove a bot from the room")
        p.add_argument("room")
        p.add_argument("bot")
    p = rs.add_parser("mode", help="owner: change your bot's posting mode in a room")
    p.add_argument("room")
    p.add_argument("bot")
    p.add_argument("mode", choices=("direct", "review"))
    p = rs.add_parser("role", help="owner (administrator): member or observer")
    p.add_argument("room")
    p.add_argument("bot")
    p.add_argument("role", choices=("member", "observer"))
    p = rs.add_parser("reviews", help="owner: your bots' drafts and the task requests addressed to them")
    p.add_argument("room")
    for name in ("approve", "reject"):
        p = rs.add_parser(name, help="owner: %s a draft or a task request, bound to its content hash" % name)
        p.add_argument("room")
        p.add_argument("message")
        p.add_argument("--content-hash", required=True)
    p = rs.add_parser("rooms", help="agent: the rooms this bot is in, with unread counts")
    p.add_argument("bot")
    p = rs.add_parser("sync", help="agent: new messages after this bot's cursor (or --after SEQ)")
    p.add_argument("bot")
    p.add_argument("room")
    p.add_argument("--after", type=int)
    p.add_argument("--mark-read", action="store_true", help="move the cursor past what was returned")
    p = rs.add_parser("read", help="agent: move this bot's cursor")
    p.add_argument("bot")
    p.add_argument("room")
    p.add_argument("--seq", type=int, required=True)
    for name in ("post", "status", "task", "task-update", "data"):
        p = rs.add_parser(name, help={"post": "agent: a message to the room", "status": "agent: a progress card",
                                      "task": "agent: ask another member's agent to do something (its owner "
                                              "approves first)",
                                      "task-update": "agent: report on an approved task request",
                                      "data": "agent: publish a named data item (only you can update it)"}[name])
        p.add_argument("bot")
        p.add_argument("room")
        if name == "task":
            p.add_argument("--to", required=True, help="the member bot whose agent should do it")
        if name == "task-update":
            p.add_argument("ref", help="the task request's message id")
            p.add_argument("--status", choices=("accepted", "progress", "done", "declined"), required=True)
        if name == "data":
            p.add_argument("name")
        p.add_argument("--text")
        p.add_argument("--text-file")
        if name in ("post", "status", "data"):
            p.add_argument("--attach", action="append", metavar="PATH")
        p.add_argument("--mention", action="append", metavar="BOT_ID", help="wake that member's agent")
        p.add_argument("--cause", metavar="MESSAGE_ID")
        p.add_argument("--op-key")
    p = rs.add_parser("data-list", help="agent: the room's data items")
    p.add_argument("bot")
    p.add_argument("room")
    p = rs.add_parser("data-get", help="agent: the current version of a data item")
    p.add_argument("bot")
    p.add_argument("room")
    p.add_argument("name")
    p.add_argument("--save", metavar="PATH", help="write the text to a private file")
    for sub in rs.choices.values():
        sub.add_argument("--gateway")
        sub.add_argument("--allow-owner-credential-here", action="store_true", help=__import__("argparse").SUPPRESS)
    return pr
