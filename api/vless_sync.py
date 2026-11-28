#!/usr/bin/env python3
"""Rewrite sing-box VLESS inbounds for panel users (multi-user VLESS).

Usage: vless_sync.py < users.json
users.json: {"users": {"username": {"uuid": "...", "port": 20000}, ...}}
(enabled users for THIS server only; the admin's own inbound is untouched.)

- Keeps the admin inbound (tag "vless-in") exactly as-is.
- Replaces any existing user inbounds (tag "vless-user-*") with one inbound
  per user: listen_port=user port, users=[{uuid, flow}] and the TLS/Reality
  block copied from the admin inbound (same keypair / certificate).
- Validates the new config with `sing-box check`, backs the old config up
  to .bak-sync, reloads sing-box (SIGHUP, existing connections are kept),
  verifies the service is active and every user port is listening.
  On any failure it restores the backup and reloads again.
- Prints {"ok": true, "users": [...]} or {"ok": false, "error": ...}.

Per-user VLESS traffic counting (v2ray_api):
- Each user inbound carries "name": <username> so the v2ray_api stats
  service can track that user by name (stats.users lists names, not UUIDs).
- The experimental.v2ray_api section is maintained here: listen on
  127.0.0.1:10085, stats.users = the enabled usernames. Any other
  experimental settings already in the config are preserved.
- The admin inbound (vless-in) is included in stats.users (as "admin"),
  so the admin's VLESS traffic is counted per user. The admin has no
  per-user limit, so counting it never triggers limit enforcement.
- A reload resets the in-memory v2ray counters; the poller treats a
  lower reading as a reset (counts only the new value, never negative).

"""
import copy
import json
import subprocess
import sys
import time

CFG = "/etc/sing-box/config.json"


def run(cmd, timeout=30):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def tcp_listening(port):
    rc, _, _ = run("ss -ltn | grep -q ':%d '" % port)
    return rc == 0


def main():
    try:
        data = json.load(sys.stdin)
        users = data["users"]
        if not isinstance(users, dict):
            raise ValueError("users must be an object")
    except Exception as e:
        print(json.dumps({"ok": False, "error": "bad users json: %s" % e}))
        return 1
    try:
        with open(CFG) as f:
            cfg = json.load(f)
        inbounds = cfg.get("inbounds") or []
        admin = None
        for ib in inbounds:
            if ib.get("tag") == "vless-in" and ib.get("type") == "vless":
                admin = ib
                break
        if admin is None:
            raise RuntimeError("admin vless inbound (tag vless-in) not found")
        admin_users = admin.get("users") or []
        flow = admin_users[0].get("flow", "") if admin_users else ""
        admin_tls = copy.deepcopy(admin.get("tls") or {})
        listen = admin.get("listen", "::")
        new_inbounds = [ib for ib in inbounds
                        if not (isinstance(ib.get("tag"), str)
                                and ib["tag"].startswith("vless-user-"))]
        ports = []
        for username in sorted(users):
            u = users[username]
            port = int(u["port"])
            ports.append(port)
            ib = {
                "type": "vless",
                "tag": "vless-user-%s" % username,
                "listen": listen,
                "listen_port": port,
                "users": [{"name": username, "uuid": u["uuid"], "flow": flow}],
            }
            if admin_tls:
                ib["tls"] = copy.deepcopy(admin_tls)
            new_inbounds.append(ib)
        new_cfg = dict(cfg)
        new_cfg["inbounds"] = new_inbounds
        exp = copy.deepcopy(cfg.get("experimental") or {})
        exp["v2ray_api"] = {
            "listen": "127.0.0.1:10085",
            "stats": {"enabled": True, "users": sorted(set(users) | {"admin"})},
        }
        new_cfg["experimental"] = exp
        tmp = CFG + ".tmp-sync"
        with open(tmp, "w") as f:
            json.dump(new_cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")
        rc, out, err = run("sing-box check -c %s" % tmp, timeout=30)
        if rc != 0:
            raise RuntimeError("sing-box check failed: %s" % (err or out))
        run("cp %s %s.bak-sync" % (CFG, CFG))
        run("cp %s %s" % (tmp, CFG))
        run("systemctl reload sing-box", timeout=30)
        time.sleep(1)
        rc, out, _ = run("systemctl is-active sing-box")
        if out != "active":
            raise RuntimeError("sing-box not active after reload")
        missing = [p for p in ports if not tcp_listening(p)]
        if missing:
            raise RuntimeError("user ports not listening after reload: %s" % missing)
        print(json.dumps({"ok": True, "users": sorted(users), "ports": ports}))
        return 0
    except Exception as e:
        run("cp %s.bak-sync %s" % (CFG, CFG))
        run("systemctl reload sing-box", timeout=30)
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
