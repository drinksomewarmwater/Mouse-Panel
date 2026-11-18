#!/usr/bin/env python3
"""Switch Hysteria 2 auth to HTTP (hy2-auth service) and keep its user file fresh.

Usage: hy2_auth_sync.py [--ensure-stats] < users.json
users.json: {"users": {"username": {"password": "...", "expires_at": int|null,
"ip_limit": int}, ...}} (enabled users only). A plain string value is
also accepted as just the password (expires_at null, ip_limit 0).
- Writes /var/lib/mouse-panel/hy2-users.json (0600) for the hy2-auth
  service, which enforces per-user expiry + concurrent-IP limits.
- Replaces the whole `auth:` block in /etc/hysteria/config.yaml with
  `type: http` pointing at http://127.0.0.1:18081/auth.
- With --ensure-stats, adds a trafficStats section (127.0.0.1:19996) if missing.
- Before switching, verifies the auth service answers /health; if it does
  not, the config is left untouched (old userpass keeps working) and the
  run fails.
- Backs up the config, restarts hysteria-server, verifies it is active and
  the UDP listen port is up, prints {"ok": true} or {"ok": false, "error": ...}.
"""
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.request

CFG = "/etc/hysteria/config.yaml"
USERS_FILE = "/var/lib/mouse-panel/hy2-users.json"
AUTH_URL = "http://127.0.0.1:18081/auth"
AUTH_HEALTH = "http://127.0.0.1:18081/health"


def run(cmd, timeout=30):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def listen_port(text):
    m = re.search(r"^listen:\s*:(\d+)(?:-\d+)?", text, re.M)
    return int(m.group(1)) if m else None


def replace_auth_block(lines):
    start = None
    for i, line in enumerate(lines):
        if line.startswith("auth:"):
            start = i
            break
    if start is None:
        raise RuntimeError("auth: block not found in config")
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if line.strip() == "" or (line and line[0] not in " \t"):
            break
        end += 1
    block = ["auth:", "  type: http", "  http:", "    url: %s" % AUTH_URL]
    return lines[:start] + block + lines[end:]


def ensure_traffic_stats(lines):
    if any(line.startswith("trafficStats:") for line in lines):
        return lines, None
    secret = secrets.token_hex(16)
    block = [
        "",
        "trafficStats:",
        "  listen: 127.0.0.1:19996",
        '  secret: "%s"' % secret,
    ]
    return lines + block, secret


def auth_service_up():
    try:
        with urllib.request.urlopen(AUTH_HEALTH, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def main():
    ensure_stats = "--ensure-stats" in sys.argv
    try:
        data = json.load(sys.stdin)
        raw_users = data["users"]
    except Exception as e:
        print(json.dumps({"ok": False, "error": "bad users json: %s" % e}))
        return 1
    # Normalize to full records.
    users = {}
    for name, info in raw_users.items():
        if isinstance(info, str):
            users[name] = {"password": info, "expires_at": None, "ip_limit": 0}
        elif isinstance(info, dict):
            users[name] = {
                "password": info.get("password", ""),
                "expires_at": info.get("expires_at"),
                "ip_limit": info.get("ip_limit", 0),
            }
    if not auth_service_up():
        print(json.dumps({"ok": False,
                          "error": "hy2-auth service not responding on 127.0.0.1:18081; config untouched"}))
        return 1
    try:
        with open(CFG) as f:
            text = f.read()
        lines = text.split("\n")
        lines = replace_auth_block(lines)
        if ensure_stats:
            lines, _ = ensure_traffic_stats(lines)
        new_text = "\n".join(lines)
        port = listen_port(new_text)
        # Write the auth user file first (0600), then switch the config.
        tmp = USERS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(users, f)
        os.replace(tmp, USERS_FILE)
        os.chmod(USERS_FILE, 0o600)
        run("cp %s %s.bak-sync" % (CFG, CFG))
        with open(CFG, "w") as f:
            f.write(new_text)
        run("systemctl restart hysteria-server")
        time.sleep(2)
        rc, out, _ = run("systemctl is-active hysteria-server")
        if out != "active":
            raise RuntimeError("hysteria-server not active after restart")
        if port:
            rc, _, _ = run("ss -lun | grep -q ':%d '" % port)
            if rc != 0:
                raise RuntimeError("UDP %d not listening after restart" % port)
        print(json.dumps({"ok": True, "port": port, "users": sorted(users)}))
        return 0
    except Exception as e:
        # Roll back and restart so the node keeps working.
        run("cp %s.bak-sync %s" % (CFG, CFG))
        run("systemctl restart hysteria-server")
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
