#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
if [ -f .env ]; then set -a; . ./.env; set +a; fi
WEB=${VIX_WEB_PORT:-47530}
MCP=${VIX_MCP_PORT:-47531}
mkdir -p logs
for p in "$WEB" "$MCP"; do
  pids=$(lsof -ti tcp:"$p" -sTCP:LISTEN || true)
  if [ -n "$pids" ]; then kill $pids || true; fi
done
sleep 1
source .venv/bin/activate
python -m vix.bootstrap
nohup python -m vix.mcp_server > logs/mcp.log 2>&1 &
nohup python -m uvicorn vix.app:app --host 127.0.0.1 --port "$WEB" > logs/web.log 2>&1 &
echo "Starting vix ..."
ok=0
for i in $(seq 1 30); do
  if curl -fs "http://127.0.0.1:$WEB/api/info" > /dev/null; then ok=1; break; fi
  if ! pgrep -f "uvicorn vix.app:app" > /dev/null; then break; fi
  sleep 1
done
if [ "$ok" = 1 ]; then
  echo "Web  : http://127.0.0.1:$WEB"
else
  echo "Web failed to start. Last lines of logs/web.log:"
  tail -25 logs/web.log
fi
echo "MCP  : http://127.0.0.1:$MCP/mcp  (log: logs/mcp.log)"
