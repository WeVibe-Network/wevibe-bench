#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# REDEPLOY — rebuild the dashboard image and restart the container.
#
#   ./redeploy.sh              rebuild + restart + verify
#   ./redeploy.sh --control    also restart the host-side control plane
#
# WHY THIS EXISTS, AND WHY `docker compose restart` IS THE WRONG COMMAND:
#
# The dashboard's UI files are COPY'd INTO the image (Dockerfile), not
# bind-mounted — only `/bench` (the run artifacts) is mounted, read-only. So a
# `restart` re-runs the SAME image and serves the OLD board, silently. Editing
# board.js and restarting looks like it worked and changes nothing on screen.
#
# `up -d --build` is therefore mandatory after any edit to index.html, board.js,
# panels/ or sources/. This script exists so that is never forgotten.
#
# It then VERIFIES the deploy by fetching the served bytes back, because
# "the build succeeded" and "the browser is being served the new file" are two
# different claims and only the second one matters.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH="$(cd "$HERE/.." && pwd)"
PORT="${WEVIBE_DASH_PORT:-7717}"
CONTROL_PORT="${WEVIBE_CONTROL_PORT:-7718}"

cd "$HERE"

echo "── rebuilding dashboard image ─────────────────────────────────────"
docker compose up -d --build 2>&1 | grep -Ev '^#' | tail -6

echo
echo "── waiting for the board to assemble ──────────────────────────────"
for i in $(seq 1 20); do
  if curl -fsS -m 2 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
    echo "  health ok after ${i} attempt(s)"
    break
  fi
  sleep 1
  if [ "$i" = 20 ]; then
    echo "  FAILED: /api/health never came up on :${PORT}"
    docker compose logs --tail 30 wevibe-bench-dashboard
    exit 1
  fi
done

# Verify the SERVED bytes, not the build log. This is the check that catches a
# stale image being restarted instead of rebuilt.
echo
echo "── verifying served content ───────────────────────────────────────"
BRAND="$(curl -fsS -m 3 "http://127.0.0.1:${PORT}/panels/chrome.js" \
  | grep -o 'class="brand">[^<]*<u>[^<]*</u>[^<]*' || true)"
if [ -n "$BRAND" ]; then
  echo "  brand served: ${BRAND#class=\"brand\">}"
else
  echo "  WARNING: brand string not found in the served chrome.js"
fi

echo
echo "── source health ──────────────────────────────────────────────────"
curl -fsS -m 5 "http://127.0.0.1:${PORT}/api/board" | python3 -c "
import json,sys
d=json.load(sys.stdin)
for s in d.get('sources',[]):
    mark='ok ' if s['ok'] else 'UNWIRED'
    reason='' if s['ok'] else f\"  :: {(s.get('reason') or '')[:70]}\"
    print(f\"  {mark:8s} {s['id']}{reason}\")
"

if [ "${1:-}" = "--control" ]; then
  echo
  echo "── restarting control plane ───────────────────────────────────────"
  # KILL WHATEVER HOLDS THE PORT, by asking the kernel who holds it.
  #
  # Two argv patterns have now failed here. "control/server.mjs" missed a
  # process started from inside control/; "server.mjs --port 7718" missed one
  # started WITHOUT the flag (`node control/server.mjs`, the default port). Both
  # failures are identical and silent: the old process survives, the new one
  # dies on EADDRINUSE, and the health check below passes AGAINST THE STALE
  # PROCESS — so redeploy reports success while serving pre-edit code.
  #
  # `lsof -ti` is the only check that cannot drift from how the process was
  # launched, because the question it answers is the one that actually matters:
  # who is listening on this port.
  holders="$(lsof -ti "tcp@127.0.0.1:${CONTROL_PORT}" -sTCP:LISTEN 2>/dev/null || true)"
  [ -n "$holders" ] && kill $holders 2>/dev/null || true
  for i in $(seq 1 10); do
    lsof -ti "tcp@127.0.0.1:${CONTROL_PORT}" -sTCP:LISTEN >/dev/null 2>&1 || break
    sleep 1
  done
  # Still held after SIGTERM: escalate once, then verify again.
  if lsof -ti "tcp@127.0.0.1:${CONTROL_PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
    kill -9 $(lsof -ti "tcp@127.0.0.1:${CONTROL_PORT}" -sTCP:LISTEN) 2>/dev/null || true
    sleep 1
  fi
  if lsof -ti "tcp@127.0.0.1:${CONTROL_PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "  FAILED: a control plane on :${CONTROL_PORT} would not die — refusing to"
    echo "          start a second one that would serve stale code."
    exit 1
  fi

  cd "$BENCH/control"
  # `< /dev/null` is mandatory: without it the shell suspends the job the
  # instant the process touches stdin.
  nohup node server.mjs --port "${CONTROL_PORT}" > /tmp/wevibe-control-plane.log 2>&1 < /dev/null &
  control_pid=$!
  disown || true
  sleep 3
  # THE PID WE STARTED MUST BE THE ONE ANSWERING. A bare health check is not
  # enough — it is exactly what passed against the stale process last time.
  if ! kill -0 "$control_pid" 2>/dev/null; then
    echo "  FAILED: the control plane exited immediately (likely EADDRINUSE)"
    tail -20 /tmp/wevibe-control-plane.log || true
    exit 1
  fi
  if curl -fsS -m 3 "http://127.0.0.1:${CONTROL_PORT}/api/health" >/dev/null 2>&1; then
    echo "  control plane up on :${CONTROL_PORT}  pid ${control_pid}  (log: /tmp/wevibe-control-plane.log)"
  else
    echo "  FAILED: control plane did not answer on :${CONTROL_PORT}"
    tail -20 /tmp/wevibe-control-plane.log || true
    exit 1
  fi
fi

echo
echo "→ http://localhost:${PORT}"
