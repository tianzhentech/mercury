#!/bin/bash

# Mercury 服务启动脚本
# 同时启动 Web Server 和 Redeem Server

cd "$(dirname "$0")"

echo "启动 Mercury 服务..."

# 启动 Web Server (端口 7999)
python web_server.py &
WEB_PID=$!
echo "Web Server 已启动 (PID: $WEB_PID)"

# 等待 Web Server 启动
sleep 2

# 启动 Redeem Server (端口 8000)
python redeem_server.py &
REDEEM_PID=$!
echo "Redeem Server 已启动 (PID: $REDEEM_PID)"

echo ""
echo "所有服务已启动:"
echo "  - Web Server: http://127.0.0.1:7999"
echo "  - Redeem Server: http://0.0.0.0:8000"
echo ""
echo "按 Ctrl+C 停止所有服务"

# 捕获退出信号，停止所有服务
trap "echo '正在停止服务...'; kill $WEB_PID $REDEEM_PID 2>/dev/null; exit" SIGINT SIGTERM

# 等待子进程
wait
