#!/bin/sh
# remote-agent-gate: forced-command gate for the panel key.
# Only the exact commands the panel / sync / poller use are allowed.
# Anything else (including an interactive shell) is rejected.
# stdin is passed through to the allowed command.
if [ "$SSH_ORIGINAL_COMMAND" = "python3 /usr/local/bin/remote-agent.py read" ]; then
  exec python3 /usr/local/bin/remote-agent.py read
elif [ "$SSH_ORIGINAL_COMMAND" = "python3 /usr/local/bin/remote-agent.py rotate vless" ]; then
  exec python3 /usr/local/bin/remote-agent.py rotate vless
elif [ "$SSH_ORIGINAL_COMMAND" = "python3 /usr/local/bin/remote-agent.py rotate hy2" ]; then
  exec python3 /usr/local/bin/remote-agent.py rotate hy2
elif [ "$SSH_ORIGINAL_COMMAND" = "python3 /usr/local/bin/remote-agent.py hystats" ]; then
  exec python3 /usr/local/bin/remote-agent.py hystats
elif [ "$SSH_ORIGINAL_COMMAND" = "python3 /usr/local/bin/hy2_auth_sync.py" ]; then
  exec python3 /usr/local/bin/hy2_auth_sync.py
elif [ "$SSH_ORIGINAL_COMMAND" = "python3 /usr/local/bin/remote-agent.py vless-sync" ]; then
  exec python3 /usr/local/bin/remote-agent.py vless-sync
elif [ "$SSH_ORIGINAL_COMMAND" = "python3 /usr/local/bin/remote-agent.py vless-stats" ]; then
  exec python3 /usr/local/bin/remote-agent.py vless-stats
elif [ "$SSH_ORIGINAL_COMMAND" = "python3 /usr/local/bin/remote-agent.py hy2-online" ]; then
  exec python3 /usr/local/bin/remote-agent.py hy2-online
else
  case "$SSH_ORIGINAL_COMMAND" in
    "python3 /usr/local/bin/remote-agent.py hy2-hop "*)
      exec python3 /usr/local/bin/remote-agent.py hy2-hop "${SSH_ORIGINAL_COMMAND#python3 /usr/local/bin/remote-agent.py hy2-hop }"
      ;;
  esac
  echo "remote-agent-gate: command not allowed" >&2
  exit 1
fi
