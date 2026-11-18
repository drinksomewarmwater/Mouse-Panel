#!/bin/bash
# Mouse Panel 远端 agent 一键安装脚本
# 在一台已经装好 sing-box（VLESS）与 Hysteria 2 的服务器上运行，
# 把它接进某台 Mouse Panel（作为“远程服务器”），面板就能读它的
# 节点链接、流量、证书，并能轮换它的凭证、管理它的 HY2 用户。
#
# 用法（在目标服务器上以 root 运行）：
#   curl -fsSL https://raw.githubusercontent.com/drinksomewarmwater/Mouse-Panel/main/agent/install-agent.sh | bash
#
# 需要先准备好两样东西：
#   ① 面板服务器的 SSH 公钥（在面板 → 服务器设置里查看并复制）；
#   ② 面板服务器的公网 IP（用于把这把钥匙限定在只允许该 IP 连入，可空）。
# 脚本可重复运行（幂等）。
set -euo pipefail

REPO="drinksomewarmwater/Mouse-Panel"
BRANCH="main"
RAW="https://raw.githubusercontent.com/${REPO}/${BRANCH}"

info() { echo -e "\033[1;34m[Mouse-Panel agent]\033[0m $*"; }
ok()   { echo -e "\033[1;32m[完成]\033[0m $*"; }
warn() { echo -e "\033[1;33m[注意]\033[0m $*" >&2; }
die()  { echo -e "\033[1;31m[失败]\033[0m $*" >&2; exit 1; }

prompt() {
  local var="$1" text="$2" def="$3" ans=""
  if [ -n "$def" ]; then
    printf '%s [%s]: ' "$text" "$def" > /dev/tty
  else
    printf '%s: ' "$text" > /dev/tty
  fi
  IFS= read -r ans < /dev/tty || ans=""
  [ -z "$ans" ] && ans="$def"
  printf -v "$var" '%s' "$ans"
}

[ "$(id -u)" = "0" ] || die "请用 root 用户运行。"
[ -c /dev/tty ] || die "安装需要交互输入，请在终端里运行（不要重定向 stdin）。"
command -v curl >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq curl; }

# ---------- 依赖与节点检查 ----------
export DEBIAN_FRONTEND=noninteractive
if ! command -v python3 >/dev/null 2>&1 || ! command -v vnstat >/dev/null 2>&1 \
   || ! command -v jq >/dev/null 2>&1; then
  info "安装依赖（python3、vnstat、jq）…"
  apt-get update -qq || die "apt update 失败。"
  apt-get install -y -qq python3 vnstat jq iproute2 || die "依赖安装失败。"
fi
[ -f /etc/sing-box/config.json ] || warn "没找到 /etc/sing-box/config.json：这台机还没装 VLESS 节点，agent 只能返回空数据。先装节点，或先跑仓库根目录的 install.sh。"
[ -f /etc/hysteria/config.yaml ] || warn "没找到 /etc/hysteria/config.yaml：这台机还没装 Hysteria 2 节点，agent 只能返回空数据。"
IFACE=$(ip route 2>/dev/null | grep -m1 '^default' | awk '{print $5}')
[ -z "$IFACE" ] && IFACE="eth0"

# ---------- 交互提问 ----------
info "把这台服务器接进 Mouse Panel，需要确认几项信息。"
OLD_DOMAIN=""; OLD_VPORT="443"; OLD_HPORT="443"; OLD_SEC="reality"; OLD_CERT=""
if [ -f /etc/mouse-panel-agent.conf ]; then
  # shellcheck disable=SC1091
  OLD_DOMAIN=$(grep -E '^DOMAIN=' /etc/mouse-panel-agent.conf | cut -d= -f2- | tr -d '"'"'")
  OLD_VPORT=$(grep -E '^VLESS_PORT=' /etc/mouse-panel-agent.conf | cut -d= -f2-)
  OLD_HPORT=$(grep -E '^HY2_PORT=' /etc/mouse-panel-agent.conf | cut -d= -f2-)
  OLD_SEC=$(grep -E '^VLESS_SECURITY=' /etc/mouse-panel-agent.conf | cut -d= -f2-)
  OLD_CERT=$(grep -E '^SINGBOX_CERT=' /etc/mouse-panel-agent.conf | cut -d= -f2-)
fi
# 节点域名：优先从 sing-box / hysteria 配置里已有的域名推断，没推断到再问。
if [ -z "$OLD_DOMAIN" ] && [ -f /etc/sing-box/config.json ]; then
  OLD_DOMAIN=$(grep -oE '"server_name"[[:space:]]*:[[:space:]]*"[^"]+"' /etc/sing-box/config.json | head -1 | cut -d'"' -f4)
  [ "$OLD_DOMAIN" = "www.apple.com" ] && OLD_DOMAIN=""
fi
while :; do
  prompt NODE_DOMAIN "① 本机节点域名（VLESS / HY2 共用，如 node2.example.com）" "$OLD_DOMAIN"
  case "$NODE_DOMAIN" in
    *.*) break ;;
    *) warn "域名格式不对，请重新输入。" ;;
  esac
done
prompt VLESS_PORT "② VLESS 端口" "${OLD_VPORT:-443}"
prompt HY2_PORT "③ Hysteria 2 端口" "${OLD_HPORT:-443}"
prompt VLESS_SEC "④ VLESS 安全方式（reality / tls）" "${OLD_SEC:-reality}"
case "$VLESS_SEC" in reality|tls) ;; *) die "安全方式只能是 reality 或 tls。" ;; esac
DEF_CERT="${OLD_CERT:-/root/.acme.sh/${NODE_DOMAIN}_ecc/fullchain.cer}"
prompt SINGBOX_CERT "⑤ 节点证书路径（用于显示到期时间，可空用默认）" "$DEF_CERT"
[ -n "$SINGBOX_CERT" ] || SINGBOX_CERT="/etc/sing-box/certs/fullchain.cer"

echo
echo "请粘贴面板服务器的 SSH 公钥（一整行，以 ssh-ed25519 开头）："
echo "（在面板 → 服务器设置 → 添加新 VPS 里查看并复制）"
printf '面板 SSH 公钥: ' > /dev/tty
IFS= read -r PANEL_PUBKEY < /dev/tty || PANEL_PUBKEY=""
PANEL_PUBKEY=$(echo "$PANEL_PUBKEY" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
case "$PANEL_PUBKEY" in
  ssh-ed25519\ *|ssh-rsa\ *|ecdsa-*) ;;
  *) die "公钥格式不对（应以 ssh-ed25519 开头的一整行）。" ;;
esac
prompt PANEL_IP "面板服务器公网 IP（限定这把钥匙只允许该 IP，可空表示不限）" ""

# ---------- 部署 agent 文件 ----------
info "部署 agent 文件…"
TMPD=$(mktemp -d)
for f in remote-agent.py hy2_auth_sync.py vless_sync.py remote-agent-gate.sh vless_stats.py; do
  curl -fsSL --max-time 60 -o "$TMPD/$f" "${RAW}/agent/$f" \
    || die "下载 $f 失败：${RAW}/agent/$f"
done
install -m 755 "$TMPD/remote-agent.py" /usr/local/bin/remote-agent.py
install -m 755 "$TMPD/hy2_auth_sync.py" /usr/local/bin/hy2_auth_sync.py
install -m 755 "$TMPD/vless_sync.py" /usr/local/bin/vless_sync.py
install -m 755 "$TMPD/remote-agent-gate.sh" /usr/local/bin/remote-agent-gate.sh
install -m 755 "$TMPD/vless_stats.py" /usr/local/bin/vless_stats.py
# v2ray_api protobuf stubs (patched for system grpcio/protobuf) for vless_stats.py
mkdir -p /usr/local/lib/mouse-panel/v2pb/app/stats/command /usr/local/lib/mouse-panel/v2pb/common/protoext
for f in app/stats/command/command_pb2.py app/stats/command/command_pb2_grpc.py common/protoext/extensions_pb2.py; do
  curl -fsSL --max-time 60 -o "/usr/local/lib/mouse-panel/v2pb/$f" "${RAW}/api/v2pb/$f" \
    || die "下载 v2pb/$f 失败：${RAW}/api/v2pb/$f"
done
touch /usr/local/lib/mouse-panel/v2pb/app/__init__.py /usr/local/lib/mouse-panel/v2pb/app/stats/__init__.py /usr/local/lib/mouse-panel/v2pb/app/stats/command/__init__.py /usr/local/lib/mouse-panel/v2pb/common/__init__.py /usr/local/lib/mouse-panel/v2pb/common/protoext/__init__.py
apt-get install -y -qq python3-grpcio python3-protobuf 2>&1 | tail -1 || warn "python3-grpcio 安装失败，VLESS 统计将不可用。"
rm -rf "$TMPD"

cat > /etc/mouse-panel-agent.conf <<EOF
DOMAIN=$NODE_DOMAIN
VLESS_PORT=$VLESS_PORT
HY2_PORT=$HY2_PORT
VLESS_SECURITY=$VLESS_SEC
VNSTAT_IFACE=$IFACE
SINGBOX_CERT=$SINGBOX_CERT
EOF
ok "agent 配置已写入 /etc/mouse-panel-agent.conf。"

# ---------- 授权钥匙（带强制命令限制，只能跑面板用的 5 条命令） ----------
mkdir -p /root/.ssh && chmod 700 /root/.ssh
touch /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys
KEY_BODY=$(echo "$PANEL_PUBKEY" | awk '{print $1" "$2}')
if grep -qF "$KEY_BODY" /root/.ssh/authorized_keys 2>/dev/null; then
  info "这把面板公钥已在 authorized_keys 里，跳过添加。"
else
  RESTRICT='command="/usr/local/bin/remote-agent-gate.sh",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty'
  [ -n "$PANEL_IP" ] && RESTRICT="from=\"$PANEL_IP\",$RESTRICT"
  printf '%s %s\n' "$RESTRICT" "$PANEL_PUBKEY" >> /root/.ssh/authorized_keys
  ok "面板公钥已加入 authorized_keys（仅允许面板的 7 条命令，拒绝交互 shell）。"
fi
[ -n "$PANEL_IP" ] || warn "未限定来源 IP：这把钥匙可从任意 IP 连入，建议补填面板服务器 IP 后重跑。"

# ---------- 验证 ----------
systemctl enable --now vnstat >/dev/null 2>&1 || true
vnstat -u -i "$IFACE" >/dev/null 2>&1 || true
READ_OUT=$(python3 /usr/local/bin/remote-agent.py read 2>&1) \
  || die "agent 自检失败：$READ_OUT"
echo "$READ_OUT" | python3 -c "import json,sys; d=json.load(sys.stdin); assert 'vless_uuid' in d, 'bad read output'" \
  || die "agent 自检输出不是有效 JSON：$READ_OUT"
ok "agent 自检通过：$(echo "$READ_OUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('domain',''), 'VLESS', d.get('vless_port'), 'HY2', d.get('hy2_port'))" 2>/dev/null)"

echo
echo "=============================================="
echo "  远端 agent 安装完成"
echo "=============================================="
echo "  本机节点域名：$NODE_DOMAIN"
echo "  VLESS：TCP $VLESS_PORT（$VLESS_SEC）"
echo "  Hysteria 2：UDP $HY2_PORT"
echo
echo "  接下来在面板里：服务器设置 → 添加新 VPS，"
echo "  填这台服务器的地址与 SSH 端口，面板会先测连通性，"
echo "  通过后就能在这里切换、读流量、轮换凭证。"
echo "=============================================="
