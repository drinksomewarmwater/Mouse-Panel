#!/usr/bin/env python3
"""Sync enabled panel users into the sing-box VLESS inbounds on every server.

Reads /var/lib/mouse-panel/users.db and, for each server (local, plus every
remote server in servers.json via the agent's vless-sync command), computes
the users who should have a VLESS inbound on that server: role=user,
enabled=1, pw_set=1, and not disabled on that server (disabled_json[server]
is null). The admin's own inbound (tag vless-in) is never touched. Each
server gets the same UUID/port per user. Skips a server entirely when its
user set is unchanged since the last sync (hash in meta), so the poller/API
can call this every 5 minutes safely. Only reloads sing-box (SIGHUP) when
the config actually changed. Prints {"ok": ..., "changed": ...}.
"""
import hashlib
import json
import sqlite3
import subprocess

DB = "/var/lib/mouse-panel/users.db"
SERVERS_PATH = "/var/lib/mouse-panel/servers.json"
PANEL_KEY = "/root/.ssh/id_ed25519_panel"
LOCAL_SYNC = "/usr/local/bin/vless_sync.py"
AGENT_SCRIPT = "/usr/local/bin/remote-agent.py"


def users_for(server):
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT username, vless_uuid, vless_port, disabled_json FROM users"
        " WHERE role='user' AND enabled=1 AND pw_set=1"
        " AND vless_uuid IS NOT NULL AND vless_port IS NOT NULL"
    ).fetchall()
    conn.close()
    out = {}
    for username, vuuid, vport, djson in rows:
        try:
            dj = json.loads(djson) if djson else {}
        except Exception:
            dj = {}
        if not isinstance(dj, dict):
            dj = {}
        if dj.get(server):
            continue
        out[username] = {"uuid": vuuid, "port": int(vport)}
    return out


def server_ids():
    ids = ["local"]
    try:
        with open(SERVERS_PATH) as f:
            servers = json.load(f)
    except Exception:
        return ids
    for s in servers:
        if isinstance(s, dict) and s.get("type") == "remote" and s.get("id"):
            ids.append(s["id"])
    return ids


def remote_servers():
    try:
        with open(SERVERS_PATH) as f:
            servers = json.load(f)
    except Exception:
        return []
    return [s for s in servers if isinstance(s, dict) and s.get("type") == "remote"]


def last_hash(server):
    conn = sqlite3.connect(DB)
    row = conn.execute(
        "SELECT value FROM meta WHERE key=?", ("vless_sync_hash_" + server,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def save_hash(server, h):
    conn = sqlite3.connect(DB)
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        ("vless_sync_hash_" + server, h),
    )
    conn.commit()
    conn.close()


def parse_out(r):
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return {
            "ok": False,
            "error": "unparsable: %s %s" % (r.stdout[-300:], r.stderr[-300:]),
        }


def run_local(payload):
    r = subprocess.run(
        ["python3", LOCAL_SYNC],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=90,
    )
    return parse_out(r)


def run_remote(server, payload):
    cmd = (
        "ssh -i %s -o StrictHostKeyChecking=accept-new "
        "-o ConnectTimeout=15 -o BatchMode=yes -p %s root@%s python3 %s vless-sync"
        % (PANEL_KEY, server.get("port") or 22, server.get("host"), AGENT_SCRIPT)
    )
    r = subprocess.run(
        cmd, shell=True, input=json.dumps(payload),
        capture_output=True, text=True, timeout=90,
    )
    return parse_out(r)


def main():
    results = {}
    ok = True
    remotes = {s.get("id"): s for s in remote_servers()}
    for server in server_ids():
        users = users_for(server)
        digest = hashlib.sha256(
            json.dumps(users, sort_keys=True).encode()
        ).hexdigest()
        if digest == last_hash(server):
            results[server] = {"ok": True, "changed": False, "users": sorted(users)}
            continue
        payload = {"users": users}
        if server == "local":
            res = run_local(payload)
        else:
            res = run_remote(remotes[server], payload) if server in remotes else {"ok": False, "error": "remote server %s not in servers.json" % server}
        results[server] = res
        if res.get("ok"):
            save_hash(server, digest)
            results[server]["changed"] = True
        else:
            ok = False
    print(json.dumps({"ok": ok, "changed": any(r.get("changed") for r in results.values()),
                      "servers": results}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
