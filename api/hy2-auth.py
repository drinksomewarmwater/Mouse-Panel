#!/usr/bin/env python3
"""Hysteria 2 HTTP auth server for Mouse-Panel (per-user expiry only).

Hysteria calls POST /auth on every new client connection with
{"addr": "client_ip:port", "auth": "password"} and expects
{"ok": true} or {"ok": false, "msg": "..."}.

The user file /var/lib/mouse-panel/hy2-users.json is written by
hy2_auth_sync.py (enabled users only):
  {"username": {"password": "...", "expires_at": 1234567890|null}}
  - expires_at: Unix timestamp, null = permanent.

The IP-count limit was removed: VLESS cannot be counted in real time
and HY2 only records an IP at connect time, so the limit could not
actually be enforced. This service now only checks the password and
the expiry date.

Security (mirrors the upstream portal's /auth hardening):
- Only accepts requests from loopback (127.0.0.1 / ::1). Hysteria's
  masquerade can proxy public traffic onto loopback ports, so any request
  carrying X-Forwarded-For / Forwarded headers is rejected outright.
- Failed auths are rate-limited globally (30/min): the endpoint would
  otherwise be a password oracle (it distinguishes "user not found" from
  a wrong password by timing/response).

Runs as a systemd service (hy2-auth) on 127.0.0.1:18081. Stdlib only.
"""
import hmac
import json
import os
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

USERS_PATH = "/var/lib/mouse-panel/hy2-users.json"
LISTEN = ("127.0.0.1", 18081)
FAIL_WINDOW = 60   # rate-limit window (seconds)
FAIL_MAX = 30      # max failed auths per window

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


def _rate_limited(now):
    while _failures and now - _failures[0] > FAIL_WINDOW:
        _failures.popleft()
    return len(_failures) >= FAIL_MAX


def _record_failure(now):
    _failures.append(now)


def authenticate(auth):
    """Returns (ok, msg). Only the password and the expiry date are
    checked; there is no IP counting (it could not be enforced)."""
    now = int(time.time())
    if _rate_limited(now):
        return False, "Auth service busy"
    users = _load_users()
    matched_info = None
    if isinstance(auth, str) and auth:
        for username, info in users.items():
            if not isinstance(info, dict):
                continue
            pw = info.get("password")
            if isinstance(pw, str) and pw and hmac.compare_digest(pw.encode(), auth.encode()):
                matched_info = info
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
        if not isinstance(auth, str):
            return self._reply(400, {"ok": False, "msg": "Invalid request"})
        ok, msg = authenticate(auth)
        return self._reply(200, {"ok": ok, "msg": msg} if not ok else {"ok": True})


if __name__ == "__main__":
    ThreadingHTTPServer(LISTEN, Handler).serve_forever()
