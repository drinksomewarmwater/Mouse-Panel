#!/usr/bin/env python3
"""Rewrite the Hysteria 2 auth block to userpass (multi-user) and restart.

Usage: hy2_auth_sync.py [--ensure-stats] < users.json
users.json: {"users": {"username": "password", ...}} (enabled users only).
- Replaces the whole `auth:` block in /etc/hysteria/config.yaml.
- With --ensure-stats, adds a trafficStats section (127.0.0.1:19996) if missing.
- Backs up the config, restarts hysteria-server, verifies it is active and
  the UDP listen port is up, prints {"ok": true} or {"ok": false, "error": ...}.
"""
import json
import re
import secrets
import subprocess
import sys
import time

CFG = "/etc/hysteria/config.yaml"


def run(cmd, timeout=30):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def listen_port(text):
    m = re.search(r"^listen:\s*:(\d+)", text, re.M)
    return int(m.group(1)) if m else None


def replace_auth_block(lines, users):
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
    block = ["auth:", "  type: userpass", "  userpass:"]
    for name in sorted(users):
        block.append('    %s: "%s"' % (name, users[name]))
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


def main():
    ensure_stats = "--ensure-stats" in sys.argv
    try:
        data = json.load(sys.stdin)
        users = data["users"]
    except Exception as e:
        print(json.dumps({"ok": False, "error": "bad users json: %s" % e}))
        return 1
    try:
        with open(CFG) as f:
            text = f.read()
        lines = text.split("\n")
        lines = replace_auth_block(lines, users)
        if ensure_stats:
            lines, _ = ensure_traffic_stats(lines)
        new_text = "\n".join(lines)
        port = listen_port(new_text)
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
