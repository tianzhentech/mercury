#!/bin/bash

# Mercury 服务启动脚本 (Gunicorn)

cd "$(dirname "$0")"

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
