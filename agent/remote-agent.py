#!/usr/bin/env python3
"""Remote node agent: read node credentials/status/traffic live, rotate credentials.

Runs on a remote server, invoked over SSH by a mouse-panel API. Nothing here
is stored on the panel box: the API reads the values fresh on every panel load.
Configuration comes from /etc/mouse-panel-agent.conf (KEY=VALUE, written by
agent/install-agent.sh); every key has a sane default so the agent also
works with no config file at all.
Subcommands:
  read              print JSON: vless_uuid, hy2_password, obfs_password,
                    domain, ports, services, traffic (vnstat month), cert expiry
  rotate vless|hy2  rotate the credential, restart the service, verify,
                    print {"ok": true, "value": new} or {"ok": false, "error": ...}
  hystats           print the Hysteria /traffic JSON (per-user tx/rx)
  hy2-online        print the hy2-auth online-IP tracker JSON
  hy2-hop RANGE     set the Hysteria listen line to a hop range
                    ('20000-40000') or back to the single node port ('')
  vless-stats       print per-user VLESS delta since the last reset-read
                    (delegates to /usr/local/bin/vless_stats.py --reset).
                    Only the poller may call this: it consumes the counters.
"""
import json
import os
import re
import secrets
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

SINGBOX_CFG = "/etc/sing-box/config.json"
HYSTERIA_CFG = "/etc/hysteria/config.yaml"
AGENT_CONF = "/etc/mouse-panel-agent.conf"


def _load_conf():
    conf = {}
    try:
        with open(AGENT_CONF) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return conf


CONF = _load_conf()


def _default_iface():
    r = subprocess.run("ip route 2>/dev/null | grep -m1 '^default' | awk '{print $5}'",
                       shell=True, capture_output=True, text=True, timeout=10)
    return r.stdout.strip() or "eth0"


DOMAIN = CONF.get("DOMAIN", "")
VLESS_PORT = int(CONF.get("VLESS_PORT", "443"))
HY2_PORT = int(CONF.get("HY2_PORT", "443"))
VLESS_SECURITY = CONF.get("VLESS_SECURITY", "tls")
VNSTAT_IFACE = CONF.get("VNSTAT_IFACE", "") or _default_iface()
SINGBOX_CERT = CONF.get("SINGBOX_CERT", "/etc/sing-box/certs/fullchain.cer")


def run(cmd, timeout=30):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def read_hop_range(text):
    """Port-hopping range from the Hysteria listen line: '20000-40000' if
    listen is ':20000-40000', else '' (single port, hopping off)."""
    m = re.search(r"^listen:\s*:(\d+)-(\d+)\s*$", text, re.M)
    return "%s-%s" % (m.group(1), m.group(2)) if m else ""


def set_hop_range(hop_range):
    """Set the Hysteria listen line to a hop range ('20000-40000') or back
    to the single node port (''). Restarts hysteria-server. Returns the
    effective listen value. Raises on failure (config is rolled back)."""
    with open(HYSTERIA_CFG) as f:
        text = f.read()
    if hop_range:
        m = re.fullmatch(r"(\d+)-(\d+)", hop_range.strip())
        if not m or not (1 <= int(m.group(1)) < int(m.group(2)) <= 65535):
            raise ValueError("hop range must be START-END, e.g. 20000-40000")
        if int(m.group(2)) - int(m.group(1)) < 99:
            raise ValueError("hop range must span at least 100 ports")
        listen = ":%s" % hop_range.strip()
    else:
        listen = ":%d" % HY2_PORT
    new_text, n = re.subn(r"^listen:\s*:\d+(?:-\d+)?\s*$", "listen: %s" % listen,
                          text, count=1, flags=re.M)
    if n != 1:
        raise RuntimeError("listen line not found in hysteria config")
    run("cp %s %s.bak-hop" % (HYSTERIA_CFG, HYSTERIA_CFG))
    try:
        with open(HYSTERIA_CFG, "w") as f:
            f.write(new_text)
        run("systemctl restart hysteria-server")
        time.sleep(2)
        if run("systemctl is-active hysteria-server")[1] != "active":
            raise RuntimeError("hysteria-server not active after restart")
    except Exception:
        run("cp %s.bak-hop %s" % (HYSTERIA_CFG, HYSTERIA_CFG))
        run("systemctl restart hysteria-server")
        raise
    return listen


def read_hy2_passwords(text):
    # Admin HY2 password: with HTTP auth it lives in the hy2-auth user file
    # (written by hy2_auth_sync.py); fall back to the legacy userpass block
    # during the transition. Obfs (salamander) is optional: a node without
    # it reports obfs_enabled=False. Never raises for a missing password —
    # the panel builds links from its own users.db anyway.
    pw = ""
    try:
        with open("/var/lib/mouse-panel/hy2-users.json") as f:
            users = json.load(f)
        admin = users.get("admin") if isinstance(users, dict) else None
        if isinstance(admin, dict) and admin.get("password"):
            pw = admin["password"]
    except (OSError, ValueError):
        pass
    if not pw:
        m = re.search(r'userpass:\s*\n\s+admin:\s*"([^"]+)"', text)
        if m:
            pw = m.group(1)
    m2 = re.search(r'salamander:\s*\n\s+password:\s*"([^"]+)"', text)
    if not m2:
        return pw, "", False
    return pw, m2.group(1), True


def read_vless_uuid(cfg):
    for ib in cfg.get("inbounds", []):
        if ib.get("type") == "vless":
            users = ib.get("users", [])
            if users:
                return users[0]["uuid"]
    raise RuntimeError("vless inbound/user not found in sing-box config")


def read_reality(cfg):
    """Reality parameters of the VLESS inbound, if it uses Reality."""
    for ib in cfg.get("inbounds", []):
        if ib.get("type") == "vless":
            tls = ib.get("tls", {}) or {}
            reality = tls.get("reality", {}) or {}
            if reality.get("enabled"):
                hs = reality.get("handshake", {}) or {}
                return {
                    "pbk": reality.get("public_key", ""),
                    "sid": reality.get("short_id", ""),
                    "sni": tls.get("server_name", "") or hs.get("server", ""),
                    "fp": "chrome",
                }
    return None


def read_all():
    with open(SINGBOX_CFG) as f:
        cfg = json.load(f)
    with open(HYSTERIA_CFG) as f:
        hy_text = f.read()
    hy_pw, obfs_pw, obfs_enabled = read_hy2_passwords(hy_text)
    _, sb_out, _ = run("systemctl is-active sing-box")
    _, hy_out, _ = run("systemctl is-active hysteria-server")
    # Beijing date (server TZ is Asia/Shanghai, but compute explicitly).
    now_bj = datetime.now(timezone.utc) + timedelta(hours=8)
    month_str = now_bj.strftime("%Y-%m")
    date_str = now_bj.strftime("%Y-%m-%d")
    rc, vn_out, _ = run("vnstat -i %s --json m 1" % VNSTAT_IFACE)
    rx = tx = 0
    if rc == 0:
        try:
            months = json.loads(vn_out)["interfaces"][0]["traffic"].get("month", [])
            if months:
                rx = months[0].get("rx", 0)
                tx = months[0].get("tx", 0)
        except Exception:
            pass
    rc, vn_day, _ = run("vnstat -i %s --json d 1" % VNSTAT_IFACE)
    drx = dtx = 0
    if rc == 0:
        try:
            days = json.loads(vn_day)["interfaces"][0]["traffic"].get("day", [])
            for day in days:
                d = day.get("date", {})
                if (d.get("year"), d.get("month"), d.get("day")) == (now_bj.year, now_bj.month, now_bj.day):
                    drx = day.get("rx", 0)
                    dtx = day.get("tx", 0)
                    break
        except Exception:
            pass
    rc, cert_out, _ = run("openssl x509 -in %s -noout -enddate" % SINGBOX_CERT)
    not_after = ""
    days_left = None
    if rc == 0:
        m = re.search(r"notAfter=(.+)", cert_out)
        if m:
            dt = datetime.strptime(m.group(1).strip(), "%b %d %H:%M:%S %Y %Z")
            dt = dt.replace(tzinfo=timezone.utc)
            not_after = dt.strftime("%Y-%m-%d")
            days_left = (dt.date() - datetime.now(timezone.utc).date()).days
    reality = read_reality(cfg)
    security = "reality" if reality else VLESS_SECURITY
    quota_gb = None
    try:
        with open("/var/lib/mouse-panel/node.json") as f:
            quota_gb = json.load(f).get("quota_gb")
    except Exception:
        pass
    return {
        "domain": DOMAIN,
        "vless_port": VLESS_PORT,
        "hy2_port": HY2_PORT,
        "hop_range": read_hop_range(hy_text),
        "vless_security": security,
        "reality": reality,
        "obfs_enabled": obfs_enabled,
        "vless_uuid": read_vless_uuid(cfg),
        "hy2_password": hy_pw,
        "obfs_password": obfs_pw,
        "singbox": sb_out == "active",
        "hysteria": hy_out == "active",
        "traffic": {"rx": rx, "tx": tx},
        "today": {"date": date_str, "rx": drx, "tx": dtx},
        "month": month_str,
        "quota_gb": quota_gb,
        "cert": {"not_after": not_after, "days_left": days_left},
    }


def tcp_listening(port):
    rc, _, _ = run("ss -ltn | grep -q ':%d '" % port)
    return rc == 0


def udp_listening(port):
    rc, _, _ = run("ss -lun | grep -q ':%d '" % port)
    return rc == 0


def rotate_vless(cfg_path=SINGBOX_CFG, restart=True):
    new_uuid = str(uuid.uuid4())
    run("cp %s %s.bak-rotate" % (cfg_path, cfg_path))
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
        found = False
        for ib in cfg.get("inbounds", []):
            if ib.get("type") == "vless":
                users = ib.get("users", [])
                if users:
                    users[0]["uuid"] = new_uuid
                    found = True
        if not found:
            raise RuntimeError("vless inbound/user not found in sing-box config")
        with open(cfg_path, "w") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
        if restart:
            run("systemctl restart sing-box")
            time.sleep(2)
            if run("systemctl is-active sing-box")[1] != "active" or not tcp_listening(VLESS_PORT):
                raise RuntimeError("sing-box did not come back up after restart")
    except Exception:
        run("cp %s.bak-rotate %s" % (cfg_path, cfg_path))
        if restart:
            run("systemctl restart sing-box")
        raise
    return new_uuid


def rotate_hy2(cfg_path=HYSTERIA_CFG, restart=True):
    new_pw = secrets.token_hex(16)
    run("cp %s %s.bak-rotate" % (cfg_path, cfg_path))
    try:
        with open(cfg_path) as f:
            text = f.read()
        pat = re.compile(r'(userpass:\s*\n\s+admin:\s*")[^"]+(")')
        new_text, n = pat.subn(r"\g<1>" + new_pw + r"\g<2>", text)
        if n != 1:
            raise RuntimeError("userpass admin line not found or ambiguous in hysteria config")
        with open(cfg_path, "w") as f:
            f.write(new_text)
        if restart:
            run("systemctl restart hysteria-server")
            time.sleep(2)
            if run("systemctl is-active hysteria-server")[1] != "active" or not udp_listening(HY2_PORT):
                raise RuntimeError("hysteria-server did not come back up after restart")
    except Exception:
        run("cp %s.bak-rotate %s" % (cfg_path, cfg_path))
        if restart:
            run("systemctl restart hysteria-server")
        raise
    return new_pw


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "error": "usage: remote-agent.py read | rotate vless|hy2 | hystats | vless-sync | vless-stats"}))
        return 1
    if sys.argv[1] == "vless-sync":
        # Rewrite the sing-box VLESS user inbounds from a users JSON on
        # stdin (same payload as vless_sync.py). Delegates to
        # /usr/local/bin/vless_sync.py so the logic lives in one place.
        try:
            import subprocess
            data = sys.stdin.read()
            r = subprocess.run(
                ["python3", "/usr/local/bin/vless_sync.py"],
                input=data, capture_output=True, text=True, timeout=90,
            )
            out = r.stdout.strip()
            if out:
                print(out.splitlines()[-1])
            else:
                print(json.dumps({"ok": False, "error": "vless_sync produced no output: %s" % r.stderr[-300:]}))
            return 0 if r.returncode == 0 else 1
        except Exception as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            return 1
    if sys.argv[1] == "vless-stats":
        # Per-user VLESS delta since the last reset-read. Delegates to
        # vless_stats.py --reset (which zeroes the counters). Only the
        # poller may call this.
        try:
            import subprocess

            r = subprocess.run(
                ["python3", "/usr/local/bin/vless_stats.py", "--reset"],
                capture_output=True, text=True, timeout=30,
            )
            out = r.stdout.strip()
            if out:
                print(out.splitlines()[-1])
            else:
                print(json.dumps({"ok": False, "error": "vless_stats produced no output: %s" % r.stderr[-300:]}))
            return 0 if r.returncode == 0 else 1
        except Exception as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            return 1
    if sys.argv[1] == "read":
        try:
            print(json.dumps(read_all()))
            return 0
        except Exception as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            return 1
    if sys.argv[1] == "hystats":
        # Print the Hysteria /traffic JSON (per-user cumulative tx/rx).
        try:
            import urllib.request

            with open(HYSTERIA_CFG) as f:
                text = f.read()
            m = re.search(
                r"trafficStats:\s*\n\s+listen:\s*([^\n]+)\s*\n\s+secret:\s*\"?([^\"]+)\"?",
                text,
            )
            if not m:
                raise RuntimeError("trafficStats not configured")
            req = urllib.request.Request(
                "http://%s/traffic" % m.group(1).strip(),
                headers={"Authorization": m.group(2)},
            )
            print(urllib.request.urlopen(req, timeout=5).read().decode())
            return 0
        except Exception as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            return 1
    if sys.argv[1] == "hy2-hop":
        # Set the Hysteria listen line to a hop range ('20000-40000') or
        # back to the single node port (empty argument). Reads the range
        # from argv[2] ('' = off). Restarts hysteria-server.
        try:
            listen = set_hop_range(sys.argv[2] if len(sys.argv) > 2 else "")
            print(json.dumps({"ok": True, "listen": listen}))
            return 0
        except Exception as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            return 1
    if sys.argv[1] == "hy2-online":
        # Print the hy2-auth online-IP tracker ({username: {ip: last_seen}}).
        # Used by the panel API to show each user's currently online IPs.
        try:
            with open("/var/lib/mouse-panel/hy2-online.json") as f:
                print(f.read())
            return 0
        except OSError:
            print("{}")
            return 0
    if sys.argv[1] == "rotate" and len(sys.argv) >= 3 and sys.argv[2] in ("vless", "hy2"):
        try:
            value = rotate_vless() if sys.argv[2] == "vless" else rotate_hy2()
            print(json.dumps({"ok": True, "value": value}))
            return 0
        except Exception as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            return 1
    print(json.dumps({"ok": False, "error": "bad arguments"}))
    return 1


if __name__ == "__main__":
    sys.exit(main())
