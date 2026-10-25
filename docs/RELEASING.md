# Building and releasing Ishoni

Everything is plain Python 3.8+ with the standard library; no network access is needed.

## Sources of truth

- `packaging/product.json`: plugin id, display name, version, publisher, license, descriptions,
  marketplace names, and which root files are bundled.
- `tailmsg.py` and the Codex bridge files at the repository root are canonical. The plugin carries
  byte-identical copies under `plugins/ishoni/scripts/`, listed with SHA-256 in `scripts/SOURCES.json`.
- Generated, never edited by hand: `plugins/ishoni/plugin.json`, `plugins/ishoni/.codex-plugin/plugin.json`,
  `plugins/ishoni/.cursor-plugin/plugin.json`, `.agents/plugins/marketplace.json`,
  `.cursor-plugin/marketplace.json`, the bundled copies, `plugins/ishoni/LICENSE` and `SOURCES.json`.
- Hand-written: `scripts/ishoni`, `scripts/ishoni.py`, `skills/*/SKILL.md` and `README.md`.
- Host-specific behavior stays in the shared skills: `ishoni-grok-bot` handles the conversation's
  computer and rebuild recovery, while setup, sending and wake configuration use the existing
  skills and runtime. Do not fork the protocol for a marketplace or invent an undocumented manifest.
- `plugins/ishoni/assets/icon.png` is the Ishoni mark: two speech bubbles (off-white and orange) on a dark
  rounded square, rendered from `docs/assets/ishoni-icon.svg`. Both plugin manifests and all READMEs use this
  same asset; `docs/assets/ishoni-banner.png` is the README banner built from the same mark. Replace both
  together when the brand changes.

## Commands

```sh
python3 tools/build_plugin.py sync      # regenerate manifests and copies, fix file modes
python3 tools/build_plugin.py check     # no writes: drift, manifest rules, skills, hygiene
python3 tools/build_plugin.py files     # what goes into the plugin archive
python3 tools/build_plugin.py release   # check, then dist/ archives and SHA256SUMS
```

`check` fails when:

- a generated file or bundled copy differs from what `sync` would write, or an executable bit is wrong;
- `plugin.json` leaves the closed Agent Plugins 1.0.0 schema
  (<https://agent-plugins.org/schemas/1.0.0/plugin.schema.json>) or its generated OpenAI extension
  loses required fields. The complete `extensions.com.openai` block replaces the legacy overlay,
  so interface metadata must stay consistent in both; see
  <https://developers.openai.com/plugins/build/plugins>;
- `.codex-plugin/plugin.json` breaks the rules of the Codex plugin-creator validator (fields, semver,
  interface fields, asset paths inside the plugin);
- `.cursor-plugin/plugin.json` or either marketplace file breaks the documented Cursor rules
  (<https://cursor.com/docs/reference/plugins>), or sets component paths that would replace default
  discovery;
- a skill folder lacks `SKILL.md`, its `name` differs from the folder or lacks the `ishoni-` prefix, or
  its description is empty or longer than 1024 characters;
- anything in the plugin is a symlink (including symlinked directories), or looks like runtime state
  or a secret (`config.json`, inbox, outbox, databases, keys, logs, backups);
- the plugin or the repository docs contain a real tailnet name, an absolute home path, a concrete
  Tailscale address, an email address, a UUID, or a token-like string. Maintainers can add personal
  markers in a file outside the repository and point `ISHONI_PRIVATE_DENYLIST` at it (one regular
  expression per line).

## Release archives

`release` writes to `dist/` (ignored by Git):

| File | Contents | Use |
|---|---|---|
| `ishoni-<version>-plugin.zip` | `ishoni/` = the plugin directory | Cursor local folder, `agent --plugin-dir`, manual install, Grok Bot fallback in `/workspace` |
| `ishoni-<version>-marketplace.zip` | `ishoni-marketplace/` = both marketplace files + `plugins/ishoni/` + `LICENSE` | `codex plugin marketplace add <unpacked folder>` |
| `SHA256SUMS` | SHA-256 of both archives | verification |

Archives are reproducible: entries sorted, timestamps fixed to 1980-01-01, modes fixed (0755 for
`scripts/ishoni` and `scripts/ishoni.py`, 0644 otherwise), stored without compression, junk such as
`__pycache__` excluded. The same commit gives the same bytes. Check out with LF line endings
(`core.autocrlf=false`) when comparing hashes on Windows.

Python's `zipfile` does not restore file modes when unpacking; the skills therefore run the launcher
as `sh .../scripts/ishoni` (or `python3 .../scripts/ishoni.py`), which works without the executable bit.

## Tests

```sh
python3 -m unittest discover -s tests -v
python3 -m unittest -v test_codex_bridge test_codex_queue_client test_codex_resident test_install_codex_bridge test_install_codex_bridge_linux
```

`tests/` builds the archives in a temporary directory, unpacks them into a path with spaces outside
the checkout and drives the unpacked `ishoni` command with an isolated `HOME`, a fake `tailscale`
CLI and loopback-only receivers. No test reads or writes `~/.tailmsg`, calls the real Tailscale, uses
port 7766, or contacts another machine. The Codex bridge tests use temporary AF_UNIX sockets and fake `codex` and
`systemctl` executables; they never reach a real app-server or the user's systemd manager.

`tests/test_public_*.py` and `tests/test_friends_*.py` cover the public bots and Friends: a real
gateway, connector, deterministic worker, calendar and resource services and callers as separate processes on
loopback ports, with synthetic homes and data; `tests/support/synthetic_host.py` plays the owner's agent (a
protocol fixture, not an agent). `tests/fixtures/bot_gateway_v1.py` builds real first-version databases for the
migration tests.

`sh selftest.sh` is the upstream end-to-end check. It needs Tailscale on the machine and runs
temporary receivers on the machine's own Tailscale address, ports 7866-7883 (and a stub webhook on
127.0.0.1:7879); confirm those ports are free first. It never uses port 7766 or `~/.tailmsg`. The
older receiver it checks compatibility against is `tests/fixtures/tailmsg_v1_pre_agents.py`, the
protocol v1 release from before @agent addressing. Its runtime files go to `test-run/` (ignored by Git).

## Version bumps

Change `version` in `packaging/product.json` and `PRODUCT_VERSION` in the canonical `tailmsg.py`,
run `sync`, `check`, the relevant tests and `release`, then commit
the regenerated files. Codex reinstalls a local plugin when its version changes. Update the version line of the
plugin README (a test reads it) and add a `CHANGELOG.md` entry. `check` accepts only a SemVer 2.0 version: dot-separated
prerelease identifiers, none empty, numeric ones without leading zeros.

### Publishing a release

Releases are published by GitHub Actions (`.github/workflows/release.yml`), not from a maintainer's machine. Once the
version bump with its `CHANGELOG.md` entry is merged, push an annotated tag `v<version>` on that commit:

```sh
git tag -a v0.7.3 -m "Ishoni 0.7.3" <commit> && git push origin v0.7.3
```

The workflow then

1. refuses the tag unless it equals `v` + the version in `packaging/product.json` and `CHANGELOG.md` has a
   `## <version> (...)` entry;
2. runs `check`, both test suites and `release` on a GitHub-hosted runner;
3. creates the GitHub release `Ishoni <version>` with the entry as its notes and the three `dist/` files attached:
   a stable version is marked latest, a prerelease is marked prerelease and not latest. If the release already exists
   (published by hand), it keeps its notes and flags and only replaces the assets;
4. for a stable version, deploys the tag to the official gateway `https://bots.example.com` (job `deploy-gateway`,
   below). Prereleases are never deployed there.

`Actions → Release → Run workflow` with an existing tag republishes it; untick *deploy* to skip the gateway, or use it
to redeploy (or roll back to) an older stable tag. `.github/workflows/ci.yml` runs `check` and the tests on every pull
request and push to `main`, on GitHub-hosted runners only.

### Deploying the official gateway

`deploy-gateway` runs on a self-hosted runner registered to this repository only, on the gateway's server, with the
label `ishoni-gateway` and its own system account that may do nothing but the deploy below, and waits for approval
on the `bots.example.com` environment (*Review deployments* in the run). It checks out nothing and holds no SSH key or other
secret: it calls `sudo -n /usr/local/sbin/ishoni-gateway-deploy <tag>`, a root-owned copy of
[`deploy/ishoni-gateway-deploy.sh`](../deploy/ishoni-gateway-deploy.sh). That script accepts only a stable tag,
fetches it from GitHub into the gateway's checkout, stops the gateway and its mail relay, copies the database and
`gateway.json` to `backups/pre-<tag>-<time>.*`, checks out the tag, runs `migrate` (a no-op when the schema is
current), starts both services and waits for `/v1/health`. On any failure it restores the copy, checks out the
previous commit and starts the old version again, and the job fails. The job then checks the public `/v1/health`.

A release that needs more than code and `migrate` (a new config key, an nginx limit, a new service) still needs that
change made on the server first; say so in the `CHANGELOG.md` entry.

One-time setup on the server, as root (repeat the `install` whenever `deploy/ishoni-gateway-deploy.sh` changes, since
the runner never installs it):

```sh
install -o root -g root -m 0755 deploy/ishoni-gateway-deploy.sh /usr/local/sbin/ishoni-gateway-deploy
useradd --system --create-home --shell /usr/sbin/nologin gha-ishoni
sed "s/RUNNER_USER/gha-ishoni/" deploy/ishoni-gateway-deploy.sudoers > /etc/sudoers.d/ishoni-gateway-deploy
chmod 0440 /etc/sudoers.d/ishoni-gateway-deploy && visudo -cf /etc/sudoers.d/ishoni-gateway-deploy
```

Then, as `gha-ishoni`, unpack the GitHub Actions runner in its home and register it to this repository with
`./config.sh --unattended --url https://github.com/zealton/ishoni --token <registration token> --name <host>-ishoni
--labels ishoni-gateway` (the token comes from `gh api -X POST repos/zealton/ishoni/actions/runners/registration-token`),
and install it as a service with `./svc.sh install gha-ishoni && ./svc.sh start` (as root, from that directory).

Paths, the service user and the health URL can be overridden in a root-owned `/etc/default/ishoni-gateway-deploy`.
In GitHub, the repository needs the `bots.example.com` environment with its maintainer as required reviewer and
deployments limited to `v*` tags, and *Settings → Actions → Fork pull request workflows* must require approval for
all outside collaborators, so that no fork's pull request can point a job at the self-hosted runner.

### The official gateway's server

`bots.example.com` shares its machine with other services. Besides the deploy script, `deploy/` holds the rest of
its setup, so that a change to it is reviewed like code. Each needs root on the server and a maintainer's approval;
none is touched by a deploy.

- `deploy/ishoni-gateway-capacity.conf`: a systemd drop-in for `ishoni-gateway.service` (CPU weight, threads, open
  files, memory ceiling). Install it as `/etc/systemd/system/ishoni-gateway.service.d/capacity.conf`,
  `systemctl daemon-reload`; it applies at the next restart. `systemctl show ishoni-gateway -p
  CPUWeight,TasksMax,LimitNOFILE,MemoryHigh,MemoryMax` shows the values in force.
- nginx: every held connector poll keeps two connections (Cloudflare to nginx, nginx to the gateway). In
  `/etc/nginx/nginx.conf`, `worker_rlimit_nofile 32768;` at the top level and `worker_connections 8192;` in
  `events` (the Ubuntu default is 768 per worker). In the `bots.example.com` site, its own access log without the
  successful connector polls, which would otherwise be most lines once thousands of agents are online:

  ```nginx
  map "$request_method $uri $status" $ishoni_bots_log {   # at the top of the site file, outside `server`
      "GET /v1/connector/jobs 200" 0;
      default 1;
  }
  # in the 443 server block:
  access_log /var/log/nginx/bots.example.com.access.log combined buffer=64k flush=5s if=$ishoni_bots_log;
  ```

  Check with `nginx -t`, apply with `systemctl reload nginx` (no connection is dropped).
- Litestream: `deploy/ishoni-litestream.service` and `deploy/ishoni-litestream.yml` replicate `gateway.sqlite3` to
  the blob store's R2 bucket under `litestream/gateway`, with the blob store's key. This copies the entire database
  and transaction history, including metadata, stored message bodies and transient verification email addresses
  and codes. Publish the [privacy notice](PRIVACY.md#the-official-gateway-botsexamplecom) describing these remote
  copies before enabling replication. The configuration takes daily snapshots with 7-day retention; Litestream
  [removes expired backups when it makes snapshots](https://litestream.io/docs/troubleshooting/), normally within
  roughly 8 days. The bucket's 35-day object expiry also applies if that cleanup stops. Install the `litestream` binary
  from its GitHub release as `/usr/local/bin/litestream`, the files as `/etc/systemd/system/ishoni-litestream.service`
  and `/etc/ishoni-litestream.yml`, and write `/etc/ishoni-litestream.env` (root, 0600) with `ISHONI_R2_ENDPOINT`,
  `LITESTREAM_ACCESS_KEY_ID` and `LITESTREAM_SECRET_ACCESS_KEY` taken from `gw/blobs.json` and `gw/r2.secret`. Add
  `"external_checkpoints": true` to `gateway.json` (a gateway older than 0.7.10 refuses it), then `systemctl
  daemon-reload && systemctl enable ishoni-litestream && systemctl restart ishoni-gateway`: Litestream starts and
  stops with the gateway. Rehearse a restore now and then, as root:
  `set -a; . /etc/ishoni-litestream.env; runuser -u ishoni-gw -- litestream restore -config /etc/ishoni-litestream.yml
  -o /srv/ishoni-gateway/restore-test.sqlite3 /srv/ishoni-gateway/gw/gateway.sqlite3`, then check the copy with
  `PRAGMA integrity_check` and delete it. To undo: `systemctl disable --now ishoni-litestream`, remove the key from
  `gateway.json` and restart the gateway.

### Prereleases

A prerelease such as `0.6.0-beta.1` is published so that no stable installation picks it up by itself:

- Point `codex.privacyPolicyURL` in `packaging/product.json` into the prerelease's own tag
  (`.../blob/v<version>/docs/PRIVACY.md`); `check` refuses a prerelease that links another branch's or tag's notice.
- Publish a release branch and the tag `v<version>` only. Keep the default branch on the stable release: the
  repository marketplace (`codex plugin marketplace add|upgrade`) and plugin pages install what the default branch
  holds, whatever GitHub release flags say.
- Mark the GitHub release as a prerelease and not as the latest release, and attach the `release` output
  (`ishoni-<version>-plugin.zip`, `ishoni-<version>-marketplace.zip`, `SHA256SUMS`).
- The stable update channel never offers it; `update check --channel preview` names its tag, archive and checksum
  file. Announce it with `update notify --channel preview`; its notices carry `prerelease: true` and ask the receiving
  owner to opt in.
- In the `CHANGELOG.md` entry, link files that exist only in the prerelease by absolute URLs pinned to its tag, so the
  same entry also reads correctly on the default branch.

## Marketplace submissions

Use [MARKETPLACE_SUBMISSION.md](MARKETPLACE_SUBMISSION.md) for current official portals, required
listing fields and the actual submission state. The Grok Bot/Cursor form accepts the public
repository. OpenAI's skills-only desktop route uses the same package with
`requires_local_executor: true`; it does not require inventing a hosted MCP service.

Before submission, verify the exact package, its [privacy notice](PRIVACY.md), reference files and
the intended host's native installation and skill invocation. Generic CLI tests or an existing
receiver are not substitutes for a required in-app plugin test. Keep test evidence sanitized:
no home addresses, tailnet domains, session IDs, account details or webhook secrets. Submit only
statements supported by completed tests. Store a receipt or status returned by the official portal;
mark public availability only after a listing or publication is actually confirmed.

## Renaming

The product id is defined once in `packaging/product.json`. A rename means: update `id`,
`displayName`, `pluginDir`, `skillPrefix` and the marketplace names there; `git mv` the plugin folder
and the skill folders (and their `name:` lines); rename `scripts/ishoni*` and the `PRODUCT`,
`DISPLAY` and `ISHONI_PYTHON` constants in `scripts/ishoni.py`; update the docs; run `sync` and `check`.
Keep the tailmsg protocol, `tailmsg.py`, `~/.tailmsg`, `TAILMSG_*` variables and service names, so
deployed machines need no change.
