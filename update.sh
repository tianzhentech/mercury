#!/bin/bash

# Niko 更新脚本
# 从 GitHub 拉取最新代码并重启服务

set -e

cd "$(dirname "$0")"

echo "正在从 GitHub 拉取最新代码..."
git pull origin main

echo "代码更新完成，正在重启服务..."
sudo systemctl restart niko

echo "服务已重启完成"
systemctl status niko --no-pager
