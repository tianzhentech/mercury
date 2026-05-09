#!/bin/bash
set -e

# Mercury 服务脚本
#
# 默认执行 ./start.sh:
#   1. 安装/同步 Python 依赖
#   2. 如果系统服务尚未安装，则安装为服务
#   3. 如果系统服务已安装，则直接重启服务
#
# 内部执行 ./start.sh run:
#   前台运行 Gunicorn，供 systemd/launchd 调用

cd "$(dirname "$0")"

COMMAND="${1:-install}"
PROJECT_DIR="$(pwd)"
SERVICE_NAME="${SERVICE_NAME:-niko}"
LAUNCHD_LABEL="${LAUNCHD_LABEL:-com.niko.mercury}"

# 兼容已有 systemd 服务文件里直接执行 start.sh 的情况，避免服务启动时递归安装/重启自己。
if [ -n "${INVOCATION_ID:-}${JOURNAL_STREAM:-}" ] && [ "$COMMAND" = "install" ]; then
    COMMAND="run"
fi

activate_virtualenv() {
    local env_path=""

    if [ -n "${VIRTUAL_ENV:-}" ]; then
        return
    fi

    if [ -f ".venv/bin/activate" ]; then
        env_path=".venv/bin/activate"
    elif [ -f "venv/bin/activate" ]; then
        env_path="venv/bin/activate"
    elif [ -f "niko/bin/activate" ]; then
        env_path="niko/bin/activate"
    else
        echo "❌ 未找到虚拟环境，请先运行 uv sync，或确认 .venv/bin/activate、venv/bin/activate、niko/bin/activate 存在"
        exit 1
    fi

    # shellcheck disable=SC1090
    source "$env_path"
}

port_in_use() {
    local host="$1"
    local port="$2"

    if command -v lsof >/dev/null 2>&1; then
        if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
            return 0
        fi
        return 1
    fi

    "$PYTHON_BIN" - "$host" "$port" <<'PY'
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

try:
    sock.bind((host, port))
except OSError:
    sys.exit(0)
finally:
    sock.close()

sys.exit(1)
PY
}

show_port_owner() {
    local port="$1"
    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true
    fi
}

ensure_port_free() {
    local name="$1"
    local host="$2"
    local port="$3"

    if port_in_use "$host" "$port"; then
        echo "❌ ${name} 启动失败: ${host}:${port} 已被占用"
        show_port_owner "$port"
        exit 1
    fi
}

wait_for_startup() {
    local pid="$1"
    local host="$2"
    local port="$3"
    local retries=10
    local i=0

    while [ "$i" -lt "$retries" ]; do
        if ! kill -0 "$pid" 2>/dev/null; then
            return 1
        fi

        if port_in_use "$host" "$port"; then
            return 0
        fi

        sleep 0.5
        i=$((i + 1))
    done

    return 1
}

build_worker_args() {
    local worker_class="$1"

    WORKER_ARGS=(--worker-class "$worker_class")
    if [ "$worker_class" = "gthread" ]; then
        WORKER_ARGS+=(--threads "${GUNICORN_THREADS:-8}")
    fi
}

display_host_for_browser() {
    local host="$1"

    case "$host" in
        0.0.0.0|::)
            echo "127.0.0.1"
            ;;
        *)
            echo "$host"
            ;;
    esac
}

shutdown_services() {
    echo "正在停止服务..."
    [ -n "${APP_PID:-}" ] && kill "$APP_PID" 2>/dev/null || true
    exit
}

run_as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        sudo "$@"
    fi
}

has_virtualenv() {
    [ -f ".venv/bin/activate" ] || [ -f "venv/bin/activate" ] || [ -f "niko/bin/activate" ]
}

ensure_dependencies() {
    echo "📦 安装/同步 Python 依赖..."

    if command -v uv >/dev/null 2>&1 && [ -f "pyproject.toml" ]; then
        if [ -f "uv.lock" ]; then
            if ! uv sync --frozen; then
                echo "⚠️  uv.lock 与 pyproject.toml 不一致，改用 uv sync 重新同步"
                uv sync
            fi
        else
            uv sync
        fi
        echo "✅ 依赖已同步"
        return
    fi

    if ! has_virtualenv; then
        local python_bin=""
        python_bin="$(command -v python3 || command -v python || true)"
        if [ -z "$python_bin" ]; then
            echo "❌ 未找到 python3/python，无法创建虚拟环境"
            exit 1
        fi
        echo "🔧 未找到可用虚拟环境，正在创建 .venv..."
        if ! "$python_bin" -m venv .venv; then
            if command -v apt-get >/dev/null 2>&1; then
                echo "🔧 创建虚拟环境失败，尝试安装 python3-venv 后重试..."
                run_as_root apt-get update
                run_as_root apt-get install -y python3-venv
                if ! "$python_bin" -m venv .venv; then
                    echo "❌ 安装 python3-venv 后仍无法创建虚拟环境"
                    exit 1
                fi
            else
                echo "❌ 创建虚拟环境失败"
                echo "   Debian/Ubuntu 可先运行: apt-get update && apt-get install -y python3-venv"
                exit 1
            fi
        fi
    fi

    activate_virtualenv
    python -m pip install --upgrade pip
    if [ -f "requirements.txt" ]; then
        python -m pip install -r requirements.txt
    fi
    echo "✅ 依赖已安装"
}

write_systemd_service() {
    local service_file="/etc/systemd/system/${SERVICE_NAME}.service"
    local run_user="${SERVICE_USER:-${SUDO_USER:-$(id -un)}}"
    local app_host="${APP_HOST:-${REDEEM_HOST:-${WEB_HOST:-0.0.0.0}}}"
    local app_port="${APP_PORT:-${REDEEM_PORT:-${WEB_PORT:-8001}}}"
    local app_workers="${APP_WORKERS:-${WEB_WORKERS:-${REDEEM_WORKERS:-2}}}"
    local worker_class="${GUNICORN_WORKER_CLASS:-gthread}"
    local gunicorn_threads="${GUNICORN_THREADS:-8}"
    local temp_file=""

    if run_as_root test -f "$service_file"; then
        echo "✅ systemd 服务已安装: ${SERVICE_NAME}"
        return
    fi

    temp_file="$(mktemp)"
    cat > "$temp_file" <<EOF
[Unit]
Description=Niko Mercury Service
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=${run_user}
WorkingDirectory=${PROJECT_DIR}
Environment=APP_HOST=${app_host}
Environment=APP_PORT=${app_port}
Environment=APP_WORKERS=${app_workers}
Environment=GUNICORN_WORKER_CLASS=${worker_class}
Environment=GUNICORN_THREADS=${gunicorn_threads}
Environment=PATH=${PROJECT_DIR}/.venv/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin
EnvironmentFile=-${PROJECT_DIR}/.env
ExecStart=/bin/bash ${PROJECT_DIR}/start.sh run
Restart=always
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

    run_as_root install -m 0644 "$temp_file" "$service_file"
    rm -f "$temp_file"
    run_as_root systemctl daemon-reload
    run_as_root systemctl enable "$SERVICE_NAME"
    echo "✅ systemd 服务已安装: ${SERVICE_NAME}"
}

restart_systemd_service() {
    run_as_root systemctl restart "$SERVICE_NAME"
    echo "✅ systemd 服务已重启: ${SERVICE_NAME}"
    systemctl status "$SERVICE_NAME" --no-pager || true
}

install_or_restart_systemd() {
    ensure_dependencies
    write_systemd_service
    restart_systemd_service
}

write_launchd_service() {
    local plist_file="${LAUNCHD_PLIST:-$HOME/Library/LaunchAgents/${LAUNCHD_LABEL}.plist}"
    local app_host="${APP_HOST:-${REDEEM_HOST:-${WEB_HOST:-0.0.0.0}}}"
    local app_port="${APP_PORT:-${REDEEM_PORT:-${WEB_PORT:-8001}}}"
    local app_workers="${APP_WORKERS:-${WEB_WORKERS:-${REDEEM_WORKERS:-2}}}"
    local worker_class="${GUNICORN_WORKER_CLASS:-gthread}"
    local gunicorn_threads="${GUNICORN_THREADS:-8}"

    if [ -f "$plist_file" ]; then
        echo "✅ launchd 服务已安装: ${LAUNCHD_LABEL}"
        return
    fi

    mkdir -p "$(dirname "$plist_file")" "$PROJECT_DIR/logs"
    cat > "$plist_file" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${PROJECT_DIR}/start.sh</string>
    <string>run</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${PROJECT_DIR}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>APP_HOST</key>
    <string>${app_host}</string>
    <key>APP_PORT</key>
    <string>${app_port}</string>
    <key>APP_WORKERS</key>
    <string>${app_workers}</string>
    <key>GUNICORN_WORKER_CLASS</key>
    <string>${worker_class}</string>
    <key>GUNICORN_THREADS</key>
    <string>${gunicorn_threads}</string>
    <key>PATH</key>
    <string>${PROJECT_DIR}/.venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${PROJECT_DIR}/logs/${SERVICE_NAME}.out.log</string>
  <key>StandardErrorPath</key>
  <string>${PROJECT_DIR}/logs/${SERVICE_NAME}.err.log</string>
</dict>
</plist>
EOF
    echo "✅ launchd 服务已安装: ${LAUNCHD_LABEL}"
}

restart_launchd_service() {
    local plist_file="${LAUNCHD_PLIST:-$HOME/Library/LaunchAgents/${LAUNCHD_LABEL}.plist}"
    local launchd_domain="gui/$(id -u)"

    if ! launchctl print "${launchd_domain}/${LAUNCHD_LABEL}" >/dev/null 2>&1; then
        launchctl bootstrap "$launchd_domain" "$plist_file"
    fi

    launchctl kickstart -k "${launchd_domain}/${LAUNCHD_LABEL}"
    echo "✅ launchd 服务已重启: ${LAUNCHD_LABEL}"
    echo "   日志: ${PROJECT_DIR}/logs/${SERVICE_NAME}.out.log"
}

install_or_restart_launchd() {
    ensure_dependencies
    write_launchd_service
    restart_launchd_service
}

install_or_restart_service() {
    case "$(uname -s)" in
        Linux)
            if ! command -v systemctl >/dev/null 2>&1; then
                echo "❌ 当前 Linux 环境未找到 systemctl，无法安装系统服务"
                exit 1
            fi
            install_or_restart_systemd
            ;;
        Darwin)
            if ! command -v launchctl >/dev/null 2>&1; then
                echo "❌ 当前 macOS 环境未找到 launchctl，无法安装服务"
                exit 1
            fi
            install_or_restart_launchd
            ;;
        *)
            echo "❌ 暂不支持当前系统: $(uname -s)"
            echo "   可使用 ./start.sh run 前台启动服务"
            exit 1
            ;;
    esac
}

show_service_status() {
    case "$(uname -s)" in
        Linux)
            systemctl status "$SERVICE_NAME" --no-pager
            ;;
        Darwin)
            launchctl print "gui/$(id -u)/${LAUNCHD_LABEL}"
            ;;
        *)
            echo "暂不支持当前系统: $(uname -s)"
            exit 1
            ;;
    esac
}

restart_installed_service() {
    case "$(uname -s)" in
        Linux)
            restart_systemd_service
            ;;
        Darwin)
            restart_launchd_service
            ;;
        *)
            echo "暂不支持当前系统: $(uname -s)"
            exit 1
            ;;
    esac
}

show_usage() {
    cat <<EOF
用法:
  ./start.sh          安装/同步依赖，并安装或重启系统服务
  ./start.sh install  同上
  ./start.sh restart  重启已安装的系统服务
  ./start.sh status   查看系统服务状态
  ./start.sh run      前台运行 Gunicorn（供系统服务内部调用）

常用环境变量:
  SERVICE_NAME=niko
  APP_HOST=0.0.0.0
  APP_PORT=8001
  APP_WORKERS=2
EOF
}

run_server() {
    activate_virtualenv

    APP_HOST="${APP_HOST:-${REDEEM_HOST:-${WEB_HOST:-0.0.0.0}}}"
    APP_PORT="${APP_PORT:-${REDEEM_PORT:-${WEB_PORT:-8001}}}"
    APP_BROWSER_HOST="$(display_host_for_browser "$APP_HOST")"
    PYTHON_BIN="$(command -v python)"

    APP_WORKERS="${APP_WORKERS:-${WEB_WORKERS:-${REDEEM_WORKERS:-2}}}"
    WORKER_CLASS="${GUNICORN_WORKER_CLASS:-gthread}"

    echo "🚀 启动 Mercury 服务 (Gunicorn 模式)..."
    echo ""
    echo "ℹ️  当前 Worker: ${WORKER_CLASS}"
    if [ "$WORKER_CLASS" = "gthread" ]; then
        echo "ℹ️  Threads: ${GUNICORN_THREADS:-8}"
    fi
    echo ""

    ensure_port_free "Mercury Server" "$APP_HOST" "$APP_PORT"

    build_worker_args "$WORKER_CLASS"

    gunicorn web_server:app \
        -b "${APP_HOST}:${APP_PORT}" \
        "${WORKER_ARGS[@]}" \
        -w "$APP_WORKERS" \
        -t 120 \
        --max-requests 1000 \
        --max-requests-jitter 50 \
        --access-logfile - \
        --error-logfile - \
        --log-level info &
    APP_PID=$!

    if wait_for_startup "$APP_PID" "$APP_HOST" "$APP_PORT"; then
        echo "✅ Mercury Server 已启动 (PID: $APP_PID) - 监听 ${APP_HOST}:${APP_PORT}"
    else
        echo "❌ Mercury Server 启动失败 (PID: $APP_PID)"
        wait "$APP_PID"
        exit 1
    fi

    echo ""
    echo "📊 服务已启动:"
    echo "   - 兑换页:   http://${APP_BROWSER_HOST}:${APP_PORT}/"
    echo "   - 管理后台: http://${APP_BROWSER_HOST}:${APP_PORT}/admin"
    if [ "$APP_HOST" = "0.0.0.0" ] || [ "$APP_HOST" = "::" ]; then
        echo "   - 也可用本机局域网 IP:${APP_PORT} 从其他设备访问"
    fi
    echo ""
    echo "💡 提示: 可通过 APP_HOST / APP_PORT / APP_WORKERS / GUNICORN_WORKER_CLASS / GUNICORN_THREADS 覆盖默认启动参数"
    echo "📝 按 Ctrl+C 停止服务"
    echo ""

    trap shutdown_services SIGINT SIGTERM

    wait
}

case "$COMMAND" in
    install)
        install_or_restart_service
        ;;
    restart)
        restart_installed_service
        ;;
    status)
        show_service_status
        ;;
    run)
        run_server
        ;;
    help|--help|-h)
        show_usage
        ;;
    *)
        echo "❌ 未知命令: $COMMAND"
        show_usage
        exit 1
        ;;
esac
