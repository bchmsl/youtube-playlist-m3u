FROM denoland/deno:bin-2.7.5 AS deno
FROM brainicism/bgutil-ytdlp-pot-provider:2.0.0 AS pot
FROM tailscale/tailscale:stable AS tailscale
FROM python:3.12-slim-bookworm
COPY --from=deno /deno /usr/local/bin/deno
COPY --from=pot /usr/local/bin/node /usr/local/bin/node
COPY --from=pot /app /opt/bgutil
COPY --from=tailscale /usr/local/bin/tailscale /usr/local/bin/tailscale
COPY --from=tailscale /usr/local/bin/tailscaled /usr/local/bin/tailscaled
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000 BGUTIL_SERVER_HOME=/opt/bgutil
WORKDIR /app
COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 libatomic1 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 app
COPY app ./app
COPY start.sh ./start.sh
RUN chmod 755 ./start.sh
USER app
RUN node /opt/bgutil/build/generate_once.js --help >/dev/null \
    && deno --version >/dev/null
EXPOSE 8000
CMD ["./start.sh"]
