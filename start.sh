#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────
#  Agent Collective — service launcher
#  Runs inside a tmux session so it survives SSH disconnection.
#
#  Usage:
#    ./start.sh                     # start (or re-attach if already running)
#    ./start.sh --snapshot          # pass flags through to run.py
#    ./start.sh --agents qwen,llama
#
#  Other commands:
#    ./attach.sh   — re-attach to the running session
#    ./stop.sh     — gracefully stop everything
# ─────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SESSION="agentcollective"

# ── Colours ───────────────────────────────────────────────────────
PURPLE='\033[0;35m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
RED='\033[0;31m'; DIM='\033[2m'; BOLD='\033[1m'; NC='\033[0m'

# ── Check tmux ────────────────────────────────────────────────────
if ! command -v tmux &>/dev/null; then
  echo -e "${RED}  ✗ tmux not found. Install it: sudo apt install tmux${NC}"
  exit 1
fi

# ── Already running? ──────────────────────────────────────────────
if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo -e "${YELLOW}  ⚠ Session '$SESSION' is already running.${NC}"
  echo -e "  Attaching… (Ctrl+B then D to detach without stopping)\n"
  tmux attach-session -t "$SESSION"
  exit 0
fi

# ── Build the inner script that tmux will run ─────────────────────
# We write the actual work to a temp script so tmux executes it cleanly
INNER="$SCRIPT_DIR/logs/service/_inner.sh"
mkdir -p "$SCRIPT_DIR/logs/service"

# Capture any CLI flags to pass through to run.py
PASSTHROUGH_ARGS="${*}"

cat > "$INNER" << INNEREOF
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$SCRIPT_DIR"
PORT=8000
LOG_DIR="\$SCRIPT_DIR/logs/service"
AC_LOG="\$LOG_DIR/agentcollective.log"
PID_FILE="\$LOG_DIR/pids"
VENV="\$SCRIPT_DIR/.venv"

PURPLE='\033[0;35m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'
YELLOW='\033[1;33m'; RED='\033[0;31m'; DIM='\033[2m'
BOLD='\033[1m'; NC='\033[0m'

cleanup() {
  echo ""
  echo -e "\${YELLOW}  Shutting down…\${NC}"
  if [[ -f "\$PID_FILE" ]]; then
    while IFS= read -r pid; do
      kill -0 "\$pid" 2>/dev/null && kill "\$pid" 2>/dev/null && echo -e "\${DIM}  killed \$pid\${NC}"
    done < "\$PID_FILE"
    rm -f "\$PID_FILE"
  fi
  echo -e "\${GREEN}  Stopped.\${NC}"
}
trap cleanup EXIT INT TERM

echo ""
echo -e "\${PURPLE}\${BOLD}  ⬡  AGENT COLLECTIVE\${NC}"
echo -e "\${DIM}  autonomous multi-agent intelligence system\${NC}"
echo ""

# ── Check venv ────────────────────────────────────────────────────
if [[ ! -f "\$VENV/bin/activate" ]]; then
  echo -e "\${RED}  ✗ No .venv at \$VENV\${NC}"
  echo "  Run: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
  exit 1
fi
source "\$VENV/bin/activate"

# ── Start Agent Collective ────────────────────────────────────────
echo -e "\${CYAN}  Starting Agent Collective on :\${PORT}…\${NC}"
python "\$SCRIPT_DIR/run.py" $PASSTHROUGH_ARGS >> "\$AC_LOG" 2>&1 &
AC_PID=\$!
echo "\$AC_PID" > "\$PID_FILE"
echo -e "\${DIM}  pid \$AC_PID  →  \$AC_LOG\${NC}"

# Wait for FastAPI to be ready
echo -ne "\${DIM}  Waiting for API"
for i in \$(seq 1 30); do
  if curl -sf "http://localhost:\$PORT/status" &>/dev/null; then
    echo -e " ready\${NC}"; break
  fi
  echo -n "."; sleep 1
  if ! kill -0 \$AC_PID 2>/dev/null; then
    echo -e "\n\${RED}  ✗ Crashed — tail \$AC_LOG\${NC}"; exit 1
  fi
done

# ── Summary ───────────────────────────────────────────────────────
echo ""
echo -e "\${BOLD}  ┌──────────────────────────────────────────────────┐\${NC}"
echo -e "\${BOLD}  │  Agent Collective is running                     │\${NC}"
echo -e "\${BOLD}  ├──────────────────────────────────────────────────┤\${NC}"
echo -e "  │  \${GREEN}Local\${NC}    http://localhost:\${PORT}                  │"
echo -e "  │  \${GREEN}Mobile\${NC}   http://localhost:\${PORT}/mobile            │"
echo -e "  │                                                  │"
echo -e "  │  \${DIM}Ctrl+B then D to detach (keeps running)\${NC}        │"
echo -e "  │  \${DIM}./stop.sh to stop   ./attach.sh to return\${NC}       │"
echo -e "\${BOLD}  └──────────────────────────────────────────────────┘\${NC}"
echo ""

# Keep alive
wait \$AC_PID
INNEREOF

chmod +x "$INNER"

# ── Launch tmux session ───────────────────────────────────────────
echo -e "${PURPLE}${BOLD}  Launching in tmux session '${SESSION}'…${NC}"
tmux new-session -d -s "$SESSION" -x 220 -y 50 "bash $INNER"

# Give it a moment to boot then attach
sleep 1
echo -e "${GREEN}  Attaching… (Ctrl+B then D to detach without stopping)${NC}\n"
tmux attach-session -t "$SESSION"
