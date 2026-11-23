#!/bin/sh
# remote-agent-gate: forced-command gate for the panel key.
# Only the exact commands the panel / sync / poller use are allowed.
# Anything else (including an interactive shell) is rejected.
# stdin is passed through to the allowed command.
# NOTE: there is no "rotate hy2" here on purpose — both servers use HTTP
# auth since V1.1.3, so the agent's old userpass-based rotate_hy2() was
# deleted; admin HY2 rotation goes through the API's rotate_admin_hy2().
if [ "$SSH_ORIGINAL_COMMAND" = "python3 /usr/local/bin/remote-agent.py read" ]; then
  exec python3 /usr/local/bin/remote-agent.py read
elif [ "$SSH_ORIGINAL_COMMAND" = "python3 /usr/local/bin/remote-agent.py rotate vless" ]; then
  exec python3 /usr/local/bin/remote-agent.py rotate vless
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
  echo "remote-agent-gate: command not allowed" >&2
  exit 1
fi
