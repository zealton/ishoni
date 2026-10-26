# A team Friends gateway on Railway (or any container host with a persistent volume): one long-running
# `ishoni gateway serve` with its SQLite database on the volume, optionally replicated by Litestream.
# Build context: the repository root (`railway.json` next to this file points Railway at it).
FROM python:3.12-slim

ARG LITESTREAM_VERSION=0.3.13
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSL "https://github.com/benbjohnson/litestream/releases/download/v${LITESTREAM_VERSION}/litestream-v${LITESTREAM_VERSION}-linux-amd64.tar.gz" \
       | tar -xz -C /usr/local/bin litestream \
    && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

RUN useradd --system --create-home --uid 10001 ishoni
WORKDIR /app
COPY public_bots/ /app/public_bots/
COPY deploy/railway/entrypoint.sh /app/entrypoint.sh
COPY deploy/railway/litestream.yml /app/litestream.yml
RUN chmod 0755 /app/entrypoint.sh

# The platform's volume mounted at /data holds gateway.json, gateway.sqlite3, the gateway's own key files and blobs.
# No VOLUME instruction: Railway rejects a Dockerfile that declares one ("use Railway Volumes"); other hosts mount /data
# with their own `-v` or volume setting (see the local check in docs/DEPLOY_RAILWAY.md).
ENV ISHONI_GATEWAY_HOME=/data/gw
EXPOSE 7780
# No USER here: the entrypoint starts as root only to hand the volume to `ishoni`, then re-executes itself as that user.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python3 -c "import json,os,urllib.request;p=os.environ.get('PORT','7780');json.load(urllib.request.urlopen('http://127.0.0.1:%s/v1/health'%p,timeout=4))" || exit 1
ENTRYPOINT ["/app/entrypoint.sh"]
