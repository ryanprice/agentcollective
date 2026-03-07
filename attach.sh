#!/usr/bin/env bash
# Re-attach to the running Agent Collective tmux session
SESSION="agentcollective"
if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "Attaching to '$SESSION'… (Ctrl+B then D to detach)"
  tmux attach-session -t "$SESSION"
else
  echo "Session '$SESSION' is not running. Use ./start.sh to launch."
  exit 1
fi
