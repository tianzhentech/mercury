#!/bin/bash

# Mercury 服务启动脚本 (Gunicorn)

cd "$(dirname "$0")"

activate_virtualenv() {
    local env_path=""

    if [ -f "niko/bin/activate" ]; then
        env_path="niko/bin/activate"
    elif [ -f "venv/bin/activate" ]; then
        env_path="venv/bin/activate"
    else
        echo "❌ 未找到虚拟环境，请确认 niko/bin/activate 或 venv/bin/activate 存在"
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
    [ -n "${WEB_PID:-}" ] && kill "$WEB_PID" 2>/dev/null || true
    [ -n "${REDEEM_PID:-}" ] && kill "$REDEEM_PID" 2>/dev/null || true
    exit
}

activate_virtualenv

WEB_HOST="${WEB_HOST:-127.0.0.1}"
WEB_PORT="${WEB_PORT:-7999}"
REDEEM_HOST="${REDEEM_HOST:-0.0.0.0}"
REDEEM_PORT="${REDEEM_PORT:-8001}"
WEB_BROWSER_HOST="$(display_host_for_browser "$WEB_HOST")"
REDEEM_BROWSER_HOST="$(display_host_for_browser "$REDEEM_HOST")"
PYTHON_BIN="$(command -v python)"

WEB_WORKERS="${WEB_WORKERS:-2}"
REDEEM_WORKERS="${REDEEM_WORKERS:-2}"
WORKER_CLASS="${GUNICORN_WORKER_CLASS:-gthread}"

echo "🚀 启动 Mercury 服务 (Gunicorn 模式)..."
echo ""
echo "ℹ️  当前 Worker: ${WORKER_CLASS}"
if [ "$WORKER_CLASS" = "gthread" ]; then
    echo "ℹ️  Threads: ${GUNICORN_THREADS:-8}"
fi
echo ""

ensure_port_free "Web Server" "$WEB_HOST" "$WEB_PORT"
ensure_port_free "Redeem Server" "$REDEEM_HOST" "$REDEEM_PORT"

build_worker_args "$WORKER_CLASS"

gunicorn web_server:app \
    -b "${WEB_HOST}:${WEB_PORT}" \
    "${WORKER_ARGS[@]}" \
    -w "$WEB_WORKERS" \
    -t 120 \
    --max-requests 1000 \
    --max-requests-jitter 50 \
    --access-logfile - \
    --error-logfile - \
    --log-level info &
WEB_PID=$!

if wait_for_startup "$WEB_PID" "$WEB_HOST" "$WEB_PORT"; then
    echo "✅ Web Server 已启动 (PID: $WEB_PID) - 监听 ${WEB_HOST}:${WEB_PORT}"
    echo "   浏览器打开: http://${WEB_BROWSER_HOST}:${WEB_PORT}"
else
    echo "❌ Web Server 启动失败 (PID: $WEB_PID)"
    wait "$WEB_PID"
    exit 1
fi

gunicorn redeem_server:app \
    -b "${REDEEM_HOST}:${REDEEM_PORT}" \
    "${WORKER_ARGS[@]}" \
    -w "$REDEEM_WORKERS" \
    -t 120 \
    --max-requests 1000 \
    --max-requests-jitter 50 \
    --access-logfile - \
    --error-logfile - \
    --log-level info &
REDEEM_PID=$!

if wait_for_startup "$REDEEM_PID" "$REDEEM_HOST" "$REDEEM_PORT"; then
    echo "✅ Redeem Server 已启动 (PID: $REDEEM_PID) - 监听 ${REDEEM_HOST}:${REDEEM_PORT}"
    echo "   浏览器打开: http://${REDEEM_BROWSER_HOST}:${REDEEM_PORT}"
else
    echo "❌ Redeem Server 启动失败 (PID: $REDEEM_PID)"
    kill "$WEB_PID" 2>/dev/null || true
    wait "$REDEEM_PID"
    exit 1
fi

echo ""
echo "📊 所有服务已启动:"
echo "   - Web Server:    http://${WEB_BROWSER_HOST}:${WEB_PORT} (内部管理)"
echo "   - Redeem Server: http://${REDEEM_BROWSER_HOST}:${REDEEM_PORT} (本机打开)"
if [ "$REDEEM_HOST" = "0.0.0.0" ] || [ "$REDEEM_HOST" = "::" ]; then
    echo "   - Redeem Server: 也可用本机局域网 IP:${REDEEM_PORT} 从其他设备访问"
fi
echo ""
echo "💡 提示: 可通过 WEB_PORT / REDEEM_PORT / GUNICORN_WORKER_CLASS / GUNICORN_THREADS 覆盖默认启动参数"
echo "📝 按 Ctrl+C 停止所有服务"
echo ""

trap shutdown_services SIGINT SIGTERM

wait
