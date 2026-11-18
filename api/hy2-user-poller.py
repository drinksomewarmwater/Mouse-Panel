#!/usr/bin/env python3
"""Per-user HY2 traffic poller + monthly-limit enforcement.

Runs every 5 minutes from cron. For each server (local, plus every remote
server in servers.json via the agent's hystats command) it reads the
Hysteria Traffic Stats API (/traffic, a per-user cumulative {tx, rx} map),
diffs it against the last-seen counters, and accumulates the delta into
usage_daily (Beijing date, one row per user per server per day). Then it
enforces monthly limits PER SERVER: a user whose month total on one server
exceeds that server's limit (limits_json) loses their nodes on that server
only (disabled_json[server]='over_limit'); the other server is unaffected.
A per-server over-limit is cleared automatically once the new month's total
on that server is under the limit again (i.e. on the 1st). Users with no
limit for a server are unlimited there and never disabled. The admin is
never disabled.

Per-user usage counts HY2 + VLESS, combined per server. Limits and
statistics are per server only; HY2 and VLESS are never split apart
anywhere (UI, API, or database). VLESS is counted per user through the
sing-box v2ray_api (custom build with the `with_v2ray_api` tag):
vless_sync.py gives each user inbound a "name" and lists the names in
experimental.v2ray_api.stats.users, so the API exposes cumulative
user>>><name>>>>traffic>>>uplink/downlink counters. /usr/local/bin/
vless_stats.py --reset reads them (locally, and on remote servers through
the agent's `vless-stats` subcommand): each reset-read returns the
traffic since the last reset-read and zeroes the counters, so the
returned delta is written straight into usage_daily alongside the HY2
delta -- no per-protocol last-seen state is stored. A sing-box
restart/reload also zeroes the counters, which a reset-read simply
reports as zero delta. Counting starts from the day the v2ray_api build
is deployed; earlier VLESS usage was never recorded per user and is not
backfilled.

Only calls the sync scripts when a user's per-server disabled state
actually changed (the sync scripts themselves are no-ops when the user
set is unchanged).
"""
import json
import re
import sqlite3
import subprocess
import urllib.request
from datetime import datetime, timedelta, timezone

DB = "/var/lib/mouse-panel/users.db"
SERVERS_PATH = "/var/lib/mouse-panel/servers.json"
D2_CFG = "/etc/hysteria/config.yaml"
PANEL_KEY = "/root/.ssh/id_ed25519_panel"
AGENT_SCRIPT = "/usr/local/bin/remote-agent.py"
SYNC_SCRIPT = "/usr/local/bin/hy2-sync-users.py"
VLESS_SYNC_SCRIPT = "/usr/local/bin/vless-sync-users.py"
VLESS_STATS_SCRIPT = "/usr/local/bin/vless_stats.py"
LOG = "/var/lib/mouse-panel/poller.log"


def log(msg):
    now = datetime.now(timezone.utc) + timedelta(hours=8)
    line = "%s %s\n" % (now.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG, "a") as f:
            f.write(line)
    except Exception:
        pass
    print(line, end="", flush=True)


def read_local_traffic():
    with open(D2_CFG) as f:
        text = f.read()
    m = re.search(
        r"trafficStats:\s*\n\s+listen:\s*([^\n]+)\s*\n\s+secret:\s*\"?([^\"]+)\"?",
        text,
    )
    if not m:
        raise RuntimeError("D2 trafficStats not configured")
    req = urllib.request.Request(
        "http://%s/traffic" % m.group(1).strip(),
        headers={"Authorization": m.group(2)},
    )
    return json.loads(urllib.request.urlopen(req, timeout=10).read().decode())


def remote_servers():
    try:
        with open(SERVERS_PATH) as f:
            servers = json.load(f)
    except Exception:
        return []
    return [s for s in servers if isinstance(s, dict) and s.get("type") == "remote"]


def read_remote_traffic(server):
    cmd = (
        "ssh -i %s -o StrictHostKeyChecking=accept-new "
        "-o ConnectTimeout=15 -o BatchMode=yes -p %s root@%s python3 %s hystats"
        % (PANEL_KEY, server.get("port") or 22, server.get("host"), AGENT_SCRIPT)
    )
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError("remote hystats failed: %s" % (r.stderr or r.stdout))
    d = json.loads(r.stdout)
    if d.get("ok") is False:
        raise RuntimeError("remote hystats failed: %s" % d.get("error"))
    return d


def read_local_vless():
    """Per-user VLESS delta since the last reset-read (local v2ray_api).

    Returns {"username": {"tx": N, "rx": N}}. The counters are zeroed by
    this read; the poller writes the delta into usage_daily immediately.
    Only the poller may call vless_stats.py --reset."""
    r = subprocess.run(
        ["python3", VLESS_STATS_SCRIPT, "--reset"],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError("local vless_stats failed: %s" % (r.stderr or r.stdout))
    d = json.loads(r.stdout)
    if d.get("ok") is False:
        raise RuntimeError("local vless_stats failed: %s" % d.get("error"))
    return d.get("users", {})


def read_remote_vless(server):
    """Per-user VLESS counters from a remote server via the agent."""
    cmd = (
        "ssh -i %s -o StrictHostKeyChecking=accept-new "
        "-o ConnectTimeout=15 -o BatchMode=yes -p %s root@%s python3 %s vless-stats"
        % (PANEL_KEY, server.get("port") or 22, server.get("host"), AGENT_SCRIPT)
    # NOTE: the agent's vless-stats subcommand itself invokes
    # vless_stats.py --reset, so this returns the delta since the last
    # reset-read (see agent/remote-agent.py).
    )
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError("remote vless_stats failed: %s" % (r.stderr or r.stdout))
    d = json.loads(r.stdout)
    if d.get("ok") is False:
        raise RuntimeError("remote vless_stats failed: %s" % d.get("error"))
    return d.get("users", {})


def main():
    now_bj = datetime.now(timezone.utc) + timedelta(hours=8)
    today = now_bj.strftime("%Y-%m-%d")
    month = now_bj.strftime("%Y-%m")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    # Migration: VLESS usage used to live in per-protocol tables
    # (usage_vless_daily / counters_vless). Fold any rows into the combined
    # usage_daily table, then drop them -- limits and statistics are per
    # server only.
    try:
        conn.execute(
            "INSERT INTO usage_daily (username, server, date, tx, rx) "
            "SELECT username, server, date, tx, rx FROM usage_vless_daily "
            "ON CONFLICT(username, server, date) DO UPDATE SET "
            "tx=tx+excluded.tx, rx=rx+excluded.rx"
        )
        conn.execute("DROP TABLE IF EXISTS usage_vless_daily")
        conn.execute("DROP TABLE IF EXISTS counters_vless")
        conn.commit()
    except Exception:
        pass
    cols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
    for col, ddl in [
        ("expires_at", "ALTER TABLE users ADD COLUMN expires_at INTEGER"),
    ]:
        if col not in cols:
            conn.execute(ddl)
    conn.commit()
    users = {
        r["username"]: dict(r)
        for r in conn.execute(
            "SELECT username, limit_gb, limits_json, disabled_json, enabled,"
            " disabled_reason, expires_at FROM users"
        )
    }
    servers = {}
    try:
        servers["local"] = read_local_traffic()
    except Exception as e:
        log("ERROR reading local traffic: %s" % e)
    for server in remote_servers():
        sid = server.get("id") or server.get("host", "")
        try:
            servers[sid] = read_remote_traffic(server)
        except Exception as e:
            log("ERROR reading remote %s traffic: %s" % (sid, e))
    changed = False
    for server, stats in servers.items():
        for username, st in stats.items():
            if username not in users:
                continue
            tx = int(st.get("tx", 0))
            rx = int(st.get("rx", 0))
            row = conn.execute(
                "SELECT last_tx, last_rx FROM counters WHERE username=? AND server=?",
                (username, server),
            ).fetchone()
            if row is None:
                dtx, drx = tx, rx
                conn.execute(
                    "INSERT INTO counters (username, server, last_tx, last_rx, updated_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (username, server, tx, rx, today),
                )
            else:
                # Counter reset (server restart) -> count only since the restart.
                dtx = tx if tx < row["last_tx"] else tx - row["last_tx"]
                drx = rx if rx < row["last_rx"] else rx - row["last_rx"]
                conn.execute(
                    "UPDATE counters SET last_tx=?, last_rx=?, updated_at=? WHERE username=? AND server=?",
                    (tx, rx, today, username, server),
                )
            if dtx or drx:
                conn.execute(
                    "INSERT INTO usage_daily (username, server, date, tx, rx) VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(username, server, date) DO UPDATE SET"
                    " tx=tx+excluded.tx, rx=rx+excluded.rx",
                    (username, server, today, dtx, drx),
                )
    conn.commit()
    # VLESS accumulation (same 5-minute rhythm, same reset handling).
    vless_servers = {}
    try:
        vless_servers["local"] = read_local_vless()
    except Exception as e:
        log("ERROR reading local VLESS stats: %s" % e)
    for server in remote_servers():
        sid = server.get("id") or server.get("host", "")
        try:
            vless_servers[sid] = read_remote_vless(server)
        except Exception as e:
            log("ERROR reading remote %s VLESS stats: %s" % (sid, e))
    # VLESS deltas (reset-read) straight into usage_daily (combined
    # HY2+VLESS, per server). No per-protocol tables.
    for server, stats in vless_servers.items():
        for username, st in stats.items():
            if username not in users:
                continue
            tx = int(st.get("tx", 0))
            rx = int(st.get("rx", 0))
            if tx or rx:
                conn.execute(
                    "INSERT INTO usage_daily (username, server, date, tx, rx) VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(username, server, date) DO UPDATE SET"
                    " tx=tx+excluded.tx, rx=rx+excluded.rx",
                    (username, server, today, tx, rx),
                )
    conn.commit()
    # Enforcement: per server. A user over the limit on one server loses
    # their nodes on that server only (disabled_json[server]='over_limit').
    # Legacy global over_limit (enabled=0) is migrated to the per-server
    # model on the first run after the upgrade.
    # Expiry enforcement (global, not per-server): an expired account loses
    # its nodes on every server. It is NOT auto-restored (unlike the monthly
    # over-limit, which clears on the 1st); the admin re-enables it, which
    # clears the expiry.
    import time as _time
    now_ts = int(_time.time())
    for username, u in users.items():
        if username == "admin":
            continue
        try:
            exp = int(u["expires_at"]) if u.get("expires_at") is not None else None
        except (TypeError, ValueError):
            exp = None
        if exp is not None and exp < now_ts and u["enabled"]:
            conn.execute(
                "UPDATE users SET enabled=0, disabled_reason='expired' WHERE username=?",
                (username,),
            )
            log("DISABLE %s: account expired" % username)
            changed = True
            u["enabled"] = 0
            u["disabled_reason"] = "expired"
    conn.commit()
    server_ids = sorted(set(list(servers.keys()) + ["local"]))
    for username, u in users.items():
        if username == "admin":
            continue
        try:
            limits = json.loads(u["limits_json"]) if u["limits_json"] else {}
        except Exception:
            limits = {}
        if not isinstance(limits, dict):
            limits = {}
        # Legacy single limit applies to every server when limits_json is empty.
        if not limits and u["limit_gb"] is not None:
            limits = {sid: u["limit_gb"] for sid in server_ids}
        try:
            disabled = json.loads(u["disabled_json"]) if u["disabled_json"] else {}
        except Exception:
            disabled = {}
        if not isinstance(disabled, dict):
            disabled = {}
        if not u["enabled"] and u["disabled_reason"] == "over_limit":
            # Migrate: the old global disable becomes a per-server disable
            # on every server that was over at the time (unknown now, so all
            # servers with a limit); the poller clears them per server below.
            for sid in server_ids:
                if sid not in disabled and limits.get(sid) is not None:
                    disabled[sid] = "over_limit"
            conn.execute(
                "UPDATE users SET enabled=1, disabled_reason=NULL, disabled_json=? WHERE username=?",
                (json.dumps(disabled), username),
            )
            changed = True
        dirty = False
        for sid in server_ids:
            limit = limits.get(sid)
            row = conn.execute(
                "SELECT COALESCE(SUM(tx),0) AS tx, COALESCE(SUM(rx),0) AS rx FROM usage_daily"
                " WHERE username=? AND server=? AND substr(date,1,7)=?",
                (username, sid, month),
            ).fetchone()
            total = row["tx"] + row["rx"]
            if limit is not None and total > limit * 1e9:
                if disabled.get(sid) != "over_limit":
                    disabled[sid] = "over_limit"
                    log("DISABLE %s on %s: month total %.2f GB > limit %.2f GB"
                        % (username, sid, total / 1e9, limit))
                    dirty = True
            elif disabled.get(sid) == "over_limit":
                del disabled[sid]
                log("RE-ENABLE %s on %s: new month total %.2f GB under limit"
                    % (username, sid, total / 1e9))
                dirty = True
        if dirty:
            conn.execute(
                "UPDATE users SET disabled_json=? WHERE username=?",
                (json.dumps(disabled), username),
            )
            changed = True
    conn.commit()
    conn.close()
    if changed:
        for script in (SYNC_SCRIPT, VLESS_SYNC_SCRIPT):
            r = subprocess.run(
                ["python3", script], capture_output=True, text=True, timeout=180
            )
            log("sync after enforcement (%s): %s" % (script, r.stdout.strip()[:300]))
    else:
        log("poll ok (servers: %s)" % ",".join(sorted(servers)))


if __name__ == "__main__":
    main()
