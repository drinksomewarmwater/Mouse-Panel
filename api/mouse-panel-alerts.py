#!/usr/bin/env python3
"""Monthly traffic alerts for the node panel (cron, every 5 minutes).

Reads each server's monthly usage (local: data.json from vnstat; remote:
live SSH read of the remote agent) and compares it with the thresholds in
alerts.json. When usage reaches the alert threshold, sends one Telegram
message per server per month. When it reaches the disable threshold, stops
the local node's services (once per month; restart is manual) and notifies
via Telegram. Remote servers cannot be stopped through the panel's SSH
key, so for them the disable action is a Telegram notice telling the admin
to stop the node manually.

State lives in /var/lib/mouse-panel/alerts.json (0600). A new month
(YYYY-MM) naturally resets the once-per-month flags. Traffic itself is
counted by vnstat and is never touched here, so panel updates cannot
reset it.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

BASE = "/var/lib/mouse-panel"
ALERTS_PATH = os.path.join(BASE, "alerts.json")
SERVERS_PATH = os.path.join(BASE, "servers.json")
DATA_PATH = os.path.join(BASE, "data.json")
NODE_PATH = os.path.join(BASE, "node.json")
LOG_PATH = os.path.join(BASE, "alerts.log")
CONFIG_PATH = os.path.join(BASE, "config.json")
DEFAULT_PANEL_NAME = "鼠鼠面板"

PANEL_SSH_KEY = "/root/.ssh/id_ed25519_panel"
DEFAULT_ALERTS = {
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "alert_threshold_pct": 80,
    "disable_threshold_pct": 95,
    "alert_sent_month": {},
    "disabled_month": {},
}
DEFAULT_SERVERS = [
    {"id": "local", "name": "本机", "type": "local"},
]


def log(msg):
    line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line)
    except Exception:
        pass
    print(line, end="")


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj, mode=None):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    if mode is not None:
        try:
            os.chmod(path, mode)
        except Exception:
            pass


def panel_name():
    """Panel display name from config.json, else the project default."""
    d = load_json(CONFIG_PATH, {})
    name = (d.get("panel_name") or "").strip() if isinstance(d, dict) else ""
    return name if name else DEFAULT_PANEL_NAME


def load_alerts():
    d = load_json(ALERTS_PATH, {})
    if not isinstance(d, dict):
        d = {}
    out = dict(DEFAULT_ALERTS)
    out.update(d)
    for k in ("alert_sent_month", "disabled_month"):
        if not isinstance(out.get(k), dict):
            out[k] = {}
    try:
        out["alert_threshold_pct"] = float(out.get("alert_threshold_pct", 80))
    except (TypeError, ValueError):
        out["alert_threshold_pct"] = 80
    try:
        out["disable_threshold_pct"] = float(out.get("disable_threshold_pct", 95))
    except (TypeError, ValueError):
        out["disable_threshold_pct"] = 95
    out["telegram_bot_token"] = (out.get("telegram_bot_token") or "").strip()
    out["telegram_chat_id"] = (out.get("telegram_chat_id") or "").strip()
    return out


def load_servers():
    sv = load_json(SERVERS_PATH, None)
    if not isinstance(sv, list) or not sv:
        return [dict(s) for s in DEFAULT_SERVERS]
    return sv


def run(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


def remote_traffic(server):
    """Monthly traffic bytes, month string and quota (GB) for a remote
    server, via the panel SSH key and the remote agent. Raises
    RuntimeError when the agent does not answer."""
    host = server.get("host")
    port = server.get("port") or 22
    cmd = ("ssh -i %s -o StrictHostKeyChecking=accept-new "
           "-o ConnectTimeout=15 -o BatchMode=yes -p %s root@%s "
           "python3 /usr/local/bin/remote-agent.py read"
           % (PANEL_SSH_KEY, port, host))
    rc, out, err = run(cmd, timeout=30)
    if rc != 0:
        raise RuntimeError("remote read failed for %s: %s"
                           % (host or server.get("id"), err or out))
    try:
        d = json.loads(out)
    except Exception:
        raise RuntimeError("remote read returned bad JSON for %s"
                           % (host or server.get("id")))
    if d.get("ok") is False:
        raise RuntimeError("remote read failed for %s"
                           % (host or server.get("id")))
    t = d.get("traffic") or {}
    return (t.get("rx") or 0) + (t.get("tx") or 0), d.get("month"), d.get("quota_gb")


def telegram_send(token, chat_id, text, timeout=15):
    url = "https://api.telegram.org/bot%s/sendMessage" % token
    body = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            d = json.loads(resp.read().decode("utf-8", "replace"))
            if d.get("ok"):
                return True, ""
            return False, str(d.get("description") or "Telegram 返回失败")
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read().decode("utf-8", "replace"))
            return False, str(d.get("description") or ("HTTP %d" % e.code))
        except Exception:
            return False, "HTTP %d" % e.code
    except Exception as e:
        return False, str(e)


def local_services_active():
    rc, out, _ = run("systemctl is-active sing-box hysteria-server")
    return "active" in out.split()


def stop_local_services():
    rc, out, err = run("systemctl stop sing-box hysteria-server", timeout=60)
    return rc == 0, (err or out)


def main():
    cfg = load_alerts()
    month = time.strftime("%Y-%m")
    now = time.strftime("%Y-%m-%d %H:%M")
    token = cfg["telegram_bot_token"]
    chat_id = cfg["telegram_chat_id"]
    alert_pct = cfg["alert_threshold_pct"]
    disable_pct = cfg["disable_threshold_pct"]
    data = load_json(DATA_PATH, {})
    node = load_json(NODE_PATH, {})
    changed = False

    def notify(text):
        if not token or not chat_id:
            log("telegram not configured, skip: %s" % text.split("\n")[0])
            return False
        ok, err = telegram_send(token, chat_id, text)
        if ok:
            log("telegram sent: %s" % text.split("\n")[0])
        else:
            log("telegram failed: %s" % err)
        return ok

    for s in load_servers():
        sid = s.get("id")
        name = s.get("name") or sid
        quota = s.get("quota_gb")
        try:
            quota = float(quota) if quota else None
        except (TypeError, ValueError):
            quota = None
        try:
            if s.get("type") == "local":
                t = (data.get("traffic") or {})
                used = (t.get("rx") or 0) + (t.get("tx") or 0)
                m = t.get("month") or month
                if not quota:
                    q = data.get("quota_gb") or node.get("quota_gb")
                    try:
                        quota = float(q) if q else None
                    except (TypeError, ValueError):
                        quota = None
            else:
                used, m, agent_quota = remote_traffic(s)
                m = m or month
                if not quota:
                    try:
                        quota = float(agent_quota) if agent_quota else None
                    except (TypeError, ValueError):
                        quota = None
        except Exception as e:
            log("skip %s: %s" % (name, e))
            continue
        if not quota:
            continue
        used_gb = used / 1e9
        pct = used_gb / quota * 100
        if pct >= disable_pct and cfg["disabled_month"].get(sid) != month:
            if s.get("type") == "local":
                if local_services_active():
                    ok, detail = stop_local_services()
                    action = ("已停用 sing-box 与 hysteria-server"
                              if ok else "停用失败（%s），请手动处理" % detail)
                else:
                    action = "节点服务已处于停止状态"
                text = ("【%s】流量预警\n"
                        "服务器：%s\n"
                        "本月已用 %.2f GB / %.0f GB（%.1f%%），达到自动停用阈值 %.0f%%\n"
                        "%s\n时间：%s（北京时间）\n恢复请手动启动节点服务。"
                        % (panel_name(), name, used_gb, quota, pct, disable_pct, action, now))
            else:
                text = ("【%s】流量预警\n"
                        "服务器：%s\n"
                        "本月已用 %.2f GB / %.0f GB（%.1f%%），达到自动停用阈值 %.0f%%\n"
                        "该服务器无法经面板远程停用，请手动停止节点服务。\n时间：%s（北京时间）"
                        % (panel_name(), name, used_gb, quota, pct, disable_pct, now))
            notify(text)
            cfg["disabled_month"][sid] = month
            # A disable notice already covers the alert, so mark the alert
            # as sent too; otherwise the next run would send a redundant
            # alert for a server that was already disabled this month.
            cfg["alert_sent_month"][sid] = month
            changed = True
            log("disabled %s at %.1f%%" % (name, pct))
        elif pct >= alert_pct and cfg["alert_sent_month"].get(sid) != month:
            if not token or not chat_id:
                continue
            text = ("【%s】流量提醒\n"
                    "服务器：%s\n"
                    "本月已用 %.2f GB / %.0f GB（%.1f%%），达到提醒阈值 %.0f%%\n"
                    "达到 %.0f%% 时将自动停用节点。\n时间：%s（北京时间）"
                    % (panel_name(), name, used_gb, quota, pct, alert_pct, disable_pct, now))
            if notify(text):
                cfg["alert_sent_month"][sid] = month
                changed = True
                log("alerted %s at %.1f%%" % (name, pct))
    if changed:
        save_json(ALERTS_PATH, cfg, mode=0o600)
    return 0


if __name__ == "__main__":
    sys.exit(main())
