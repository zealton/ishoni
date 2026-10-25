#!/bin/sh
# Start a team gateway in a container: initialize the home on first boot (or restore it from the Litestream
# replica when the volume is empty but a replica exists), write gateway.json from the environment, migrate an
# older schema, then serve on $PORT behind the platform's HTTPS proxy, with Litestream replicating beside it.
#
# Environment (Railway service variables):
#   PORT                      the port the platform routes to (Railway sets it)
#   ISHONI_PUBLIC_URL         https://gateway.example.com  (the public base URL the gateway prints in cards)
#   ISHONI_OWNER_LABEL        label of the administrator owner created on first boot (default "Team admin")
#   ISHONI_ALLOW_VISITORS     "true" to let anonymous visitor identities enroll (default false: team only)
#   ISHONI_ALLOW_SELF_ENROLL  "true" to let anyone enroll a bot (default false: the administrator adds owners)
#   LITESTREAM_REPLICA_URL    s3://bucket/path (or empty: no replication); LITESTREAM_ACCESS_KEY_ID,
#                             LITESTREAM_SECRET_ACCESS_KEY and optionally LITESTREAM_ENDPOINT, LITESTREAM_REGION
#
# The administrator's owner credential is created once, printed to the deploy log on that first boot only, and
# written to $ISHONI_GATEWAY_HOME/owner.token on the volume. Copy it to the admin device, then delete the file:
#   railway run cat /data/gw/owner.token   (or the platform's shell)   ->   ishoni public owner login URL --token-file F
set -eu
umask 077

HOME_DIR="${ISHONI_GATEWAY_HOME:-/data/gw}"
# The platform mounts the volume owned by root: give it to the gateway's user once, then continue as that user.
if [ "$(id -u)" = "0" ]; then
  mkdir -p "$HOME_DIR"
  chown -R ishoni:ishoni "$(dirname "$HOME_DIR")"
  exec setpriv --reuid=ishoni --regid=ishoni --init-groups "$0" "$@"
fi
PORT="${PORT:-7780}"
PUBLIC_URL="${ISHONI_PUBLIC_URL:-http://127.0.0.1:$PORT}"
PY="python3 /app/public_bots/bot_gateway.py --gateway-home $HOME_DIR"
DB="$HOME_DIR/gateway.sqlite3"
mkdir -p "$HOME_DIR"

if [ ! -f "$DB" ] && [ -n "${LITESTREAM_REPLICA_URL:-}" ]; then
  echo "==> no database on the volume: trying to restore from $LITESTREAM_REPLICA_URL"
  litestream restore -if-replica-exists -o "$DB" "$LITESTREAM_REPLICA_URL" || true
  [ -f "$DB" ] && chmod 0600 "$DB"
fi

if [ ! -f "$HOME_DIR/gateway.json" ] && [ ! -f "$DB" ]; then
  echo "==> first boot: initializing the gateway home"
  $PY init --port "$PORT" --public-url "$PUBLIC_URL" --owner-label "${ISHONI_OWNER_LABEL:-Team admin}"
  echo "==> the administrator's owner credential is in $HOME_DIR/owner.token: copy it to the admin device and delete it here"
elif [ ! -f "$HOME_DIR/gateway.json" ]; then
  echo "==> restored database without gateway.json: writing one"
fi

# gateway.json is derived from the environment on every start, so a changed variable takes effect on redeploy.
# `trust_forwarded_for: "private"`: the only way into the container is Railway's edge proxy, which connects from a
# private address and sets X-Forwarded-For; the per-IP limits read it. `external_checkpoints` leaves WAL checkpoints
# to Litestream when it runs.
python3 - "$HOME_DIR" "$PORT" "$PUBLIC_URL" <<'PY'
import json, os, sys
home, port, public = sys.argv[1], int(sys.argv[2]), sys.argv[3]
path = os.path.join(home, "gateway.json")
cfg = json.load(open(path)) if os.path.exists(path) else {"v": 1, "limits": {}, "mcp": {"enabled": True, "allowed_origins": []}}
flag = lambda name: os.environ.get(name, "").strip().lower() in ("1", "true", "yes")
cfg.update({"v": 1, "bind": "0.0.0.0", "port": port, "public_base_url": public, "allow_non_loopback_bind": True,
            "trust_forwarded_for": "private", "external_checkpoints": bool(os.environ.get("LITESTREAM_REPLICA_URL"))})
enrollment = cfg.get("enrollment") or {}
enrollment.update({"visitors": flag("ISHONI_ALLOW_VISITORS"), "bots": flag("ISHONI_ALLOW_SELF_ENROLL"),
                   "self_enrolled_publish": flag("ISHONI_ALLOW_SELF_ENROLL"), "email_verification": False})
cfg["enrollment"] = enrollment
tmp = path + ".tmp"
with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
    json.dump(cfg, f, indent=2); f.write("\n")
os.replace(tmp, path)
PY

echo "==> migrate (a no-op when the schema is current)"
$PY migrate || true

if [ -n "${LITESTREAM_REPLICA_URL:-}" ]; then
  echo "==> serving on :$PORT with Litestream replication"
  exec litestream replicate -config /app/litestream.yml -exec "python3 /app/public_bots/bot_gateway.py --gateway-home $HOME_DIR serve --bind 0.0.0.0 --port $PORT"
fi
echo "==> serving on :$PORT (no replication: back up the volume yourself)"
exec $PY serve --bind 0.0.0.0 --port "$PORT"
