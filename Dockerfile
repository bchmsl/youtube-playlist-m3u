FROM denoland/deno:bin-2.7.5 AS deno
FROM tailscale/tailscale:stable AS tailscale
FROM python:3.12-slim-bookworm
COPY --from=deno /deno /usr/local/bin/deno
COPY --from=tailscale /usr/local/bin/tailscale /usr/local/bin/tailscale
COPY --from=tailscale /usr/local/bin/tailscaled /usr/local/bin/tailscaled
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000 \
    DENO_NO_PROMPT=1 DENO_NO_UPDATE_CHECK=1
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
RUN deno --version >/dev/null
EXPOSE 8000
CMD ["./start.sh"]
