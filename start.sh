#!/bin/sh
set -eu

if [ -n "${TS_AUTHKEY:-}" ] || [ -n "${TS_EXIT_NODE:-}" ]; then
    if [ -z "${TS_AUTHKEY:-}" ] || [ -z "${TS_EXIT_NODE:-}" ]; then
        echo "TS_AUTHKEY and TS_EXIT_NODE must both be configured" >&2
        exit 1
    fi

    ts_socket=/tmp/tailscaled.sock
    ts_log=/tmp/tailscaled.log
    tailscaled \
        --tun=userspace-networking \
        --socks5-server=127.0.0.1:1055 \
        --outbound-http-proxy-listen=127.0.0.1:1055 \
        --state=mem: \
        --socket="$ts_socket" >"$ts_log" 2>&1 &
    ts_pid=$!

    attempts=0
    while [ ! -S "$ts_socket" ] && [ "$attempts" -lt 20 ]; do
        if ! kill -0 "$ts_pid" 2>/dev/null; then
            cat "$ts_log" >&2
            exit 1
        fi
        attempts=$((attempts + 1))
        sleep 1
    done
    if [ ! -S "$ts_socket" ]; then
        echo "Tailscale daemon did not become ready" >&2
        cat "$ts_log" >&2
        exit 1
    fi

    tailscale --socket="$ts_socket" up \
        --auth-key="$TS_AUTHKEY" \
        --hostname="${TS_HOSTNAME:-render-youtube-m3u}" \
        --accept-dns=false \
        --exit-node="$TS_EXIT_NODE"
    export YOUTUBE_PROXY=http://127.0.0.1:1055
    echo "Tailscale YouTube egress enabled through the configured exit node"
fi

exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --proxy-headers \
    --forwarded-allow-ips='*' \
    --limit-concurrency 64
