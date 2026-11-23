#!/bin/bash
# 节点面板数据：vnstat 月流量 + 今日流量 + 证书到期 + 服务状态 -> /var/lib/mouse-panel/data.json
# 同时写一份仅含月流量的公开数据 -> /var/www/mouse-panel/traffic.json（供免登录流量页用）
# 节点信息（域名、面板域名、月额度、网卡、证书路径）来自 /var/lib/mouse-panel/node.json（install.sh 写入）。
set -u
OUT="/var/lib/mouse-panel/data.json"
TRAFFIC_OUT="/var/www/mouse-panel/traffic.json"
NODE_JSON="/var/lib/mouse-panel/node.json"
SERVERS_JSON="/var/lib/mouse-panel/servers.json"
mkdir -p /var/lib/mouse-panel /var/www/mouse-panel

NODE_DOMAIN=$(jq -r '.domain // empty' "$NODE_JSON" 2>/dev/null)
PANEL_DOMAIN=$(jq -r '.panel_domain // empty' "$NODE_JSON" 2>/dev/null)
# 月额度：node.json 为准；没有时再看 servers.json 里本机（type=local）条目；
# 两者都没有才用 2000——这只是全新安装提问时的默认值，不是某台服务器的值，
# 重跑安装时上面两步会沿用已记录的额度。
QUOTA_GB=$(jq -r '.quota_gb // empty' "$NODE_JSON" 2>/dev/null)
if [ -z "$QUOTA_GB" ] && [ -f "$SERVERS_JSON" ]; then
  QUOTA_GB=$(jq -r '.[] | select(.type=="local") | .quota_gb // empty' "$SERVERS_JSON" 2>/dev/null | head -1)
fi
[ -z "$QUOTA_GB" ] && QUOTA_GB=2000
IFACE=$(jq -r '.iface // empty' "$NODE_JSON" 2>/dev/null)
[ -z "$IFACE" ] && IFACE=$(ip route 2>/dev/null | grep -m1 '^default' | awk '{print $5}')
[ -z "$IFACE" ] && IFACE="eth0"
case "$QUOTA_GB" in ''|*[!0-9]*) QUOTA_GB=2000;; esac

MONTH_JSON=$(vnstat -i "$IFACE" --json m 1 2>/dev/null)
RX=$(echo "$MONTH_JSON" | jq -r '.interfaces[0].traffic.month[0].rx // 0')
TX=$(echo "$MONTH_JSON" | jq -r '.interfaces[0].traffic.month[0].tx // 0')
MONTH=$(echo "$MONTH_JSON" | jq -r '"\(.interfaces[0].traffic.month[0].date.year)-\(.interfaces[0].traffic.month[0].date.month)"' 2>/dev/null)
# 今日流量：按北京时间日期取 vnstat 当日条目；服务器时区已是 Asia/Shanghai，
# 每天 00:00（北京时间）vnstat 自动切到新的一天。取不到当日条目时记 0，
# 绝不用昨天的数据顶替，避免 00:00 前后几分钟显示错误。
DAY_JSON=$(vnstat -i "$IFACE" --json d 1 2>/dev/null)
Y=$(date +%Y); MO=$(date +%-m); DY=$(date +%-d)
TODAY_DATE=$(date '+%Y-%m-%d')
DAY_RX=$(echo "$DAY_JSON" | jq -r --argjson y "$Y" --argjson m "$MO" --argjson d "$DY" '(.interfaces[0].traffic.day[] | select(.date.year==$y and .date.month==$m and .date.day==$d) | .rx) // 0' 2>/dev/null)
DAY_TX=$(echo "$DAY_JSON" | jq -r --argjson y "$Y" --argjson m "$MO" --argjson d "$DY" '(.interfaces[0].traffic.day[] | select(.date.year==$y and .date.month==$m and .date.day==$d) | .tx) // 0' 2>/dev/null)
DAY_RX=${DAY_RX:-0}
DAY_TX=${DAY_TX:-0}

# 证书到期：按安装布局依次找，第一个存在的文件为准。
#   acme.sh 布局（install.sh 签发）：/root/.acme.sh/<域名>_ecc/fullchain.cer；
#   Hysteria 证书位置：/etc/hysteria/certs/fullchain.cer（acme.sh --install-cert 装过去的）；
#   sing-box 证书位置：/etc/sing-box/certs/fullchain.cer（D1 由同步脚本从 Hysteria 处复制）；
#   Hysteria 自带 ACME 布局（D1 节点证书）：/etc/hysteria/acme/certificates/*/<域名>/<域名>.crt；
#   Caddy 面板证书：/etc/caddy/certs/panel.cer（install.sh 装过去的）或该目录下任意 .cer
#   （D1 的面板证书是 /etc/caddy/certs/tz.cer）。
# 证书文件不存在时（比如刚装完还没签发）输出空日期与 null 天数，不报错。
cert_info() {
  local cer="$1"
  if [ ! -f "$cer" ]; then
    echo '{"not_after":"","days_left":null}'
    return
  fi
  local exp na days
  exp=$(openssl x509 -in "$cer" -noout -enddate 2>/dev/null | cut -d= -f2)
  if [ -z "$exp" ]; then
    echo '{"not_after":"","days_left":null}'
    return
  fi
  na=$(date -d "$exp" -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)
  days=$(( ( $(date -d "$exp" +%s) - $(date +%s) ) / 86400 ))
  echo "{\"not_after\":\"$na\",\"days_left\":$days}"
}
first_existing() {
  local f
  for f in "$@"; do
    [ -f "$f" ] && { printf '%s' "$f"; return 0; }
  done
  return 1
}
NODE_CER=$(first_existing \
  "/root/.acme.sh/${NODE_DOMAIN}_ecc/fullchain.cer" \
  /etc/hysteria/certs/fullchain.cer \
  /etc/sing-box/certs/fullchain.cer \
  /etc/hysteria/acme/certificates/*/"${NODE_DOMAIN}"/"${NODE_DOMAIN}.crt" \
  || true)
PANEL_CER=$(first_existing \
  "/root/.acme.sh/${PANEL_DOMAIN}_ecc/fullchain.cer" \
  /etc/caddy/certs/panel.cer \
  /etc/caddy/certs/*.cer \
  || true)
NODE_CERT_JSON=$(cert_info "$NODE_CER")
PANEL_CERT_JSON=$(cert_info "$PANEL_CER")

sb=$(systemctl is-active sing-box 2>/dev/null || echo unknown)
hy=$(systemctl is-active hysteria-server 2>/dev/null || echo unknown)
NOW=$(date '+%Y-%m-%d %H:%M')
jq -n \
  --arg updated "$NOW" \
  --arg domain "$NODE_DOMAIN" \
  --arg panel_domain "$PANEL_DOMAIN" \
  --argjson quota_gb "$QUOTA_GB" \
  --arg month "$MONTH" \
  --argjson rx "$RX" \
  --argjson tx "$TX" \
  --arg today_date "$TODAY_DATE" \
  --argjson day_rx "$DAY_RX" \
  --argjson day_tx "$DAY_TX" \
  --arg node_na "$(echo "$NODE_CERT_JSON" | jq -r '.not_after')" \
  --argjson node_days "$(echo "$NODE_CERT_JSON" | jq -r '.days_left')" \
  --arg panel_na "$(echo "$PANEL_CERT_JSON" | jq -r '.not_after')" \
  --argjson panel_days "$(echo "$PANEL_CERT_JSON" | jq -r '.days_left')" \
  --arg singbox "$sb" \
  --arg hysteria "$hy" \
  '{
    updated: $updated, domain: $domain, panel_domain: $panel_domain,
    quota_gb: $quota_gb,
    traffic: {month: $month, rx: $rx, tx: $tx,
              today: {date: $today_date, rx: $day_rx, tx: $day_tx}},
    certs: {
      node: {domain: $domain, not_after: $node_na, days_left: $node_days},
      panel: {domain: $panel_domain, not_after: $panel_na, days_left: $panel_days}
    },
    services: {"sing-box": $singbox, "hysteria-server": $hysteria}
  }' > "$OUT"
jq -n \
  --arg updated "$NOW" \
  --arg domain "$NODE_DOMAIN" \
  --argjson quota_gb "$QUOTA_GB" \
  --arg month "$MONTH" \
  --argjson rx "$RX" \
  --argjson tx "$TX" \
  '{updated: $updated, domain: $domain, quota_gb: $quota_gb,
    traffic: {month: $month, rx: $rx, tx: $tx}}' > "$TRAFFIC_OUT"
chmod 644 "$OUT" "$TRAFFIC_OUT"
