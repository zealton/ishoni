# Deploying a team gateway on Railway

**Status: deployed on Railway on 2026-10-26 with these files (Railway CLI 4.36, Dockerfile builder, one volume, no
replication yet).** This page is for a gateway that one team uses for [team rooms](ROOMS.md) and Friends:
the administrator holds the Railway project and the credentials, self-enrollment stays off, and each colleague gets
one owner credential from the administrator.

The gateway is one Python process (standard library only) with an SQLite database, so it fits one Railway service
with a persistent volume. Litestream inside the same container copies the database to an S3-compatible bucket
within seconds of each change, so a lost volume costs at most a few seconds of writes. Everything the gateway
stores is end-to-end encrypted when both sides use the Ishoni client; the host sees who talks to whom, when and how
much ([storage rules](PUBLIC_BOTS.md#privacy-storage-and-deletion)).

```text
colleagues' agents ──https──> Railway edge proxy ──> gateway container (PORT) ── /data volume (gateway.sqlite3, blobs)
                                                                           └─ Litestream ──> S3 / R2 bucket (optional)
```

## Files

| File | What it does |
|---|---|
| `deploy/railway/Dockerfile` | `python:3.12-slim` + Litestream; copies `public_bots/`; healthcheck on `/v1/health` |
| `deploy/railway/entrypoint.sh` | hands the volume to the `ishoni` user, restores from Litestream when the volume is empty, `init` on first boot, writes `gateway.json` from the environment on every start, `migrate`, `serve` |
| `deploy/railway/litestream.yml` | replica configuration read from `LITESTREAM_*` variables |
| `railway.toml` (repository root) | Dockerfile builder, healthcheck path, one replica, restart on failure; Railway reads it without any dashboard setting |
| `deploy/railway/railway.json` | the same configuration, for a service whose config file path is set by hand |
| `deploy/railway/setup.sh` | the dashboard steps below done with the Railway CLI: project, service, volume, domain, variables, deploy |

Only one replica: SQLite is on one volume and a second process on another machine would not see it. One gateway
process answers roughly 200 connector polls a second, far more than a team needs.

## Steps

With the CLI: `railway login`, then `sh deploy/railway/setup.sh` from a clone does steps 1 to 3 and starts the
deploy; continue at step 4. By hand:

1. **Create the service** from the fork: Railway → New project → Deploy from GitHub repo → `zealton/ishoni`,
   branch `main`. `railway.toml` at the repository root already names `deploy/railway/Dockerfile` and the health
   check, so no build setting is needed; the build context is the repository root.
2. **Add a volume** (service → Volumes → New) mounted at **`/data`**. Everything the gateway keeps lives under
   `/data/gw`: the configuration, the database, the gateway's own key files and the blob store.
3. **Variables** (service → Variables):

   | Variable | Value |
   |---|---|
   | `ISHONI_PUBLIC_URL` | the public HTTPS URL of the service, for example `https://ishoni-gw.up.railway.app` (Settings → Networking → Generate domain; a custom domain works the same) |
   | `ISHONI_OWNER_LABEL` | label of the administrator's owner identity (default `Team admin`) |
   | `ISHONI_ALLOW_VISITORS` | leave unset (`false`): no anonymous visitors on a team gateway |
   | `ISHONI_ALLOW_SELF_ENROLL` | leave unset (`false`): the administrator adds every owner |
   | `LITESTREAM_REPLICA_URL` | `s3://BUCKET/ishoni-gw` to replicate; unset for no replication |
   | `LITESTREAM_ACCESS_KEY_ID`, `LITESTREAM_SECRET_ACCESS_KEY` | the bucket's credentials |
   | `LITESTREAM_ENDPOINT`, `LITESTREAM_REGION` | for R2, B2 or MinIO (`https://ACCOUNT.r2.cloudflarestorage.com`, `auto`); unset for AWS S3 |

   `PORT` is set by Railway. `gateway.json` is rewritten from these variables on every start, so a changed variable
   takes effect on the next deploy; `limits`, `mcp` and `partners` in it are kept as they are and can be edited on the
   volume.
4. **Deploy.** The first boot prints `first boot: initializing the gateway home` and writes the administrator's
   owner credential to `/data/gw/owner.token`. Fetch it once and delete it from the volume:

   ```sh
   railway ssh --service GATEWAY -- cat /data/gw/owner.token > admin.token   # or the service's shell in the dashboard
   railway ssh --service GATEWAY -- rm /data/gw/owner.token
   sh Y --home ADMIN_HOME public owner login https://ishoni-gw.up.railway.app --token-file admin.token
   ```

   Run the `rm` yourself from a terminal: an agent driving the CLI under a permission policy is usually stopped at
   a remote delete, and the file must not stay on the volume.

   The health check (`GET /v1/health`) must answer before Railway routes traffic; the gateway answers it as soon as
   the database is open.
5. **Add the colleagues.** Each colleague gets an owner identity of their own. `gateway owner add` is a local
   command (it writes to the database on the volume), so it runs inside the container; the administrator fetches the
   credential file, deletes it there and hands it over through a private channel (never through a room or a chat):

   ```sh
   railway ssh --service GATEWAY -- python3 /app/public_bots/bot_gateway.py --gateway-home /data/gw \
       owner add --label "Bo" --token-file /data/gw/bo.token            # repeat per colleague
   railway ssh --service GATEWAY -- cat /data/gw/bo.token > bo.token
   railway ssh --service GATEWAY -- rm /data/gw/bo.token
   ```

   Then follow [Setting up a team](ROOMS.md#setting-up-a-team): each colleague logs in on their admin device,
   creates a bot, binds it to their agent and accepts the room invitations for it.

## What the environment changes

- No `VOLUME` instruction in the Dockerfile: Railway refuses one ("use Railway Volumes"); the service's volume is
  mounted at `/data` by the platform.
- `bind: 0.0.0.0` with `allow_non_loopback_bind: true`: the gateway listens on the container's port; only Railway's
  edge proxy reaches it, and the proxy terminates HTTPS. The CLI refuses a plain `http://` public URL except on
  loopback, so `ISHONI_PUBLIC_URL` must be the `https://` URL.
- `trust_forwarded_for: "private"`: the proxy connects from a private address and sets `X-Forwarded-For`; the
  per-IP limits read the client's address from it. `true` (the self-hosted default behind a proxy on the same host)
  would see every request as coming from one address and apply the per-IP limits to the whole team at once.
- `external_checkpoints: true` when `LITESTREAM_REPLICA_URL` is set: Litestream runs the WAL checkpoints
  ([continuous backups](PUBLIC_BOTS.md#continuous-backups)).
- `enrollment`: `visitors` and `bots` from the two `ISHONI_ALLOW_*` variables, `false` by default;
  `email_verification` off (there is no mail relay in the container).

## Operations

- **Upgrade:** push to the deployed branch; Railway rebuilds, the entrypoint runs `gateway migrate` (a no-op when the
  schema is current) and serves. Litestream or a volume snapshot is the backup before an upgrade.
- **Restore:** with replication on, an empty volume is filled from the bucket on boot (`litestream restore
  -if-replica-exists`). Without it, back up the volume with Railway's volume backups.
- **Logs:** the gateway logs one line per request without bodies or credentials; `railway logs --service GATEWAY`.
- **Rotate an owner credential** (lost device, colleague leaving): in the container,
  `python3 /app/public_bots/bot_gateway.py --gateway-home /data/gw owner rotate OWNER_ID --token-file /data/gw/new.token`
  revokes that owner's credentials and writes a new one; fetch and delete it as above.
- **Sleep:** `sleepApplication` is off in `railway.json`; a sleeping gateway would miss the agents' connector polls
  and `room sync` schedules.

## Local check before deploying

```sh
docker build -f deploy/railway/Dockerfile -t ishoni-gw .
docker run --rm -p 7780:7780 -e PORT=7780 -v ishoni-gw-data:/data ishoni-gw
curl -s http://127.0.0.1:7780/v1/health
```

Without `ISHONI_PUBLIC_URL` the container initializes with `http://127.0.0.1:7780`, which the CLI accepts because
it is loopback; a gateway initialized that way and later given a public URL takes the new URL from the environment
on the next start.
