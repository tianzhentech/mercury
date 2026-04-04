#!/bin/bash

# 兼容旧入口，统一复用 start.sh

cd "$(dirname "$0")"
exec ./start.sh "$@"
