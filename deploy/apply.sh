#!/usr/bin/env bash
# Install/refresh the host-level configuration of the summarizer. Needs sudo.
#
#   ./deploy/apply.sh                # 资源限制 + (unit 不存在时安装) + nginx + logrotate
#   ./deploy/apply.sh --watchdog     # 额外开启 systemd watchdog（Type=notify）
#   ./deploy/apply.sh --dry-run      # 只打印要做什么，不写任何文件
#
# 模板里的占位符在安装前渲染：
#   __APP_DIR__      仓库绝对路径（自动：本脚本的上一级目录）
#   __APP_USER__     运行用户（默认 $SUDO_USER / 当前用户）
#   __APP_GROUP__    运行用户组（默认该用户的主组）
#   __PYTHON__       解释器（默认 python3，可用 PYTHON_BIN 指定虚拟环境）
#   __SERVER_NAME__  nginx server_name（默认沿用已安装配置里的值，否则 "_"）
#   __APP_PORT__     监听端口（默认 13210）
#   __REPO_URL__     "/" 的重定向目标（默认本仓库地址）
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$HERE/.." && pwd)"
APP_USER="${APP_USER:-${SUDO_USER:-$(id -un)}}"
APP_GROUP="${APP_GROUP:-$(id -gn "$APP_USER" 2>/dev/null || echo "$APP_USER")}"
# 已安装 unit 的 ExecStart 解释器作为默认值，避免重新 apply 时换掉虚拟环境
DEFAULT_PY=""
if [[ -r "/etc/systemd/system/${UNIT:-paper-summarizer}.service" ]]; then
    DEFAULT_PY="$(awk -F= '/^ExecStart=/{print $2; exit}' \
        "/etc/systemd/system/${UNIT:-paper-summarizer}.service" 2>/dev/null \
        | awk '{print $1}')"
fi
PYTHON_BIN="${PYTHON_BIN:-${DEFAULT_PY:-$(command -v python3 || command -v python)}}"
APP_PORT="${APP_PORT:-13210}"
REPO_URL="${REPO_URL:-https://github.com/cs-qyzhang/zotero-ai-summary}"
UNIT="${UNIT:-paper-summarizer}"
INSTALL_UNIT="${INSTALL_UNIT:-auto}"   # auto | always | never

DRY=""
WATCHDOG=""
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY="echo [dry-run]" ;;
        --watchdog) WATCHDOG="1" ;;
        *) echo "usage: $0 [--dry-run] [--watchdog]" >&2; exit 2 ;;
    esac
done

run() { echo "+ $*"; [[ -n "$DRY" ]] || "$@"; }

# 已安装 nginx 站点里的 server_name 作为默认值，避免重新 apply 时把域名改掉
INSTALLED_SITE="/etc/nginx/sites-enabled/$UNIT"
DEFAULT_SN=""
if [[ -r "$INSTALLED_SITE" ]]; then
    DEFAULT_SN="$(awk '/^[[:space:]]*server_name/{gsub(/;/, "", $2); print $2; exit}' \
        "$INSTALLED_SITE" 2>/dev/null || true)"
fi
SERVER_NAME="${SERVER_NAME:-${DEFAULT_SN:-_}}"

RENDER_DIR="$(mktemp -d)"
trap 'rm -rf "$RENDER_DIR"' EXIT

render() { # render <template> <name>
    sed -e "s|__APP_DIR__|$APP_DIR|g" \
        -e "s|__APP_USER__|$APP_USER|g" \
        -e "s|__APP_GROUP__|$APP_GROUP|g" \
        -e "s|__PYTHON__|$PYTHON_BIN|g" \
        -e "s|__SERVER_NAME__|$SERVER_NAME|g" \
        -e "s|__APP_PORT__|$APP_PORT|g" \
        -e "s|__REPO_URL__|$REPO_URL|g" \
        "$HERE/$1" > "$RENDER_DIR/$2"
    echo "  rendered $2"
}

echo "== settings =="
echo "  APP_DIR=$APP_DIR"
echo "  APP_USER=$APP_USER  APP_GROUP=$APP_GROUP"
echo "  PYTHON_BIN=$PYTHON_BIN  APP_PORT=$APP_PORT"
echo "  SERVER_NAME=$SERVER_NAME"
echo

echo "== render templates =="
render 10-resource-limits.conf 10-resource-limits.conf
render 20-watchdog.conf 20-watchdog.conf
render nginx-paper-summarizer.conf nginx-site
render logrotate-paper-summarizer logrotate
render paper-summarizer-restart.service restart.service
render paper-summarizer-restart.timer restart.timer
render paper-summarizer.service unit.service
echo

if [[ -n "$DRY" ]]; then
    echo "== backup current configs (skipped: --dry-run) =="
else
    echo "== backup current configs =="
    BK="$HERE/backup/$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$BK"
    for f in \
        "/etc/systemd/system/$UNIT.service" \
        "/etc/systemd/system/$UNIT.service.d/10-resource-limits.conf" \
        "/etc/systemd/system/$UNIT.service.d/20-watchdog.conf" \
        "/etc/systemd/system/$UNIT-restart.service" \
        "/etc/systemd/system/$UNIT-restart.timer" \
        "$INSTALLED_SITE" \
        "/etc/logrotate.d/$UNIT"; do
        [[ -f "$f" ]] && cp "$f" "$BK/" || true
    done
    echo "  -> $BK"
fi
echo

echo "== systemd unit =="
if [[ -f "/etc/systemd/system/$UNIT.service" && "$INSTALL_UNIT" != "always" ]]; then
    echo "  keep existing /etc/systemd/system/$UNIT.service"
    echo "  (template: deploy/paper-summarizer.service; 覆盖请用 INSTALL_UNIT=always)"
    diff -u "/etc/systemd/system/$UNIT.service" "$RENDER_DIR/unit.service" \
        | sed 's/^/    /' | head -30 || true
elif [[ "$INSTALL_UNIT" == "never" ]]; then
    echo "  skip (INSTALL_UNIT=never)"
else
    run sudo install -m 644 "$RENDER_DIR/unit.service" "/etc/systemd/system/$UNIT.service"
fi
echo

echo "== systemd drop-ins =="
run sudo install -d -m 755 "/etc/systemd/system/$UNIT.service.d"
run sudo install -m 644 "$RENDER_DIR/10-resource-limits.conf" \
    "/etc/systemd/system/$UNIT.service.d/10-resource-limits.conf"
run sudo install -m 644 "$RENDER_DIR/restart.service" \
    "/etc/systemd/system/$UNIT-restart.service"
run sudo install -m 644 "$RENDER_DIR/restart.timer" \
    "/etc/systemd/system/$UNIT-restart.timer"
if [[ -n "$WATCHDOG" ]]; then
    run sudo install -m 644 "$RENDER_DIR/20-watchdog.conf" \
        "/etc/systemd/system/$UNIT.service.d/20-watchdog.conf"
elif [[ -f "/etc/systemd/system/$UNIT.service.d/20-watchdog.conf" ]]; then
    echo "  !! 20-watchdog.conf 已存在；关闭请删除后 daemon-reload"
fi
echo

echo "== nginx site =="
run sudo install -m 644 "$RENDER_DIR/nginx-site" "$INSTALLED_SITE"
echo

echo "== logrotate =="
run sudo install -m 644 "$RENDER_DIR/logrotate" "/etc/logrotate.d/$UNIT"
echo

echo "== reload =="
run sudo systemctl daemon-reload
run sudo nginx -t
run sudo systemctl reload nginx
run sudo systemctl enable --now "$UNIT-restart.timer" >/dev/null 2>&1 || true
run sudo systemctl restart "$UNIT"
echo

echo "== verify =="
systemctl show -p ActiveState -p OOMPolicy -p RestartUSec -p MemoryMax -p WatchdogUSec "$UNIT"
systemctl show -p StartLimitIntervalUSec "$UNIT"
curl -s -o /dev/null -w "healthz=%{http_code}\n" "http://127.0.0.1:$APP_PORT/healthz" || true
echo
echo "提示：首次部署后确认 证书/server_name 与 $SERVER_NAME 匹配，再执行"
echo "      sudo systemctl enable --now $UNIT"
