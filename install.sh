#!/usr/bin/env bash
# 简易版中转助手 一行安装（在中转服务器上用 root 运行）
#   bash <(curl -fsSL https://raw.githubusercontent.com/katon4612-spec/relay-lite/main/install.sh) 你的域名
#   没有域名：后面什么都不写，自动用服务器公网 IP
# 没装 s-ui 的会自动装 s-ui 1.3.10（随机管理员密码）并建好 Reality 配置；已装的直接用。
# 再执行一次 = 升级（配置和线路都保留）；最后加 uninstall = 卸载（s-ui 里的节点不受影响）。
set -e
DIR=/opt/relay-lite
SRC="$(cd "$(dirname "$0")" && pwd)"
SVC=/etc/systemd/system/relay-lite.service
REPO="katon4612-spec/relay-lite"
SUI_VERSION="v1.3.10"

[ "$(id -u)" = "0" ] || { echo "请用 root 运行"; exit 1; }

if [ "$1" = "uninstall" ]; then
  systemctl disable --now relay-lite 2>/dev/null || true
  rm -f "$SVC"; systemctl daemon-reload
  rm -rf "$DIR"
  echo "已卸载简易版中转助手（s-ui 里已建的节点不受影响）"
  exit 0
fi

need() { command -v "$1" >/dev/null 2>&1; }

pkg_install() {
  (apt-get update -y >/dev/null 2>&1 && apt-get install -y "$@") || yum install -y "$@" || dnf install -y "$@"
}
need curl || pkg_install curl
need tar || pkg_install tar
need python3 || { echo "安装 python3 ..."; pkg_install python3; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)' || { echo "需要 Python 3.7 以上"; exit 1; }
python3 -c 'import sqlite3' || { echo "python3 缺少 sqlite3 模块"; exit 1; }

# 命令后面带了域名（或 IP）就用它；没带、又是第一次安装：自动用这台服务器的公网 IP。都不再问。
if [ -n "$1" ]; then
  export RL_PUBLIC_HOST="$1"
  export RL_HTTPS_YES=1
elif [ ! -f "$DIR/config.json" ]; then
  IP=""
  for u in https://api.ipify.org https://ifconfig.me/ip https://ipv4.icanhazip.com; do
    IP="$(curl -4 -fsS --max-time 6 "$u" 2>/dev/null | tr -d '[:space:]')" || true
    echo "$IP" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' && break
    IP=""
  done
  [ -z "$IP" ] && IP="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n1)"
  if [ -n "$IP" ]; then
    echo "没有填域名，使用本机公网 IP：$IP"
    export RL_PUBLIC_HOST="$IP"
    export RL_HTTPS_YES=1
  fi
fi

# ---------- 没装 s-ui：自动安装 s-ui 1.3.10 ----------
SUI_NEW_PASS=""
if [ ! -x /usr/local/s-ui/sui ]; then
  case "$(uname -m)" in
    x86_64|x64|amd64) ARCH=amd64 ;;
    i*86|x86) ARCH=386 ;;
    armv8*|armv8|arm64|aarch64) ARCH=arm64 ;;
    armv7*|armv7) ARCH=armv7 ;;
    armv6*|armv6) ARCH=armv6 ;;
    armv5*|armv5) ARCH=armv5 ;;
    s390x) ARCH=s390x ;;
    *) echo "不支持的 CPU 架构：$(uname -m)"; exit 1 ;;
  esac
  echo "没有找到 s-ui，自动安装 s-ui $SUI_VERSION（$ARCH）..."
  T="$(mktemp -d)"
  curl -fL --retry 3 -o "$T/s-ui.tar.gz" "https://github.com/alireza0/s-ui/releases/download/$SUI_VERSION/s-ui-linux-$ARCH.tar.gz" \
    || { echo "下载 s-ui 失败，确认服务器能访问 GitHub"; exit 1; }
  tar xzf "$T/s-ui.tar.gz" -C "$T"
  chmod +x "$T/s-ui/sui" "$T/s-ui/s-ui.sh"
  cp "$T/s-ui/s-ui.sh" /usr/bin/s-ui
  cp -rf "$T/s-ui" /usr/local/
  cp -f "$T/s-ui/"*.service /etc/systemd/system/
  rm -rf "$T"
  /usr/local/s-ui/sui migrate >/dev/null
  SUI_NEW_PASS="$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 12)"
  /usr/local/s-ui/sui admin -username admin -password "$SUI_NEW_PASS" >/dev/null
  # 面板 2095 / 订阅 2096 被别的程序（比如 x-ui）占了：自动换成空闲端口，否则 s-ui 起不来
  port_used() {
    if command -v ss >/dev/null 2>&1; then ss -lnt 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]$1\$"
    else (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; fi
  }
  pick_free() { p=$1; while port_used "$p"; do p=$((p + 100)); done; echo "$p"; }
  WP=2095; SP=2096
  port_used $WP && WP=$(pick_free 2195)
  port_used $SP && SP=$(pick_free 2196)
  [ "$SP" = "$WP" ] && SP=$(pick_free $((WP + 1)))
  if [ "$WP" != "2095" ] || [ "$SP" != "2096" ]; then
    echo "端口 2095/2096 有被占用的，s-ui 改用 面板 $WP / 订阅 $SP"
    /usr/local/s-ui/sui setting -port "$WP" -subPort "$SP" >/dev/null
  fi
  systemctl daemon-reload
  systemctl enable s-ui --now >/dev/null 2>&1
  echo -n "等待 s-ui 启动"
  SUI_UP=""
  for i in $(seq 1 60); do
    if curl -s -o /dev/null --max-time 3 "http://127.0.0.1:$WP/app/"; then SUI_UP=1; break; fi
    [ "$i" = "20" ] && systemctl restart s-ui >/dev/null 2>&1
    echo -n "."; sleep 2
  done
  echo ""
  if [ -n "$SUI_UP" ]; then
    echo "✓ s-ui $SUI_VERSION 已安装并运行"
  else
    echo "⚠ s-ui 已安装，但 2 分钟内没有响应。状态："
    systemctl status s-ui --no-pager -l 2>&1 | tail -n 15
  fi
fi

# ---------- 安装 / 升级 简易版中转助手 ----------
if [ ! -f "$SRC/app.py" ]; then   # 用一行命令在线安装时，从 GitHub 下载最新版
  TMP="$(mktemp -d)"
  URL="https://github.com/$REPO/archive/refs/heads/main.tar.gz"
  echo "下载简易版中转助手 $URL"
  curl -fsSL "$URL" | tar xz -C "$TMP"
  SRC="$TMP/relay-lite-main"
  [ -f "$SRC/app.py" ] || { echo "下载失败"; exit 1; }
fi

mkdir -p "$DIR"
if [ "$SRC" != "$DIR" ]; then
  install -m 644 "$SRC/app.py" "$SRC/qrencoder.py" "$SRC/index.html" "$SRC/configure.py" "$DIR/"
  install -m 755 "$SRC/https.sh" "$SRC/install.sh" "$DIR/"
fi

python3 "$DIR/configure.py"
bash "$DIR/https.sh" "$DIR" || true

cat > "$SVC" <<EOF
[Unit]
Description=Relay Lite (one-click relay for s-ui)
After=network.target s-ui.service

[Service]
Type=simple
WorkingDirectory=$DIR
ExecStart=$(command -v python3) $DIR/app.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable relay-lite >/dev/null 2>&1
systemctl restart relay-lite
sleep 2

cfg() { python3 -c "import json;c=json.load(open('$DIR/config.json',encoding='utf-8-sig'));print($1)"; }
PORT=$(cfg "c['port']")
WPATH=$(cfg "c['path']")
PASS=$(cfg "c['password']")
HOST=$(cfg "c['public_host']")
SUSER=$(cfg "c.get('sui_user','')")
SCHEME=$(cfg "'https' if c.get('cert_file') else 'http'")

if need ufw && ufw status | grep -q "Status: active"; then ufw allow "$PORT/tcp" >/dev/null; ufw allow 10000:60000/tcp >/dev/null; fi
if need firewall-cmd && firewall-cmd --state 2>/dev/null | grep -q running; then
  firewall-cmd --permanent --add-port="$PORT/tcp" --add-port=10000-60000/tcp >/dev/null && firewall-cmd --reload >/dev/null; fi

if systemctl is-active --quiet relay-lite; then
  echo ""
  echo "=============================================="
  echo " 简易版中转助手已运行"
  echo " 地址： $SCHEME://$HOST:$PORT$WPATH"
  echo " 账号： $SUSER"
  if [ -n "$SUI_NEW_PASS" ]; then
    echo " 密码： $SUI_NEW_PASS"
    echo "       （也是 s-ui 面板 http://$HOST:${WP:-2095}/app/ 的密码，请记好）"
  else
    echo " 密码： 和 s-ui 面板一样"
  fi
  echo " 备用密码（s-ui 连不上时用，账号留空）： $PASS"
  echo "----------------------------------------------"
  echo " 云服务器安全组需要放行：TCP $PORT（页面）、TCP 10000-60000（节点）、TCP 80（申请证书）"
  if python3 -c "import ipaddress,sys;ipaddress.ip_address(sys.argv[1])" "$HOST" 2>/dev/null; then
    echo " 用的是 IP，页面是自签名证书：浏览器提示「不安全」时点「高级 → 继续访问」即可（传输照样加密，节点不受影响）"
  fi
  echo " 配置文件：$DIR/config.json   日志：journalctl -u relay-lite -f"
  echo "=============================================="
else
  echo "启动失败，查看日志： journalctl -u relay-lite -n 50"
  exit 1
fi
