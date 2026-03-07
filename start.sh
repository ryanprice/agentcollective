#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────
#  Agent Collective — service launcher
#  Starts: agentcollective (FastAPI) + ngrok tunnel
#  Usage:  ./start.sh [--snapshot] [--agents qwen,llama]
#  Ctrl+C: gracefully stops both
# ─────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"
PORT=8000
LOG_DIR="$SCRIPT_DIR/logs/service"
mkdir -p "$LOG_DIR"

AC_LOG="$LOG_DIR/agentcollective.log"
NGROK_LOG="$LOG_DIR/ngrok.log"
PID_FILE="$LOG_DIR/pids"

# ── Colours ───────────────────────────────────────────────────────
PURPLE='\033[0;35m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
DIM='\033[2m'
BOLD='\033[1m'
NC='\033[0m'

banner() {
  echo ""
  echo -e "${PURPLE}${BOLD}  ⬡  AGENT COLLECTIVE${NC}"
  echo -e "${DIM}  autonomous multi-agent intelligence system${NC}"
  echo ""
}

# ── Cleanup on exit ───────────────────────────────────────────────
cleanup() {
  echo ""
  echo -e "${YELLOW}  Shutting down…${NC}"

  if [[ -f "$PID_FILE" ]]; then
    while IFS= read -r pid; do
      if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null && echo -e "${DIM}  killed pid $pid${NC}"
      fi
    done < "$PID_FILE"
    rm -f "$PID_FILE"
  fi

  echo -e "${GREEN}  Stopped.${NC}"
  echo ""
}
trap cleanup EXIT INT TERM

# ── Check venv ────────────────────────────────────────────────────
if [[ ! -f "$VENV/bin/activate" ]]; then
  echo -e "${RED}  ✗ No .venv found at $VENV${NC}"
  echo -e "  Run: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
  exit 1
fi

# ── Check ngrok ───────────────────────────────────────────────────
if ! snap run ngrok version &>/dev/null; then
  echo -e "${RED}  ✗ ngrok not found (snap run ngrok)${NC}"
  exit 1
fi

banner

# ── Start Agent Collective ────────────────────────────────────────
echo -e "${CYAN}  Starting Agent Collective on :${PORT}…${NC}"
source "$VENV/bin/activate"

# Pass through any CLI flags (--snapshot, --agents, etc.)
python "$SCRIPT_DIR/run.py" "$@" \
  >> "$AC_LOG" 2>&1 &
AC_PID=$!
echo "$AC_PID" > "$PID_FILE"
echo -e "${DIM}  agentcollective pid: $AC_PID  →  $AC_LOG${NC}"

# Wait for FastAPI to be ready
echo -ne "${DIM}  Waiting for API"
for i in $(seq 1 30); do
  if curl -sf "http://localhost:$PORT/status" &>/dev/null; then
    echo -e " ready${NC}"
    break
  fi
  echo -n "."
  sleep 1
  if ! kill -0 $AC_PID 2>/dev/null; then
    echo -e "\n${RED}  ✗ agentcollective crashed — check $AC_LOG${NC}"
    exit 1
  fi
done

# ── Start ngrok ───────────────────────────────────────────────────
echo -e "${CYAN}  Starting ngrok tunnel…${NC}"
snap run ngrok http $PORT \
  --log=stdout \
  --log-format=json \
  >> "$NGROK_LOG" 2>&1 &
NGROK_PID=$!
echo "$NGROK_PID" >> "$PID_FILE"
echo -e "${DIM}  ngrok pid: $NGROK_PID  →  $NGROK_LOG${NC}"

# Poll ngrok local API for the public URL
PUBLIC_URL=""
echo -ne "${DIM}  Waiting for tunnel"
for i in $(seq 1 20); do
  sleep 1
  echo -n "."
  TUNNEL_JSON=$(curl -sf http://localhost:4040/api/tunnels 2>/dev/null || true)
  if [[ -n "$TUNNEL_JSON" ]]; then
    PUBLIC_URL=$(echo "$TUNNEL_JSON" \
      | python3 -c "
import sys, json
data = json.load(sys.stdin)
tunnels = data.get('tunnels', [])
for t in tunnels:
    if t.get('proto') == 'https':
        print(t['public_url'])
        break
" 2>/dev/null || true)
    if [[ -n "$PUBLIC_URL" ]]; then
      echo -e " ready${NC}"
      break
    fi
  fi
  if ! kill -0 $NGROK_PID 2>/dev/null; then
    echo -e "\n${YELLOW}  ⚠ ngrok exited — running local-only${NC}"
    break
  fi
done

# ── Print summary ─────────────────────────────────────────────────
echo ""
echo -e "${BOLD}  ┌─────────────────────────────────────────────┐${NC}"
echo -e "${BOLD}  │  Agent Collective is running                │${NC}"
echo -e "${BOLD}  ├─────────────────────────────────────────────┤${NC}"
echo -e "  │  ${GREEN}Local${NC}   http://localhost:${PORT}               │"
echo -e "  │  ${GREEN}Mobile${NC}  http://localhost:${PORT}/mobile         │"
if [[ -n "$PUBLIC_URL" ]]; then
echo -e "  │                                             │"
echo -e "  │  ${PURPLE}Public${NC}  ${BOLD}${PUBLIC_URL}${NC}"
echo -e "  │  ${PURPLE}Mobile${NC}  ${BOLD}${PUBLIC_URL}/mobile${NC}"
fi
echo -e "  │                                             │"
echo -e "  │  ${DIM}Logs    $LOG_DIR${NC}"
echo -e "${BOLD}  └─────────────────────────────────────────────┘${NC}"
echo ""
echo -e "${DIM}  Ctrl+C to stop both services${NC}"
echo ""

# ── Wait — keep alive until Ctrl+C ───────────────────────────────
wait $AC_PID
