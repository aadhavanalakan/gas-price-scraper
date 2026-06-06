#!/usr/bin/env bash
#
# share.sh — launch the Gas Price Scraper app AND a public link, in one command.
#
#   ./share.sh
#
# Starts the Streamlit app locally, opens a free Cloudflare tunnel to it, and
# prints the public https://….trycloudflare.com URL you can share. Everything
# runs on THIS machine's IP (so GasBuddy won't block it). Press Ctrl+C to stop
# both the app and the tunnel.

cd "$(dirname "$0")" || exit 1

PORT=8501
CF="$HOME/.local/bin/cloudflared"
[ -x "$CF" ] || CF="$(command -v cloudflared 2>/dev/null)"

if [ -z "$CF" ]; then
  echo "❌ cloudflared not found."
  echo "   Install it (Apple Silicon):"
  echo "   curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-arm64.tgz | tar -xz -C ~/.local/bin/"
  exit 1
fi

cleanup() { echo; echo "Shutting down…"; kill "$APP_PID" "$CF_PID" 2>/dev/null; }
trap cleanup EXIT INT TERM

echo "▶ Starting the app…"
python3 -m streamlit run app.py --server.headless true --server.port "$PORT" \
  >/tmp/gps_app.log 2>&1 &
APP_PID=$!

# wait until the app answers
for _ in $(seq 1 40); do
  curl -s -o /dev/null "http://localhost:$PORT/healthz" && break
  sleep 1
done

echo "▶ Opening public tunnel…"
"$CF" tunnel --url "http://localhost:$PORT" >/tmp/gps_tunnel.log 2>&1 &
CF_PID=$!

# grab the public URL it prints
URL=""
for _ in $(seq 1 40); do
  URL=$(grep -Eo "https://[a-z0-9-]+\.trycloudflare\.com" /tmp/gps_tunnel.log | head -1)
  [ -n "$URL" ] && break
  sleep 1
done

echo
echo "============================================================"
echo "   ⛽  Gas Price Scraper is live"
echo
echo "   Local:  http://localhost:$PORT"
[ -n "$URL" ] && echo "   Share:  $URL" \
              || echo "   (tunnel URL not detected — see /tmp/gps_tunnel.log)"
echo "============================================================"
echo "   Press Ctrl+C to stop the app and the tunnel."
echo

wait
