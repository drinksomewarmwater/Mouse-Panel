#!/usr/bin/env python3
"""Hysteria 2 HTTP auth server for Mouse-Panel (per-user expiry + IP limit).

Hysteria calls POST /auth on every new client connection with
{"addr": "client_ip:port", "auth": "password"} and expects
{"ok": true} or {"ok": false, "msg": "..."}.

The user file /var/lib/mouse-panel/hy2-users.json is written by
hy2_auth_sync.py (enabled users only):
  {"username": {"password": "...", "expires_at": 1234567890|null, "ip_limit": 0}}
  - expires_at: Unix timestamp, null = permanent.
  - ip_limit: max concurrent online IPs, 0 = unlimited.

Online IPs are tracked in memory (per user, {ip: last_seen}) and persisted
to /var/lib/mouse-panel/hy2-online.json so the panel API can display them.
An IP counts toward the limit if it authed within IP_TIMEOUT seconds
(1800s). Note the known limit: a device that stays connected for hours
without re-authing ages out of the tracker, so the limit is enforced at
connection time, not as a live session count. Hysteria's stats API
(/online) only reports session counts, not IPs, so there is no live-IP
source to merge in on this build.

Security (mirrors the upstream portal's /auth hardening):
- Only accepts requests from loopback (127.0.0.1 / ::1). Hysteria's
  masquerade can proxy public traffic onto loopback ports, so any request
  carrying X-Forwarded-For / Forwarded headers is rejected outright.
- Failed auths are rate-limited globally (30/min): the endpoint would
  otherwise be a password oracle (it distinguishes "user not found" from
  a wrong password by timing/response).

Runs as a systemd service (hy2-auth) on 127.0.0.1:18081. Stdlib only.
"""
import hashlib
import hmac
import ipaddress
import json
import os
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

USERS_PATH = "/var/lib/mouse-panel/hy2-users.json"
ONLINE_PATH = "/var/lib/mouse-panel/hy2-online.json"
LISTEN = ("127.0.0.1", 18081)
IP_TIMEOUT = 1800  # an IP counts toward the limit if seen within 30 min
FAIL_WINDOW = 60   # rate-limit window (seconds)
FAIL_MAX = 30      # max failed auths per window

_lock = threading.Lock()
_tracker = {}          # username -> {ip: last_seen_ts}
_failures = deque()    # timestamps of failed auths (global)
_users_cache = {"mtime": None, "data": {}}


def _load_users():
    try:
        mtime = os.path.getmtime(USERS_PATH)
    except OSError:
        return {}
    if _users_cache["mtime"] == mtime:
        return _users_cache["data"]
    try:
        with open(USERS_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    _users_cache["mtime"] = mtime
    _users_cache["data"] = data
    return data


def _norm_ip(raw):
    """Normalize a client address to a plain IP string. Handles
    '1.2.3.4:56789', '[2001:db8::1]:56789' and IPv4-mapped IPv6."""
    if not raw:
        return ""
    ip = str(raw).strip()
    # Strip the port: last ':' separates it (IPv6 is bracketed).
    if ip.startswith("["):
        ip = ip[1:].split("]", 1)[0]
    elif ip.count(":") == 1:
        ip = ip.rsplit(":", 1)[0]
    try:
        addr = ipaddress.ip_address(ip)
        if addr.version == 6 and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        return str(addr)
    except ValueError:
        return ""


def _prune_tracker(now):
    for user in list(_tracker):
        ips = {ip: t for ip, t in _tracker[user].items() if now - t < IP_TIMEOUT}
        if ips:
            _tracker[user] = ips
        else:
            _tracker.pop(user, None)


def _save_tracker():
    tmp = ONLINE_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(_tracker, f)
        os.replace(tmp, ONLINE_PATH)
        try:
            os.chmod(ONLINE_PATH, 0o600)
        except OSError:
            pass
    except OSError:
        pass


def _rate_limited(now):
    while _failures and now - _failures[0] > FAIL_WINDOW:
        _failures.popleft()
    return len(_failures) >= FAIL_MAX


def _record_failure(now):
    _failures.append(now)


def authenticate(auth, addr):
    """Returns (ok, msg)."""
    now = int(time.time())
    if _rate_limited(now):
        return False, "Auth service busy"
    client_ip = _norm_ip(addr)
    users = _load_users()
    matched_user = None
    matched_info = None
    if isinstance(auth, str) and auth:
        for username, info in users.items():
            if not isinstance(info, dict):
                continue
            pw = info.get("password")
            if isinstance(pw, str) and pw and hmac.compare_digest(pw.encode(), auth.encode()):
                matched_user, matched_info = username, info
                break
    if matched_info is None:
        _record_failure(now)
        return False, "User not found"
    expires_at = matched_info.get("expires_at")
    try:
        expires_at = int(expires_at) if expires_at is not None else None
    except (TypeError, ValueError):
        expires_at = None
    if expires_at is not None and now >= expires_at:
        _record_failure(now)
        return False, "User account expired"
    try:
        ip_limit = max(0, int(matched_info.get("ip_limit") or 0))
    except (TypeError, ValueError):
        ip_limit = 0
    with _lock:
        _prune_tracker(now)
        if ip_limit > 0 and client_ip:
            active = _tracker.get(matched_user, {})
            if client_ip not in active and len(active) >= ip_limit:
                _record_failure(now)
                return False, "Concurrent IP limit exceeded (%d max)" % ip_limit
            active[client_ip] = now
            _tracker[matched_user] = active
        elif client_ip:
            active = _tracker.get(matched_user, {})
            active[client_ip] = now
            _tracker[matched_user] = active
        _save_tracker()
    return True, ""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _is_loopback(self):
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            return False
        # Hysteria's masquerade proxies public traffic onto loopback;
        # a forwarded request is not the Hysteria core talking to us.
        for name in ("X-Forwarded-For", "X-Forwarded-Host",
                     "X-Forwarded-Proto", "Forwarded"):
            if name in self.headers:
                return False
        return True

    def _reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._is_loopback():
            return self._reply(200, {"ok": False, "msg": "Forbidden"})
        if self.path == "/health":
            return self._reply(200, {"ok": True})
        if self.path == "/online":
            with _lock:
                _prune_tracker(int(time.time()))
                return self._reply(200, {"ok": True, "online": _tracker})
        return self._reply(404, {"ok": False, "msg": "not found"})

    def do_POST(self):
        if self.path != "/auth":
            return self._reply(404, {"ok": False, "msg": "not found"})
        if not self._is_loopback():
            return self._reply(200, {"ok": False, "msg": "Forbidden"})
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            return self._reply(400, {"ok": False, "msg": "Invalid Content-Length"})
        if not 0 <= length <= 1024 * 1024:
            return self._reply(413, {"ok": False, "msg": "Request body too large"})
        try:
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            req = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return self._reply(400, {"ok": False, "msg": "Invalid JSON"})
        if not isinstance(req, dict):
            return self._reply(400, {"ok": False, "msg": "Invalid request"})
        auth = req.get("auth", "")
        addr = req.get("addr", "")
        if not isinstance(auth, str) or not isinstance(addr, str):
            return self._reply(400, {"ok": False, "msg": "Invalid request"})
        ok, msg = authenticate(auth, addr)
        return self._reply(200, {"ok": ok, "msg": msg} if not ok else {"ok": True})


if __name__ == "__main__":
    # Load any persisted tracker so a restart does not forget recent IPs.
    try:
        with open(ONLINE_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict):
            now = int(time.time())
            for user, ips in data.items():
                if isinstance(ips, dict):
                    kept = {ip: t for ip, t in ips.items()
                            if isinstance(t, (int, float)) and now - t < IP_TIMEOUT}
                    if kept:
                        _tracker[user] = kept
    except (OSError, ValueError):
        pass
    ThreadingHTTPServer(LISTEN, Handler).serve_forever()
