#!/bin/sh
# Create and deploy the team gateway with the Railway CLI (https://docs.railway.com/guides/cli), from a clone of
# this repository, after `railway login`. Every step is idempotent enough to rerun after a failure; the dashboard
# steps in docs/DEPLOY_RAILWAY.md are the same thing done by hand.
#
#   sh deploy/railway/setup.sh                      # project "ishoni-gateway", service "gateway"
#   PROJECT=my-gw SERVICE=gw sh deploy/railway/setup.sh
#
# Optional replication: export LITESTREAM_REPLICA_URL, LITESTREAM_ACCESS_KEY_ID, LITESTREAM_SECRET_ACCESS_KEY and
# (for R2/B2/MinIO) LITESTREAM_ENDPOINT, LITESTREAM_REGION before running; they are copied to the service.
set -eu
cd "$(dirname "$0")/../.."
PROJECT="${PROJECT:-ishoni-gateway}"
SERVICE="${SERVICE:-gateway}"
command -v railway >/dev/null || { echo "install the Railway CLI first: brew install railway  (or npm i -g @railway/cli)"; exit 1; }
railway whoami >/dev/null 2>&1 || { echo "run: railway login"; exit 1; }

if ! railway status >/dev/null 2>&1; then
  echo "==> creating project $PROJECT"
  railway init --name "$PROJECT"
fi
if ! railway status 2>/dev/null | grep -q "$SERVICE"; then
  echo "==> creating service $SERVICE"
  railway add --service "$SERVICE"
fi
railway service "$SERVICE"

echo "==> volume at /data (kept if it already exists)"
railway volume list 2>/dev/null | grep -q "/data" || railway volume add --mount-path /data

echo "==> public domain"
DOMAIN=$(railway domain 2>/dev/null | grep -oE 'https://[a-zA-Z0-9.-]+' | head -1 || true)
[ -n "$DOMAIN" ] || DOMAIN=$(railway domain | grep -oE 'https://[a-zA-Z0-9.-]+' | head -1)
echo "    $DOMAIN"

echo "==> variables"
railway variables --set "ISHONI_PUBLIC_URL=$DOMAIN" --set "ISHONI_OWNER_LABEL=${ISHONI_OWNER_LABEL:-Team admin}" --skip-deploys
if [ -n "${LITESTREAM_REPLICA_URL:-}" ]; then
  railway variables --skip-deploys \
    --set "LITESTREAM_REPLICA_URL=$LITESTREAM_REPLICA_URL" \
    --set "LITESTREAM_ACCESS_KEY_ID=${LITESTREAM_ACCESS_KEY_ID:?}" \
    --set "LITESTREAM_SECRET_ACCESS_KEY=${LITESTREAM_SECRET_ACCESS_KEY:?}" \
    --set "LITESTREAM_ENDPOINT=${LITESTREAM_ENDPOINT:-}" --set "LITESTREAM_REGION=${LITESTREAM_REGION:-}"
fi

echo "==> deploying the working tree (railway.toml at the root points at deploy/railway/Dockerfile)"
railway up --service "$SERVICE" --detach

cat <<MSG

Next, once the deploy log shows "serving on":
  curl -s $DOMAIN/v1/health
  railway ssh --service $SERVICE -- cat /data/gw/owner.token > admin.token && chmod 600 admin.token
  railway ssh --service $SERVICE -- rm /data/gw/owner.token
  sh plugins/ishoni/scripts/ishoni public owner login $DOMAIN --token-file admin.token
MSG
