#!/usr/bin/env python3
"""Read per-user VLESS byte counters from the local sing-box v2ray_api.

The panel's sing-box is a custom build with the `with_v2ray_api` tag
(official builds do not include it). vless_sync.py enables the API on
127.0.0.1:10085 and lists every enabled user's *name* in stats.users;
each VLESS user inbound carries "name": <username>, so the counters are
named user>>><username>>>>traffic>>>uplink / downlink.

Output (stdout, JSON):
  success: {"ok": true, "users": {"alice": {"tx": 123, "rx": 456}, ...}}
  failure: {"ok": false, "error": "..."} (exit code 1)

Reading modes:
  default (no flag): cumulative counters (reset=false). Safe for manual
    checks; does not disturb the poller.
  --reset: QueryStats with reset=true. Returns the traffic since the last
    reset-read AND resets the counters to zero. ONLY the poller
    (hy2-user-poller.py) may use --reset: it writes the returned delta
    straight into usage_daily (combined HY2+VLESS). A manual --reset call
    would consume the counters and the poller would miss that traffic.
    A reset-read whose DB write then fails loses that interval's VLESS
    traffic (bounded to one 5-minute interval); a failed read does NOT
    reset anything, so the next successful read still gets the full amount.

tx/rx follow the Hysteria convention (server perspective):
  tx = server -> client = downlink (the user's download)
  rx = client -> server = uplink   (the user's upload)

Requires the v2ray_api protobuf stubs in /usr/local/lib/mouse-panel/v2pb
(patched for the system grpcio/protobuf: runtime_version check and the
_registered_method kwarg removed) and python3-grpcio (apt).
The poller (hy2-user-poller.py) calls this every 5 minutes with --reset
and writes the returned delta straight into usage_daily (the combined
HY2+VLESS table). A sing-box restart/reload also resets the counters to
zero, which a reset-read simply reports as zero delta. No per-protocol
last-seen state is stored anywhere: limits and statistics are per
server only (D1 / D2), HY2+VLESS combined.
"""
import json
import sys

V2RAY_API = "127.0.0.1:10085"
STUBS_DIR = "/usr/local/lib/mouse-panel/v2pb"

sys.path.insert(0, STUBS_DIR)

try:
    import grpc
    from app.stats.command import command_pb2, command_pb2_grpc
except Exception as e:
    print(json.dumps({"ok": False, "error": "grpc import failed: %s" % e}))
    sys.exit(1)


def main():
    reset = "--reset" in sys.argv[1:]
    try:
        channel = grpc.insecure_channel(V2RAY_API)
        stub = command_pb2_grpc.StatsServiceStub(channel)
        req = command_pb2.QueryStatsRequest(patterns=["user>>>"], reset=reset)
        resp = stub.QueryStats(req, timeout=10)
    except Exception as e:
        print(json.dumps({"ok": False, "error": "v2ray_api query failed: %s" % e}))
        return 1
    users = {}
    for s in resp.stat:
        # name: user>>><username>>>>traffic>>>uplink | downlink
        parts = s.name.split(">>>")
        if len(parts) != 4 or parts[0] != "user" or parts[2] != "traffic":
            continue
        username, direction = parts[1], parts[3]
        if direction not in ("uplink", "downlink"):
            continue
        u = users.setdefault(username, {"tx": 0, "rx": 0})
        if direction == "downlink":
            u["tx"] = int(s.value)
        else:
            u["rx"] = int(s.value)
    print(json.dumps({"ok": True, "users": users}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
