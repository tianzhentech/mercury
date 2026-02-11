#!/bin/bash

# Mercury 服务启动脚本 (使用 Gunicorn)
# 更好的内存管理和进程控制

cd "$(dirname "$0")"

# 激活虚拟环境
source venv/bin/activate

echo "🚀 启动 Mercury 服务 (Gunicorn 模式)..."
echo ""

# Gunicorn 配置说明:
# -k gevent     : 使用 gevent 异步 worker (支持 SSE 流式请求)
# -w 4          : 4个 worker 进程
# -t 120        : 请求超时时间 120 秒
# --max-requests 1000       : 每个 worker 处理 1000 个请求后重启 (防止内存泄漏)
# --max-requests-jitter 50  : 随机增加 0-50 个请求后重启 (避免所有 worker 同时重启)
# --access-logfile -        : 访问日志输出到 stdout
# --error-logfile -         : 错误日志输出到 stderr

# 启动 Web Server (端口 7999)
gunicorn web_server:app \
    -b 127.0.0.1:7999 \
    -k gevent \
    -w 4 \
    -t 120 \
    --max-requests 1000 \
    --max-requests-jitter 50 \
    --access-logfile - \
    --error-logfile - \
    --log-level info &
WEB_PID=$!
echo "✅ Web Server 已启动 (PID: $WEB_PID) - http://127.0.0.1:7999"

# 等待 Web Server 启动
sleep 2

# 启动 Redeem Server (端口 8000)
gunicorn redeem_server:app \
    -b 0.0.0.0:8000 \
    -k gevent \
    -w 2 \
    -t 120 \
    --max-requests 1000 \
    --max-requests-jitter 50 \
    --access-logfile - \
    --error-logfile - \
    --log-level info &
REDEEM_PID=$!
echo "✅ Redeem Server 已启动 (PID: $REDEEM_PID) - http://0.0.0.0:8000"

echo ""
echo "📊 所有服务已启动:"
echo "   - Web Server:    http://127.0.0.1:7999 (内部管理)"
echo "   - Redeem Server: http://0.0.0.0:8000  (外部兑换)"
echo ""
echo "💡 提示: 使用 --max-requests 自动重启 worker 可防止内存泄漏"
echo "📝 按 Ctrl+C 停止所有服务"
echo ""

# 捕获退出信号，停止所有服务
trap "echo '正在停止服务...'; kill $WEB_PID $REDEEM_PID 2>/dev/null; exit" SIGINT SIGTERM

# 等待子进程
wait
