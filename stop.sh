#!/usr/bin/env bash
# Gracefully stop Agent Collective + ngrok
SESSION="agentcollective"
LOG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/logs/service"
PID_FILE="$LOG_DIR/pids"

YELLOW='\033[1;33m'; GREEN='\033[0;32m'; DIM='\033[2m'; NC='\033[0m'

echo -e "${YELLOW}  Stopping Agent Collective…${NC}"

# Kill tracked PIDs first (graceful SIGTERM to run.py → triggers --snapshot if set)
if [[ -f "$PID_FILE" ]]; then
  while IFS= read -r pid; do
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null
      echo -e "${DIM}  sent SIGTERM to $pid${NC}"
      # Wait up to 5s for clean exit
      for i in $(seq 1 5); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
      done
      # Force if still alive
      kill -9 "$pid" 2>/dev/null || true
    fi
  done < "$PID_FILE"
  rm -f "$PID_FILE"
fi

# Kill the tmux session (takes care of anything still inside)
if tmux has-session -t "$SESSION" 2>/dev/null; then
  tmux kill-session -t "$SESSION"
  echo -e "${DIM}  tmux session '$SESSION' killed${NC}"
fi

# Clean up public URL file
rm -f "$LOG_DIR/public_url.txt"

echo -e "${GREEN}  Stopped.${NC}"
