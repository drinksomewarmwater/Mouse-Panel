#!/bin/bash
# Mouse Panel 一键安装脚本
# 在一台全新的 Debian 11/12 或 Ubuntu 22.04/24.04 服务器上，一条命令装好
# sing-box（VLESS + Reality）+ Hysteria 2 + Caddy + 节点面板。
#
# 用法：
#   curl -fsSL https://raw.githubusercontent.com/drinksomewarmwater/Mouse-Panel/main/install.sh | bash
# 或下载后执行：
#   bash install.sh
#
# 要求：root 用户运行；节点域名与面板子域名须已解析到本机公网 IP。
# 安装时依次提问：节点域名、面板域名、ACME 邮箱、每月额度、面板端口、
# 节点端口。面板端口默认 8443，节点端口（VLESS 与 HY2 共用）默认 443；
# 两个端口都可自填任意 1-65535 的端口（自填最优先）；回车用默认，默认端口
# 被别的服务占用时自动挑一个 10000-19999 的空闲端口。
# 80 端口须空闲（acme.sh standalone 签证书用）。
# 脚本可重复运行（幂等）：已装好的组件会跳过，已有的配置与证书会复用，
# 重跑时端口默认沿用上次的选择。
# 流程：先拉取仓库，再检查并安装环境，最后部署（部署用的文件全部来自这一次拉取）。
# 全程不会把真实密码写进任何日志文件；生成的 UUID / 密码只在终端显示一次，
# VLESS 链接写进 /var/lib/mouse-panel/links.json（0600）。
set -euo pipefail

REPO="drinksomewarmwater/Mouse-Panel"
BRANCH="main"
PANEL_DIR="/var/www/mouse-panel"
DATA_DIR="/var/lib/mouse-panel"
SB_VER_FALLBACK="v1.14.4"
HY_VER_FALLBACK="app/v2.13.0"

info()  { echo -e "\033[1;34m[Mouse-Panel]\033[0m $*"; }
ok()    { echo -e "\033[1;32m[完成]\033[0m $*"; }
warn()  { echo -e "\033[1;33m[注意]\033[0m $*" >&2; }
die()   { echo -e "\033[1;31m[失败]\033[0m $*" >&2; exit 1; }

# 交互提问：从 /dev/tty 读，这样 curl | bash 也能正常输入。
# 用法：prompt VAR "提示文字" "默认值"
# 免交互安装（借鉴 hysteria2-installer 的 HY2_* 环境变量做法）：设置
# MP_<VAR>（如 MP_NODE_DOMAIN）可跳过对应提问，全部设好即全程不交互。
# 例：MP_NODE_DOMAIN=node.example.com MP_PANEL_DOMAIN=panel.example.com MP_QUOTA_GB=2000 bash install.sh
prompt() {
  local var="$1" text="$2" def="$3" ans=""
  local envvar="MP_$var" envval=""
  if [[ -v $envvar ]]; then envval="${!envvar}"; fi
  if [ -n "$envval" ]; then
    printf -v "$var" '%s' "$envval"
    return 0
  fi
  if [ -n "$def" ]; then
    printf '%s [%s]: ' "$text" "$def" > /dev/tty
  else
    printf '%s: ' "$text" > /dev/tty
  fi
  IFS= read -r ans < /dev/tty || ans=""
  [ -z "$ans" ] && ans="$def"
  printf -v "$var" '%s' "$ans"
}

# 随机取一个 10000-19999 内的空闲端口。用法：
#   pick_free_port [exclude_port] [need_udp]
# exclude_port：跳过该端口（节点端口不能与面板端口撞号）；
# need_udp=1：同时要求 UDP 空闲（VLESS 走 TCP、HY2 走 UDP，共用一个端口号）。
# 最多试 20 次，取不到返回 1。
pick_free_port() {
  local exclude="${1:-}" need_udp="${2:-0}" i p
  for i in $(seq 1 20); do
    p=$((10000 + RANDOM % 10000))
    [ -n "$exclude" ] && [ "$p" = "$exclude" ] && continue
    ss -ltn 2>/dev/null | grep -q ":$p " && continue
    if [ "$need_udp" = "1" ]; then
      ss -lun 2>/dev/null | grep -q ":$p " && continue
    fi
    echo "$p"
    return 0
  done
  return 1
}

# 端口是否正由本项目自己的服务占用（重跑安装时）：sing-box / hysteria / caddy。
port_held_by_project() {
  local proto="$1" port="$2" holder
  if [ "$proto" = "tcp" ]; then
    holder=$(ss -ltnp 2>/dev/null | grep ":$port " | sed -E 's/.*users:\(\("([^"]+)".*/\1/' | sort -u | tr '\n' ' ')
  else
    holder=$(ss -lunp 2>/dev/null | grep ":$port " | sed -E 's/.*users:\(\("([^"]+)".*/\1/' | sort -u | tr '\n' ' ')
  fi
  case " $holder " in *" sing-box "*|*" hysteria "*|*" caddy "*) return 0 ;; *) return 1 ;; esac
}

# 面板端口是否可用：TCP 空闲，或正由 Caddy 占用（重跑）。
panel_port_ok() {
  local p="$1"
  if ss -ltn 2>/dev/null | grep -q ":$p "; then
    port_held_by_project tcp "$p" && return 0
    return 1
  fi
  return 0
}

# 节点端口是否可用：TCP 与 UDP 都要空闲，或正由本项目服务占用（重跑）。
node_port_ok() {
  local p="$1"
  if ss -ltn 2>/dev/null | grep -q ":$p "; then
    port_held_by_project tcp "$p" || return 1
  fi
  if ss -lun 2>/dev/null | grep -q ":$p "; then
    port_held_by_project udp "$p" || return 1
  fi
  return 0
}

# 是/否提问，默认 No；返回 0 表示 yes。
confirm() {
  local text="$1" ans=""
  printf '%s [y/N]: ' "$text" > /dev/tty
  IFS= read -r ans < /dev/tty || ans=""
  case "$ans" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

valid_domain() {
  # 域名：字母数字、点、横线，必须含一个点，首尾不能是点或横线。
  case "$1" in
    *..*|.*|*.|*-*.*|*.-*) return 1 ;;
  esac
  printf '%s' "$1" | grep -Eq '^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$'
}

# ---------- 前置检查 ----------
[ "$(id -u)" = "0" ] || die "请用 root 用户运行（sudo -i 后再执行）。"
[ -r /etc/os-release ] || die "无法识别操作系统。"
. /etc/os-release
case "$ID-$VERSION_ID" in
  debian-11|debian-12|ubuntu-22.04|ubuntu-24.04) ;;
  *) die "仅支持 Debian 11/12 与 Ubuntu 22.04/24.04，当前是 $ID $VERSION_ID。" ;;
esac
command -v curl >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq curl; }
command -v systemctl >/dev/null 2>&1 || die "需要 systemd。"

# ---------- 交互提问 ----------
[ -c /dev/tty ] || die "安装需要交互输入域名，请在终端里运行（不要重定向 stdin）。"
info "安装 Mouse Panel，需要先确认几个信息。"
# ---------- 域名解析提示 ----------
# 问域名之前先亮出本机公网 IP，并告诉用户怎么加解析；
# 拿不到 IP 也不卡住，只提示去 VPS 控制台看（后面的 DNS 预检会再核对一次）。
SHOW_IP4=$(curl -fsSL --max-time 5 https://api.ipify.org 2>/dev/null || true)
SHOW_IP6=$(curl -fsSL --max-time 5 https://api64.ipify.org 2>/dev/null || true)
case "$SHOW_IP6" in *:* ) ;; *) SHOW_IP6="" ;; esac
if [ -z "$SHOW_IP4" ]; then
  SHOW_IP4=$(hostname -I 2>/dev/null | awk '{print $1}')
fi
echo
if [ -n "$SHOW_IP4" ]; then
  info "本机公网 IPv4：$SHOW_IP4"
  [ -n "$SHOW_IP6" ] && info "本机公网 IPv6：$SHOW_IP6"
  IP_HINT="上面这个 IPv4"
  IP6_HINT="上面的 IPv6"
else
  warn "未能自动获取到公网 IP，请到 VPS 控制台查看。"
  IP_HINT="本机公网 IPv4"
  IP6_HINT="本机公网 IPv6"
fi
echo "域名解析：去域名 DNS 服务商处，给节点域名和面板域名各加一条 A 记录，"
echo "指向 $IP_HINT；有 IPv6 的话，再各加一条 AAAA 记录指向 $IP6_HINT。"
echo "解析一般几分钟生效，生效后再继续安装。"
echo
# 重跑时沿用已有配置作为默认值。
OLD_NODE=""; OLD_PANEL=""; OLD_QUOTA="2000"; OLD_PANEL_PORT=""; OLD_NODE_PORT=""
if [ -f "$DATA_DIR/node.json" ] && command -v jq >/dev/null 2>&1; then
  OLD_NODE=$(jq -r '.domain // empty' "$DATA_DIR/node.json" 2>/dev/null)
  OLD_PANEL=$(jq -r '.panel_domain // empty' "$DATA_DIR/node.json" 2>/dev/null)
  OLD_QUOTA=$(jq -r '.quota_gb // 2000' "$DATA_DIR/node.json" 2>/dev/null)
  OLD_PANEL_PORT=$(jq -r '.panel_port // empty' "$DATA_DIR/node.json" 2>/dev/null)
  OLD_NODE_PORT=$(jq -r '.vless_port // empty' "$DATA_DIR/node.json" 2>/dev/null)
fi
# 环境变量先校验，不合法直接退出（不回落到交互提问，免得免交互安装半路卡住）。
if [ -n "${MP_NODE_DOMAIN:-}" ] && ! valid_domain "$MP_NODE_DOMAIN"; then die "MP_NODE_DOMAIN 域名格式不对：$MP_NODE_DOMAIN"; fi
if [ -n "${MP_PANEL_DOMAIN:-}" ] && ! valid_domain "$MP_PANEL_DOMAIN"; then die "MP_PANEL_DOMAIN 域名格式不对：$MP_PANEL_DOMAIN"; fi
if [ -n "${MP_QUOTA_GB:-}" ]; then case "$MP_QUOTA_GB" in *[!0-9]*) die "MP_QUOTA_GB 须为数字（GB，0 = 不限）。";; esac; fi
if [ -n "${MP_PANEL_PORT:-}" ]; then case "$MP_PANEL_PORT" in *[!0-9]*) die "MP_PANEL_PORT 须为数字（1-65535）。";; *) if [ "$MP_PANEL_PORT" -lt 1 ] || [ "$MP_PANEL_PORT" -gt 65535 ]; then die "MP_PANEL_PORT 须在 1-65535 之间。"; fi;; esac; fi
if [ -n "${MP_NODE_PORT:-}" ]; then case "$MP_NODE_PORT" in *[!0-9]*) die "MP_NODE_PORT 须为数字（1-65535）。";; *) if [ "$MP_NODE_PORT" -lt 1 ] || [ "$MP_NODE_PORT" -gt 65535 ]; then die "MP_NODE_PORT 须在 1-65535 之间。"; fi;; esac; fi
while :; do
  prompt NODE_DOMAIN "① 节点域名（VLESS / HY2 共用，须已解析到本机 IP）" "$OLD_NODE"
  if valid_domain "$NODE_DOMAIN"; then break; fi
  warn "域名格式不对，请重新输入（例如 node.example.com）。"
done
# 面板子域名默认 panel.<节点域名去掉首段>，也可直接输完整域名。
BASE_DOMAIN="${NODE_DOMAIN#*.}"
[ "$BASE_DOMAIN" = "$NODE_DOMAIN" ] && BASE_DOMAIN="$NODE_DOMAIN"
DEF_PANEL="${OLD_PANEL:-panel.$BASE_DOMAIN}"
while :; do
  prompt PANEL_DOMAIN "② 面板域名（默认 panel.$BASE_DOMAIN）" "$DEF_PANEL"
  if valid_domain "$PANEL_DOMAIN"; then break; fi
  warn "域名格式不对，请重新输入（例如 panel.example.com）。"
done
prompt ACME_EMAIL "③ ACME 邮箱（可选，用于证书到期通知，可空）" ""
while :; do
  prompt QUOTA_GB "④ 每月流量额度（GB，0 = 不限）" "$OLD_QUOTA"
  case "$QUOTA_GB" in ''|*[!0-9]*) warn "请输入数字（GB），0 表示不限。";;
    *) break ;;
  esac
done
[ -z "$QUOTA_GB" ] && QUOTA_GB=2000
# ⑤ 面板端口：默认 8443，可自填任意 1-65535。
# 重跑时默认沿用上次的端口。回车（不填）时：默认端口空闲就用它；被别的
# 服务占了就自动挑一个 10000-19999 的空闲随机端口（不重填、不退出）；
# 被 Caddy 占用（重跑）则沿用。
DEF_PANEL_PORT="${OLD_PANEL_PORT:-8443}"
if [ -n "${MP_PANEL_PORT:-}" ]; then
  PANEL_PORT="$MP_PANEL_PORT"
  if ! panel_port_ok "$PANEL_PORT"; then die "MP_PANEL_PORT 指定的 TCP $PANEL_PORT 端口已被占用。"; fi
  info "面板端口（环境变量）：$PANEL_PORT"
else
while :; do
  printf '⑤ 面板端口（默认 %s，可自填；回车用默认，被占自动改随机）: ' "$DEF_PANEL_PORT" > /dev/tty
  IFS= read -r _panel_ans < /dev/tty || _panel_ans=""
  if [ -z "$_panel_ans" ]; then
    PANEL_PORT="$DEF_PANEL_PORT"
    if ! panel_port_ok "$PANEL_PORT"; then
      PANEL_PORT=$(pick_free_port) || die "默认端口 $DEF_PANEL_PORT 被占用，且取不到空闲面板端口，请检查端口占用后重跑。"
      info "$DEF_PANEL_PORT 被占用，面板端口自动改为 $PANEL_PORT。"
    fi
    break
  fi
  PANEL_PORT="$_panel_ans"
  case "$PANEL_PORT" in *[!0-9]*) warn "请输入数字（1-65535）。";;
    *) if [ "$PANEL_PORT" -lt 1 ] || [ "$PANEL_PORT" -gt 65535 ]; then warn "端口须在 1-65535 之间。";
       elif ! panel_port_ok "$PANEL_PORT"; then warn "TCP $PANEL_PORT 端口已被其他服务占用，请换一个。";
       else break; fi ;;
  esac
done
fi
# ⑥ 节点端口：VLESS（TCP）与 HY2（UDP）共用一个端口号，默认 443，可自填任意 1-65535。
# 重跑时默认沿用上次的端口。节点端口不能与面板端口撞号。
# 回车（不填）时：默认端口空闲就用它；被别的服务占了就自动挑一个
# 10000-19999 的空闲随机端口（不重填、不退出）；被本项目服务占用（重跑）则沿用。
DEF_NODE_PORT="${OLD_NODE_PORT:-443}"
if [ -n "${MP_NODE_PORT:-}" ]; then
  NODE_PORT="$MP_NODE_PORT"
  if [ "$NODE_PORT" = "$PANEL_PORT" ]; then die "MP_NODE_PORT 不能与面板端口（$PANEL_PORT）相同。"; fi
  if ! node_port_ok "$NODE_PORT"; then die "MP_NODE_PORT 指定的端口 $NODE_PORT 已被占用（TCP 或 UDP）。"; fi
  info "节点端口（环境变量）：$NODE_PORT"
else
while :; do
  printf '⑥ 节点端口（VLESS/HY2 共用，默认 %s，可自填；回车用默认，被占自动改随机）: ' "$DEF_NODE_PORT" > /dev/tty
  IFS= read -r _node_ans < /dev/tty || _node_ans=""
  if [ -z "$_node_ans" ]; then
    NODE_PORT="$DEF_NODE_PORT"
    if ! node_port_ok "$NODE_PORT"; then
      NODE_PORT=$(pick_free_port "$PANEL_PORT" 1) || die "默认端口 $DEF_NODE_PORT 被占用，且取不到空闲节点端口，请检查端口占用后重跑。"
      info "$DEF_NODE_PORT 被占用，节点端口自动改为 $NODE_PORT。"
    fi
    break
  fi
  NODE_PORT="$_node_ans"
  case "$NODE_PORT" in *[!0-9]*) warn "请输入数字（1-65535）。";;
    *) if [ "$NODE_PORT" -lt 1 ] || [ "$NODE_PORT" -gt 65535 ]; then warn "端口须在 1-65535 之间。";
       elif [ "$NODE_PORT" = "$PANEL_PORT" ]; then warn "节点端口不能与面板端口（$PANEL_PORT）相同，请换一个。";
       elif ! node_port_ok "$NODE_PORT"; then warn "端口 $NODE_PORT 已被其他服务占用（TCP 或 UDP），请换一个。";
       else break; fi ;;
  esac
done
fi
if [ "$NODE_PORT" != "443" ]; then
  info "节点端口：$NODE_PORT（非 443；VLESS Reality 在非 443 端口上与正常 HTTPS 流量相似度略低，但功能完全一致，个人使用无影响）。"
fi
info "节点域名：$NODE_DOMAIN"
info "面板域名：$PANEL_DOMAIN"
info "面板端口：$PANEL_PORT"
info "节点端口：$NODE_PORT"
info "每月额度：$([ "$QUOTA_GB" = "0" ] && echo 不限 || echo "${QUOTA_GB} GB")"

# ---------- DNS 与端口预检 ----------
PUB_IP=$(curl -fsSL --max-time 10 https://api.ipify.org 2>/dev/null || curl -fsSL --max-time 10 https://ifconfig.me 2>/dev/null || true)
[ -n "$PUB_IP" ] || die "取不到本机公网 IP，请检查出网。"
info "本机公网 IP：$PUB_IP"
for d in "$NODE_DOMAIN" "$PANEL_DOMAIN"; do
  RESOLVED=$(getent hosts "$d" | awk '{print $1}' | head -1)
  if [ -z "$RESOLVED" ]; then
    die "域名 $d 未解析。请先在 DNS 里把 $d 的 A 记录指到 $PUB_IP，再重跑安装。"
  fi
  if [ "$RESOLVED" != "$PUB_IP" ]; then
    warn "域名 $d 解析到 $RESOLVED，与本机公网 IP $PUB_IP 不一致，证书签发会失败。"
    confirm "仍要继续安装吗？" || die "已取消。请把 $d 的 A 记录改到 $PUB_IP 后再重跑。"
  fi
done
# :80 须空闲（acme.sh standalone 签证书用）。:443 不再预检——节点端口已在
# 上面的提问里校验（默认 443，可自填；被占用的话当场重填，不直接报错退出）。
if ss -ltn 2>/dev/null | grep -q ':80 '; then
  die "80 端口已被占用，acme.sh 无法签证书。请先停掉占用 80 端口的服务（如 nginx / apache）再重跑。"
fi

# ---------- 拉取仓库 ----------
# 先拉取，再装环境、再部署：面板代码可能引入新的依赖，只有拉到最新代码后
# 才知道这次要装什么；部署用的文件也全部来自这一次拉取，不再各处单独下载。
# curl 在前置检查里已确保可用；tar 若缺失先装上，否则解包不了。
command -v tar >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq tar; }
info "拉取仓库（${REPO}@${BRANCH}）…"
TMPD=$(mktemp -d)
trap 'rm -rf "$TMPD"' EXIT
curl -fsSL --max-time 120 -o "$TMPD/repo.tar.gz" \
  "https://github.com/${REPO}/archive/refs/heads/${BRANCH}.tar.gz" \
  || die "仓库下载失败：https://github.com/${REPO}。"
tar -xzf "$TMPD/repo.tar.gz" -C "$TMPD"
SRC="$TMPD/Mouse-Panel-${BRANCH}"
[ -d "$SRC/panel" ] || die "仓库内容不对，找不到 panel/ 目录。"
ok "仓库已拉取。"

# ---------- 系统依赖 ----------
export DEBIAN_FRONTEND=noninteractive
info "安装系统依赖…"
apt-get update -qq || die "apt update 失败，请检查软件源与网络。"
apt-get install -y -qq curl wget socat vnstat python3 python3-bcrypt sqlite3 jq \
  uuid-runtime openssl iproute2 procps ca-certificates gnupg lsb-release \
  tar cron debian-keyring debian-archive-keyring apt-transport-https \
  || die "依赖安装失败。"
timedatectl set-timezone Asia/Shanghai 2>/dev/null || true
mkdir -p "$DATA_DIR" "$PANEL_DIR/music" /etc/sing-box /etc/hysteria /etc/caddy/certs /etc/hysteria/certs

ARCH=$(uname -m)
case "$ARCH" in
  x86_64|amd64) ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) die "不支持的架构：$ARCH（仅 amd64 / arm64）。" ;;
esac
IFACE=$(ip route 2>/dev/null | grep -m1 '^default' | awk '{print $5}')
[ -z "$IFACE" ] && IFACE="eth0"

# ---------- sing-box（VLESS + Reality） ----------
# 注意：面板的 VLESS 按用户统计需要 sing-box 的 v2ray_api（官方预编译版不含
# 该功能），所以这里从源码编译，带 with_v2ray_api 标签。官方版不含
# with_musl / with_naive_outbound 也没关系，本面板用不到这两项。
if ! command -v sing-box >/dev/null 2>&1 || ! sing-box version 2>/dev/null | grep -q with_v2ray_api; then
  SB_VER=$(curl -fsSL --max-time 15 "https://api.github.com/repos/SagerNet/sing-box/releases/latest" 2>/dev/null | grep '"tag_name"' | head -1 | cut -d'"' -f4)
  [ -z "$SB_VER" ] && SB_VER="$SB_VER_FALLBACK"
  info "从源码编译 sing-box $SB_VER（+v2ray_api，用于 VLESS 按用户统计）…"
  if ! command -v go >/dev/null 2>&1; then
    info "安装 Go 工具链…"
    GO_VER="1.25.4"
    TMPD_GO=$(mktemp -d)
    curl -fsSL --max-time 180 -o "$TMPD_GO/go.tgz" "https://go.dev/dl/go${GO_VER}.linux-${ARCH}.tar.gz" \
      || die "Go 下载失败。"
    rm -rf /usr/local/go && tar -C /usr/local -xzf "$TMPD_GO/go.tgz"
    rm -rf "$TMPD_GO"
    export PATH="/usr/local/go/bin:$PATH"
  fi
  export CGO_LDFLAGS_ALLOW='-l:libcronet\.a'
  export GOCACHE="${GOCACHE:-/root/.cache/go-build}"
  go install -tags "with_gvisor,with_quic,with_dhcp,with_wireguard,with_utls,with_acme,with_clash_api,with_v2ray_api,with_tailscale,with_ccm,with_ocm,with_cloudflared,with_usbip,with_openvpn,with_openconnect" "github.com/sagernet/sing-box/cmd/sing-box@${SB_VER}" \
    || die "sing-box 编译失败（版本 $SB_VER）。"
  install -m 755 "$HOME/go/bin/sing-box" /usr/local/bin/sing-box
  ok "sing-box $SB_VER 已安装（+v2ray_api）。"
else
  ok "sing-box 已安装：$(sing-box version 2>/dev/null | head -1)"
fi
# VLESS 按用户统计：python3-grpcio + v2ray_api stubs + vless_stats.py。
apt-get install -y -qq python3-grpcio python3-protobuf 2>&1 | tail -1 || warn "python3-grpcio 安装失败，VLESS 统计将不可用。"
# v2ray_api 桩代码与统计脚本直接取自前面拉取的仓库（不再单独下载；
# 之前这里用的 ${RAW} 从未定义，set -u 下会在这里直接报错退出）。
mkdir -p /usr/local/lib/mouse-panel/v2pb/app/stats/command /usr/local/lib/mouse-panel/v2pb/common/protoext
for f in app/stats/command/command_pb2.py app/stats/command/command_pb2_grpc.py common/protoext/extensions_pb2.py; do
  [ -f "$SRC/api/v2pb/$f" ] || die "仓库里缺少 api/v2pb/$f。"
  install -m 644 "$SRC/api/v2pb/$f" "/usr/local/lib/mouse-panel/v2pb/$f"
done
touch /usr/local/lib/mouse-panel/v2pb/app/__init__.py /usr/local/lib/mouse-panel/v2pb/app/stats/__init__.py /usr/local/lib/mouse-panel/v2pb/app/stats/command/__init__.py /usr/local/lib/mouse-panel/v2pb/common/__init__.py /usr/local/lib/mouse-panel/v2pb/common/protoext/__init__.py
[ -f "$SRC/api/vless_stats.py" ] || die "仓库里缺少 api/vless_stats.py。"
install -m 755 "$SRC/api/vless_stats.py" /usr/local/bin/vless_stats.py

# 生成 VLESS UUID、Reality 密钥对与 short_id；已有配置则复用（重跑不换凭证）。
if [ -f /etc/sing-box/config.json ] && jq -e '.inbounds[] | select(.type=="vless")' /etc/sing-box/config.json >/dev/null 2>&1; then
  VLESS_UUID=$(jq -r '.inbounds[] | select(.type=="vless") | .users[0].uuid' /etc/sing-box/config.json)
  REALITY_PRIV=$(jq -r '.inbounds[] | select(.type=="vless") | .tls.reality.private_key' /etc/sing-box/config.json)
  REALITY_PUB=$(jq -r '.inbounds[] | select(.type=="vless") | .tls.reality.public_key // empty' /etc/sing-box/config.json)
  SHORT_ID=$(jq -r '.inbounds[] | select(.type=="vless") | .tls.reality.short_id | if type=="array" then .[0] else . end' /etc/sing-box/config.json)
  [ -z "$REALITY_PUB" ] && REALITY_PUB=$(echo "$REALITY_PRIV" | /usr/local/bin/sing-box generate reality-keypair --private-key 2>/dev/null | awk '/PublicKey/{print $2}')
  info "复用已有 VLESS 配置（UUID 与 Reality 密钥不变）。"
else
  VLESS_UUID=$(uuidgen 2>/dev/null || cat /proc/sys/kernel/random/uuid)
  KP=$(/usr/local/bin/sing-box generate reality-keypair)
  REALITY_PRIV=$(echo "$KP" | awk '/PrivateKey/{print $2}')
  REALITY_PUB=$(echo "$KP" | awk '/PublicKey/{print $2}')
  SHORT_ID=$(openssl rand -hex 8)
  [ -n "$VLESS_UUID" ] && [ -n "$REALITY_PRIV" ] && [ -n "$REALITY_PUB" ] || die "生成 VLESS 凭证失败。"
fi
cat > /etc/sing-box/config.json <<EOF
{
  "log": { "level": "info", "timestamp": true },
  "inbounds": [
    {
      "type": "vless",
      "tag": "vless-in",
      "listen": "::",
      "listen_port": $NODE_PORT,
      "users": [{ "uuid": "$VLESS_UUID", "flow": "xtls-rprx-vision" }],
      "tls": {
        "enabled": true,
        "server_name": "www.apple.com",
        "reality": {
          "enabled": true,
          "handshake": { "server": "www.apple.com", "server_port": 443 },
          "private_key": "$REALITY_PRIV",
          "public_key": "$REALITY_PUB",
          "short_id": "$SHORT_ID"
        }
      }
    }
  ],
  "outbounds": [{ "type": "direct", "tag": "direct" }]
}
EOF
/usr/local/bin/sing-box check -c /etc/sing-box/config.json || die "sing-box 配置校验失败。"
cat > /etc/systemd/system/sing-box.service <<EOF
[Unit]
Description=sing-box service
Documentation=https://sing-box.sagernet.org
After=network.target nss-lookup.target

[Service]
User=root
WorkingDirectory=/etc/sing-box
ExecStart=/usr/local/bin/sing-box run -c /etc/sing-box/config.json
Restart=on-failure
RestartSec=5
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now sing-box >/dev/null 2>&1 || die "sing-box 启动失败，请看 systemctl status sing-box。"
sleep 2
systemctl is-active --quiet sing-box || die "sing-box 未运行。"
ss -ltn 2>/dev/null | grep -q ":$NODE_PORT " || die "sing-box 未监听 TCP $NODE_PORT。"
ok "sing-box 运行中（VLESS + Reality，TCP $NODE_PORT）。"

# ---------- acme.sh 与证书（Let's Encrypt，standalone HTTP-01，走 80 端口） ----------
if [ ! -x /root/.acme.sh/acme.sh ]; then
  info "安装 acme.sh…"
  if [ -n "$ACME_EMAIL" ]; then
    curl -fsSL --max-time 60 https://get.acme.sh | sh -s "email=$ACME_EMAIL" >/dev/null 2>&1 \
      || die "acme.sh 安装失败。"
  else
    curl -fsSL --max-time 60 https://get.acme.sh | sh >/dev/null 2>&1 \
      || die "acme.sh 安装失败。"
  fi
  ok "acme.sh 已安装。"
fi
ACME=/root/.acme.sh/acme.sh
# 证书默认用 ECC（acme.sh 的 <域名>_ecc 目录），与面板数据脚本的读取路径一致。
for d in "$NODE_DOMAIN" "$PANEL_DOMAIN"; do
  if [ -f "/root/.acme.sh/${d}_ecc/fullchain.cer" ]; then
    info "证书已存在，跳过签发：$d"
  else
    info "签发证书：$d（Let's Encrypt，HTTP-01）…"
    "$ACME" --issue --standalone -d "$d" --keylength ec-256 --force \
      || die "证书签发失败：$d。请确认 $d 的 A 记录已指到本机（$PUB_IP）且 80 端口未被占用。"
    ok "证书已签发：$d"
  fi
done

# ---------- Hysteria 2（UDP，用户密码认证，无混淆） ----------
if ! command -v hysteria >/dev/null 2>&1; then
  HY_VER=$(curl -fsSL --max-time 15 "https://api.github.com/repos/apernet/hysteria/releases/latest" 2>/dev/null | grep '"tag_name"' | head -1 | cut -d'"' -f4)
  [ -z "$HY_VER" ] && HY_VER="$HY_VER_FALLBACK"
  info "安装 Hysteria 2 $HY_VER…"
  curl -fsSL --max-time 120 -o /usr/local/bin/hysteria \
    "https://github.com/apernet/hysteria/releases/download/${HY_VER}/hysteria-linux-${ARCH}" \
    || die "Hysteria 2 下载失败（版本 $HY_VER）。"
  chmod 755 /usr/local/bin/hysteria
  ok "Hysteria 2 $HY_VER 已安装。"
else
  ok "Hysteria 2 已安装：$(hysteria version 2>/dev/null | head -1)"
fi
# 节点证书装到固定位置供 Hysteria 读取；续期时自动重启 Hysteria。
"$ACME" --install-cert -d "$NODE_DOMAIN" --ecc \
  --key-file /etc/hysteria/certs/privkey.key \
  --fullchain-file /etc/hysteria/certs/fullchain.cer \
  --reloadcmd "systemctl restart hysteria-server || true" >/dev/null 2>&1 \
  || die "安装节点证书到 /etc/hysteria/certs 失败。"
# 初始管理员 HY2 密码与流量统计密钥；首次在面板里设置管理员后，
# 面板会重新生成 HY2 密码并同步到这里（以面板显示的链接为准）。
if [ -f /etc/hysteria/config.yaml ] && grep -q '^  userpass:' /etc/hysteria/config.yaml; then
  ADMIN_HY2_PW=$(grep -E '^[[:space:]]+admin:[[:space:]]*"' /etc/hysteria/config.yaml 2>/dev/null | head -1 | sed -E 's/.*"([^"]+)".*/\1/')
  [ -z "$ADMIN_HY2_PW" ] && ADMIN_HY2_PW=$(openssl rand -hex 16)
  info "复用已有 Hysteria 管理员密码。"
else
  ADMIN_HY2_PW=$(openssl rand -hex 16)
fi
TRAFFIC_SECRET=$(grep -oP '^  secret: "\K[^"]+' /etc/hysteria/config.yaml 2>/dev/null || true)
[ -z "$TRAFFIC_SECRET" ] && TRAFFIC_SECRET=$(openssl rand -hex 16)
cat > /etc/hysteria/config.yaml <<EOF
listen: :$NODE_PORT

tls:
  cert: /etc/hysteria/certs/fullchain.cer
  key: /etc/hysteria/certs/privkey.key

auth:
  type: userpass
  userpass:
    admin: "$ADMIN_HY2_PW"

trafficStats:
  listen: 127.0.0.1:19996
  secret: "$TRAFFIC_SECRET"
EOF
cat > /etc/systemd/system/hysteria-server.service <<EOF
[Unit]
Description=Hysteria 2 server
Documentation=https://hysteria.network
After=network.target nss-lookup.target

[Service]
User=root
WorkingDirectory=/etc/hysteria
ExecStart=/usr/local/bin/hysteria server -c /etc/hysteria/config.yaml
Restart=on-failure
RestartSec=5
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now hysteria-server >/dev/null 2>&1 || die "Hysteria 2 启动失败，请看 systemctl status hysteria-server。"
sleep 2
systemctl is-active --quiet hysteria-server || die "Hysteria 2 未运行。"
ss -lun 2>/dev/null | grep -q ":$NODE_PORT " || die "Hysteria 2 未监听 UDP $NODE_PORT。"
ok "Hysteria 2 运行中（UDP $NODE_PORT）。"

# ---------- Caddy（面板，显式证书文件，不抢 80/443） ----------
if ! command -v caddy >/dev/null 2>&1; then
  info "安装 Caddy（官方 apt 源）…"
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg \
    || die "Caddy GPG 密钥下载失败。"
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null \
    || die "Caddy 软件源添加失败。"
  apt-get update -qq || die "apt update 失败（Caddy 源）。"
  apt-get install -y -qq caddy || die "Caddy 安装失败。"
  ok "Caddy 已安装。"
else
  ok "Caddy 已安装：$(caddy version 2>/dev/null | head -1)"
fi
# 面板证书装到 Caddy 可读的位置（Caddy 以 caddy 用户运行，读不了 /root/.acme.sh）。
"$ACME" --install-cert -d "$PANEL_DOMAIN" --ecc \
  --key-file /etc/caddy/certs/panel.key \
  --fullchain-file /etc/caddy/certs/panel.cer \
  --reloadcmd "systemctl reload caddy || true" >/dev/null 2>&1 \
  || die "安装面板证书到 /etc/caddy/certs 失败。"
cat > /etc/caddy/Caddyfile <<EOF
{
	auto_https disable_redirects
}
$PANEL_DOMAIN:$PANEL_PORT {
	tls /etc/caddy/certs/panel.cer /etc/caddy/certs/panel.key
	handle /traffic.json {
		root * $PANEL_DIR
		file_server
	}
	handle /traffic {
		rewrite * /traffic.html
		root * $PANEL_DIR
		file_server
	}
	handle {
		root * $PANEL_DIR
		reverse_proxy /api/* 127.0.0.1:18080
		file_server
	}
}
EOF
caddy fmt --overwrite /etc/caddy/Caddyfile >/dev/null 2>&1 || true
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile \
  || die "Caddyfile 校验失败。"
systemctl enable --now caddy >/dev/null 2>&1 || systemctl restart caddy \
  || die "Caddy 启动失败，请看 systemctl status caddy。"
sleep 2
systemctl is-active --quiet caddy || die "Caddy 未运行。"
ok "Caddy 运行中（面板 :$PANEL_PORT）。"

# ---------- 部署面板文件（用前面拉取的仓库内容） ----------
info "部署面板文件…"
install -m 644 "$SRC/panel/index.html" "$SRC/panel/traffic.html" \
  "$SRC/panel/qrcode.min.js" "$SRC/panel/favicon.svg" "$PANEL_DIR/"
[ -f "$SRC/panel/music/.gitkeep" ] && install -m 644 "$SRC/panel/music/.gitkeep" "$PANEL_DIR/music/"
install -m 755 "$SRC/api/mouse-panel-api.py" /usr/local/bin/mouse-panel-api.py
install -m 755 "$SRC/api/mouse-panel-data.sh" /usr/local/bin/mouse-panel-data.sh
install -m 755 "$SRC/api/hy2-user-poller.py" /usr/local/bin/hy2-user-poller.py
install -m 755 "$SRC/api/hy2-sync-users.py" /usr/local/bin/hy2-sync-users.py
install -m 755 "$SRC/api/hy2_auth_sync.py" /usr/local/bin/hy2_auth_sync.py
install -m 755 "$SRC/api/vless_sync.py" /usr/local/bin/vless_sync.py
install -m 755 "$SRC/api/vless-sync-users.py" /usr/local/bin/vless-sync-users.py
install -m 755 "$SRC/api/mouse-panel-alerts.py" /usr/local/bin/mouse-panel-alerts.py
install -m 755 "$SRC/agent/remote-agent.py" /usr/local/bin/remote-agent.py
install -m 755 "$SRC/agent/remote-agent-gate.sh" /usr/local/bin/remote-agent-gate.sh
install -m 755 "$SRC/api/hy2-auth.py" /usr/local/bin/hy2-auth.py
install -m 644 "$SRC/api/hy2-auth.service" /etc/systemd/system/hy2-auth.service
install -m 644 "$SRC/api/mouse-panel-api.service" /etc/systemd/system/mouse-panel-api.service
ok "面板文件已部署到 $PANEL_DIR 与 /usr/local/bin。"

# ---------- 面板数据与配置 ----------
VLESS_LINK="vless://${VLESS_UUID}@${NODE_DOMAIN}:${NODE_PORT}?security=reality&encryption=none&pbk=${REALITY_PUB}&fp=chrome&type=tcp&flow=xtls-rprx-vision&sni=www.apple.com&sid=${SHORT_ID}#Vless"
jq -n \
  --arg domain "$NODE_DOMAIN" --arg panel_domain "$PANEL_DOMAIN" \
  --argjson quota_gb "$QUOTA_GB" --arg iface "$IFACE" \
  --argjson panel_port "$PANEL_PORT" --argjson vless_port "$NODE_PORT" --argjson hy2_port "$NODE_PORT" \
  '{domain: $domain, panel_domain: $panel_domain, panel_port: $panel_port, vless_port: $vless_port, hy2_port: $hy2_port,
    reality_sni: "www.apple.com", quota_gb: $quota_gb, iface: $iface}' \
  > "$DATA_DIR/node.json"
jq -n --arg vless "$VLESS_LINK" '{vless: $vless}' > "$DATA_DIR/links.json"
chmod 600 "$DATA_DIR/links.json"
[ -f "$DATA_DIR/config.json" ] || echo '{"panel_name": "鼠鼠面板"}' > "$DATA_DIR/config.json"
[ -f "$DATA_DIR/playlist.json" ] || echo '[]' > "$DATA_DIR/playlist.json"
# 流量预警默认配置（提醒 80%、自动停用 95%；Token 在面板里填，不写进脚本）。
[ -f "$DATA_DIR/alerts.json" ] || echo '{"telegram_bot_token": "", "telegram_chat_id": "", "alert_threshold_pct": 80, "disable_threshold_pct": 95, "alert_sent_month": {}, "disabled_month": {}}' > "$DATA_DIR/alerts.json"
chmod 600 "$DATA_DIR/alerts.json" 2>/dev/null || true
# 月度额度写进 servers.json（流量预警按它判断；重装时同步更新本机条目）。
if [ -f "$DATA_DIR/servers.json" ] && command -v jq >/dev/null 2>&1; then
  jq --argjson q "$QUOTA_GB" 'map(if .type == "local" then .quota_gb = $q else . end)' "$DATA_DIR/servers.json" > "$DATA_DIR/servers.json.tmp" 2>/dev/null \
    && mv "$DATA_DIR/servers.json.tmp" "$DATA_DIR/servers.json"
fi
[ -f "$DATA_DIR/servers.json" ] || echo "[{\"id\": \"local\", \"name\": \"本机\", \"type\": \"local\", \"quota_gb\": $QUOTA_GB}]" > "$DATA_DIR/servers.json"
# 远端 agent 配置（本机被别的面板加为远程服务器时用）。
cat > /etc/mouse-panel-agent.conf <<EOF
DOMAIN=$NODE_DOMAIN
VLESS_PORT=$NODE_PORT
HY2_PORT=$NODE_PORT
VLESS_SECURITY=reality
VNSTAT_IFACE=$IFACE
SINGBOX_CERT=/root/.acme.sh/${NODE_DOMAIN}_ecc/fullchain.cer
EOF
# 面板 SSH 钥匙（加远程服务器时用；私钥只留本机，公钥由面板设置页给出）。
if [ ! -f /root/.ssh/id_ed25519_panel ]; then
  mkdir -p /root/.ssh && chmod 700 /root/.ssh
  ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519_panel -N "" -C "mouse-panel" >/dev/null 2>&1 \
    || die "面板 SSH 钥匙生成失败。"
  chmod 600 /root/.ssh/id_ed25519_panel
fi

# ---------- 面板 API 服务与定时任务 ----------
systemctl daemon-reload
# HY2 鉴权服务（到期 + IP 限制）必须先于 Hysteria 切到 HTTP 鉴权之前跑起来。
systemctl enable --now hy2-auth >/dev/null 2>&1 \
  || die "HY2 鉴权服务启动失败，请看 systemctl status hy2-auth。"
sleep 1
systemctl is-active --quiet hy2-auth || die "HY2 鉴权服务未运行。"
systemctl enable --now mouse-panel-api >/dev/null 2>&1 \
  || die "面板 API 启动失败，请看 systemctl status mouse-panel-api。"
sleep 2
systemctl is-active --quiet mouse-panel-api || die "面板 API 未运行。"
# 5 分钟 cron：数据采集 + 每用户 HY2 流量统计（已存在则不重复加）。
CRON_TMP=$(mktemp)
crontab -l 2>/dev/null | grep -v "mouse-panel-data.sh\|hy2-user-poller.py\|mouse-panel-alerts.py" > "$CRON_TMP" || true
{
  echo "*/5 * * * * /usr/local/bin/mouse-panel-data.sh"
  echo "*/5 * * * * /usr/bin/python3 /usr/local/bin/hy2-user-poller.py"
  echo "*/5 * * * * /usr/bin/python3 /usr/local/bin/mouse-panel-alerts.py >> /var/lib/mouse-panel/alerts.log 2>&1"
} >> "$CRON_TMP"
crontab "$CRON_TMP" && rm -f "$CRON_TMP"
# vnstat 初始化（流量统计用）。
systemctl enable --now vnstat >/dev/null 2>&1 || true
vnstat -u -i "$IFACE" >/dev/null 2>&1 || true
/usr/local/bin/mouse-panel-data.sh || warn "首次数据采集失败，5 分钟后 cron 会再跑一次。"

# ---------- 验证 ----------
sleep 1
PANEL_CODE=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 15 "https://127.0.0.1:$PANEL_PORT/" || echo "000")
[ "$PANEL_CODE" = "200" ] || warn "面板本地访问返回 $PANEL_CODE（服务已起，可能是证书还在生效中，稍后刷新看看）。"
for s in sing-box hysteria-server caddy mouse-panel-api; do
  systemctl is-active --quiet "$s" || die "服务 $s 未运行，安装未完成。"
done

# ---------- 安装结果 ----------
echo
echo "=============================================="
echo "  Mouse Panel 安装完成"
echo "=============================================="
echo "  面板地址：https://$PANEL_DOMAIN:$PANEL_PORT"
echo "  节点域名：$NODE_DOMAIN"
echo "  VLESS：TCP $NODE_PORT（Reality，SNI www.apple.com）"
echo "  Hysteria 2：UDP $NODE_PORT（用户密码认证）"
echo "  每月额度：$([ "$QUOTA_GB" = "0" ] && echo 不限 || echo "${QUOTA_GB} GB")"
echo
echo "  VLESS 链接（导入客户端即用）："
echo "  $VLESS_LINK"
echo
echo "  初始 HY2 管理员密码（只显示这一次）："
echo "  $ADMIN_HY2_PW"
echo "  首次在面板里设置管理员后，HY2 密码会重新生成，"
echo "  请以面板里显示的链接为准；VLESS 链接不受影响。"
echo
echo "  首次打开面板会让你设置管理员用户名与密码（≥6 位），"
echo "  设置完直接进入面板；之后在「用户管理」里加普通用户。"
echo
echo "  防火墙提醒（面板不自动配，须自己去云厂商控制台放行）："
echo "  节点端口 $NODE_PORT（TCP+UDP）、面板端口 $PANEL_PORT（TCP），"
echo "  再加端口范围 10000–40000（TCP，一次放开以后加用户、重装都不用再动）。"
echo "  签证书用的 80 端口装完可关。详见 README 的防火墙一节。"
echo "  卸载：见仓库 README 的卸载一节。"
echo "=============================================="
