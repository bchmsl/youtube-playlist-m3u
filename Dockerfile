FROM denoland/deno:bin-2.7.5 AS deno
FROM python:3.12-slim
COPY --from=deno /deno /usr/local/bin/deno
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home --uid 10001 app
COPY app ./app
USER app
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*' --limit-concurrency 64"]
