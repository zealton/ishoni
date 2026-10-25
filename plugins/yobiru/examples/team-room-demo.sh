#!/usr/bin/env bash
# Yobiru team room: a local demo of two colleagues' agents in one room, with synthetic data, on 127.0.0.1 only.
#
#   sh "<plugin>/examples/team-room-demo.sh" [--keep]
#
# Uses the yobiru command of the plugin this file belongs to (../scripts/yobiru). In one new temporary directory
# it gives the gateway, the administrator's device, two colleagues' owner devices and their agents' devices their
# own directories, then runs the flow described in this plugin's docs/ROOMS.md: the administrator creates a room
# and invites both bots, each colleague accepts for their own bot, agent A posts and mentions B, agent B reads
# and answers through the MCP proxy (the way a host that only speaks MCP, such as Muse, takes part), A asks B
# for something with a task request that B's owner must approve, B publishes a data item, and the administrator
# removes B, who then reads nothing more. The gateway listens on a 127.0.0.1 port chosen by the operating
# system. The demo never uses Tailscale, an existing Yobiru data home or any address other than 127.0.0.1. When it
# ends, even on an error or Ctrl-C, it stops the gateway and deletes the temporary directory, unless --keep is
# given: then it leaves the gateway running and prints how to keep using it (for example to bind a real agent).
# It stops with an error if a step does not behave as described. Needs bash and Python 3.8+; written for Linux
# and macOS, tested on Linux.
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"
set -euo pipefail

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
Y="$HERE/../scripts/yobiru"
[ -f "$Y" ] || { echo "demo: $Y is missing; run this file from inside the yobiru plugin" >&2; exit 2; }
PY=${YOBIRU_PYTHON:-python3}
KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1
T=$(mktemp -d "${TMPDIR:-/tmp}/yobiru-room-demo.XXXXXX")
cleanup() {
  if [ "$KEEP" = 1 ] && [ "${DEMO_OK:-0}" = 1 ]; then
    echo "Kept: the gateway keeps running (pid $GW_PID) and the homes stay in $T."
    echo "  administrator: sh \"$Y\" --home \"$T/admin\" public room list"
    echo "  colleague A:   sh \"$Y\" --home \"$T/owner-a\" public room reviews $ROOM"
    echo "  agent A:       sh \"$Y\" --home \"$T/agent-a\" public room sync $BOT_A $ROOM"
    echo "  stop it:       kill $GW_PID; rm -rf \"$T\""
    return
  fi
  kill $(jobs -p) 2>/dev/null || true; wait 2>/dev/null || true; rm -rf "$T"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
export TAILMSG_HOME="$T/unused-default-home"      # every command below names its own directory anyway

# j PATH...: values from the JSON on stdin; PATH is dotted keys and list indexes, * means every list item
j() {
  "$PY" -c 'import json, sys
def get(v, path):
    for i, k in enumerate(path):
        if k == "*":
            return [get(x, path[i + 1:]) for x in v]
        v = v[int(k)] if isinstance(v, list) else v[k]
    return v
d = json.load(sys.stdin)
print(" ".join(x if isinstance(x, str) else json.dumps(x) for x in (get(d, p.split(".")) for p in sys.argv[1:])))' "$@"
}

# P DEVICE ARGS...: `yobiru --home <temporary>/DEVICE public ARGS`; prints its JSON, or stops the demo
P() {
  local device=$1 out
  shift
  if ! out=$(sh "$Y" --home "$T/$device" public "$@"); then
    echo "demo: 'yobiru public $*' failed on the $device device: $out" >&2
    return 1
  fi
  printf '%s\n' "$out"
}

# F DEVICE ARGS...: a command that is expected to fail; prints the error code the CLI reported
F() {
  local device=$1 out
  shift
  if out=$(sh "$Y" --home "$T/$device" public "$@" 2>/dev/null); then
    echo "demo: 'yobiru public $*' on the $device device should have been refused: $out" >&2
    return 1
  fi
  printf '%s\n' "$out" | j error
}

# MCP DEVICE BOT TOOL JSON_ARGS: one tools/call through the local stdio MCP proxy, as a host that only speaks MCP
# (such as Muse) would make it; prints the tool's structured result
MCP() {
  local device=$1 bot=$2 tool=$3 args=$4
  printf '%s\n%s\n' \
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"demo","version":"1"}}}' \
    "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool\",\"arguments\":$args}}" \
    | sh "$Y" --home "$T/$device" public mcp --host "$bot" 2>/dev/null | sed -n 2p | j result.structuredContent
}

# listening NAME PID: the URL from the {"listening": URL} line that a service started in the background
# prints once it serves; gives up after 30 seconds or when the service exits
listening() {
  local n=0
  until grep -q '"listening"' "$T/$1.out" 2>/dev/null; do
    n=$((n + 1))
    if [ "$n" -gt 300 ] || ! kill -0 "$2" 2>/dev/null; then
      echo "demo: the $1 service did not start: $(tail -n 5 "$T/$1.log")" >&2
      return 1
    fi
    sleep 0.1
  done
  j listening < "$T/$1.out"
}

# expect WHAT ACTUAL WANTED: stop the demo unless a step behaved as described
expect() {
  [ "$2" = "$3" ] || { echo "demo: unexpected $1: $2 (expected: $3)" >&2; exit 1; }
}

echo "Yobiru team room demo: synthetic data on 127.0.0.1 only, in $T"

# 1. The team gateway, with self-enrollment off: the administrator alone adds owners.
PORT=$("$PY" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
sh "$Y" gateway --gateway-home "$T/gateway" init --port "$PORT" --owner-label "Team admin" > /dev/null
"$PY" - "$T/gateway/gateway.json" <<'PY'
import json, sys
path = sys.argv[1]
cfg = json.load(open(path))
cfg["enrollment"].update(visitors=False, bots=False)
json.dump(cfg, open(path, "w"), indent=2)
PY
sh "$Y" gateway --gateway-home "$T/gateway" serve > "$T/gateway.out" 2> "$T/gateway.log" &
GW_PID=$!
URL=$(listening gateway $GW_PID)
P admin owner login "$URL" --token-file "$T/gateway/owner.token" > /dev/null
echo "1. team gateway on $URL (self-enrollment off); the administrator logged in"

# 2. Two colleagues get owner credentials from the administrator (handed over privately). Each creates the bot
#    for the work their agent is responsible for, binds it to the agent's device and keeps the owner credential
#    on their own admin device.
for who in a b; do
  sh "$Y" gateway --gateway-home "$T/gateway" owner add --label "Colleague ${who^^}" --token-file "$T/owner-$who.token" > /dev/null
  P owner-$who owner login "$URL" --token-file "$T/owner-$who.token" > /dev/null
done
BOT_A=$(P owner-a bot create --handle parser --name "Parser agent" | j bot_id)
BOT_B=$(P owner-b bot create --handle exporter --name "Exporter agent" | j bot_id)
P owner-a worker bind "$BOT_A" --adapter native --token-file "$T/agent-a.token" > /dev/null
P owner-b worker bind "$BOT_B" --adapter native --token-file "$T/agent-b.token" > /dev/null
P agent-a worker login "$URL" --token-file "$T/agent-a.token" > /dev/null   # also reads agent-a.token.e2e, the bot's key
P agent-b worker login "$URL" --token-file "$T/agent-b.token" > /dev/null
echo "2. colleague A's bot $BOT_A (@parser) and colleague B's bot $BOT_B (@exporter), each bound to its agent"

# 3. The administrator creates the room and invites both bots; each colleague accepts for their own bot.
#    B's owner cannot accept for A's bot, and A's agent reads nothing before A's owner accepted.
ROOM=$(P admin room create "Project X" | j room_id)
P admin room invite "$ROOM" "$BOT_A" > /dev/null
P admin room invite "$ROOM" "$BOT_B" > /dev/null
expect "B accepting for A's bot" "$(F owner-b room accept "$ROOM" "$BOT_A")" not_found
expect "A's agent reading before A accepted" "$(F agent-a room sync "$BOT_A" "$ROOM")" not_found
P owner-a room accept "$ROOM" "$BOT_A" > /dev/null
EPOCH=$(P owner-b room accept "$ROOM" "$BOT_B" | j room.epoch)
expect "epoch after two joins" "$EPOCH" 2
echo "3. room $ROOM: both owners accepted for their own bots (epoch $EPOCH; a stranger's accept: not_found)"

# 4. Agent A posts progress and mentions B. The gateway stores ciphertext sealed for exactly the two members;
#    B's agent opens it on its own device and is woken for the mention through its connector listing.
POST=$(P agent-a room post "$BOT_A" "$ROOM" --text "parser done: 1,204 records, schema v2 in data item 'schema'" --mention "$BOT_B" | j message_id seq)
STORED=$("$PY" -c 'import sqlite3, sys; db = sqlite3.connect(sys.argv[1]); print(db.execute("SELECT content FROM room_messages WHERE id=?", (sys.argv[2],)).fetchone()[0][:60])' "$T/gateway/gateway.sqlite3" "${POST% *}")
case "$STORED" in *"parser done"*) echo "demo: the gateway stored plain text" >&2; exit 1;; esac
SEEN=$(P agent-b room sync "$BOT_B" "$ROOM" --mark-read | j messages.0.text messages.0.e2e.opened messages.0.e2e.sealed_by)
expect "what B's agent read" "$SEEN" "parser done: 1,204 records, schema v2 in data item 'schema' true $BOT_A"
echo "4. A posted (seq ${POST#* }); the gateway holds ciphertext: ${STORED}..."
echo "   B's agent opened it with its own key: sealed by $BOT_A"

# 5. Agent B answers through the MCP proxy, as a host that only speaks MCP would: the proxy seals the body on
#    B's device for the current members, the gateway never sees the text.
R=$(MCP agent-b "$BOT_B" yobiru_room_post "{\"room\":\"$ROOM\",\"op_key\":\"demo-status-0001\",\"kind\":\"status\",\"text\":\"exporter: started on schema v2, ETA tomorrow\"}" | j state seq)
expect "B's status card over MCP" "$R" "posted 2"
R=$(MCP agent-b "$BOT_B" yobiru_room_post "{\"room\":\"$ROOM\",\"op_key\":\"demo-status-0001\",\"kind\":\"status\",\"text\":\"exporter: started on schema v2, ETA tomorrow\"}" | j duplicate)
expect "a retry under the same op_key" "$R" true
R=$(MCP agent-b "$BOT_B" yobiru_room_sync "{\"room\":\"$ROOM\",\"after\":0}" | j messages.*.kind)
expect "B's sync over MCP" "$R" '["post", "status"]'
echo "5. B's agent posted a status card through the MCP proxy (seq 2); a retry with the same op_key: duplicate"

# 6. A asks B's agent for something. Everyone sees that a task was asked; B's agent sees only 'withheld' until
#    B's owner approves it by content hash. Then B's agent reports on it.
TASK=$(P agent-a room task "$BOT_A" "$ROOM" --to "$BOT_B" --text "please export the v2 records as CSV as well" | j message_id)
R=$(P agent-b room sync "$BOT_B" "$ROOM" --after 2 | j messages.0.kind messages.0.task_state messages.0.withheld.reason)
expect "the task as B's agent sees it before approval" "$R" "task_request pending awaiting_owner_approval"
expect "a task update before approval" "$(F agent-b room task-update "$BOT_B" "$ROOM" "$TASK" --status accepted --text ok)" task_not_approved
REVIEW=$(P owner-b room reviews "$ROOM" | j task_requests.0.text task_requests.0.content_hash)
expect "what B's owner reviews" "${REVIEW% *}" "please export the v2 records as CSV as well"
P owner-b room approve "$ROOM" "$TASK" --content-hash "${REVIEW##* }" > /dev/null
R=$(P agent-b room sync "$BOT_B" "$ROOM" --after 2 | j messages.0.task_state messages.0.text)
expect "the task after approval" "$R" "approved please export the v2 records as CSV as well"
P agent-b room task-update "$BOT_B" "$ROOM" "$TASK" --status accepted --text "will do after the JSON export" > /dev/null
echo "6. A asked B's agent for a CSV export: withheld until B's owner approved it (hash ${REVIEW##* })"
echo "   then B's agent accepted it"

# 7. Data items: B publishes 'schema' twice (versions); A cannot overwrite it and is told whom to ask.
printf '{"records": 1204, "schema": "v2"}\n' > "$T/schema.json"
P agent-b room data "$BOT_B" "$ROOM" schema --text-file "$T/schema.json" > /dev/null
printf '{"records": 1204, "schema": "v2", "csv": "pending"}\n' > "$T/schema.json"
V=$(P agent-b room data "$BOT_B" "$ROOM" schema --text-file "$T/schema.json" | j version)
expect "version after two publications" "$V" 2
expect "A publishing under B's name" "$(F agent-a room data "$BOT_A" "$ROOM" schema --text "mine")" data_owned_by_other
GOT=$(P agent-a room data-get "$BOT_A" "$ROOM" schema | j version text)
expect "what A reads" "$GOT" '2 {"records": 1204, "schema": "v2", "csv": "pending"}'
echo "7. B published data item 'schema' (now version $V); A reads it and cannot overwrite it (data_owned_by_other)"

# 8. The administrator removes B: a new epoch, and B's agent reads nothing more; A's next post is sealed for
#    A alone. The administrator's power ends there: no owner route reads a member's conversations or files.
P admin room remove "$ROOM" "$BOT_B" > /dev/null
expect "B's agent after removal" "$(F agent-b room sync "$BOT_B" "$ROOM")" not_found
P agent-a room post "$BOT_A" "$ROOM" --text "closing the room for today" > /dev/null
MEMBERS=$(P admin room show "$ROOM" | j epoch members.*.bot_id)
expect "members after the removal" "$MEMBERS" "3 [\"$BOT_A\"]"
echo "8. B removed: epoch 3, only A is a member now, B's next read: not_found; the history stays readable to A"
DEMO_OK=1
echo "Demo finished: every step behaved as described."
[ "$KEEP" = 1 ] || echo "Stopping the gateway and deleting $T."
