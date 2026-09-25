FROM denoland/deno:bin-2.7.5 AS deno
FROM brainicism/bgutil-ytdlp-pot-provider:2.0.0 AS pot
FROM python:3.12-slim-bookworm
COPY --from=deno /deno /usr/local/bin/deno
COPY --from=pot /usr/local/bin/node /usr/local/bin/node
COPY --from=pot /app /opt/bgutil
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000 BGUTIL_SERVER_HOME=/opt/bgutil
WORKDIR /app
COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 app
COPY app ./app
USER app
RUN node /opt/bgutil/build/generate_once.js --help >/dev/null \
    && deno --version >/dev/null
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*' --limit-concurrency 64"]
