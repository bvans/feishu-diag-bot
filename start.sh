#!/bin/bash
# 单容器启动：先起 CLIProxyAPI（后台），等 8317 就绪，再前台启动机器人。
# 代理配置由运行时挂载到 /CLIProxyAPI/config.yaml（官方镜像的约定）；
# 机器人配置由运行时挂载到 /app/config.yaml。
set -euo pipefail

# 1. 启动代理（后台，日志进容器 stdout，docker logs 可见）
cd /CLIProxyAPI
./CLIProxyAPI &
PROXY_PID=$!

# 2. 等代理就绪（最多 30 秒；未就绪只告警，机器人仍启动，Codex 失败时走快速通道兜底）
READY=0
for _ in $(seq 1 30); do
  if curl -s -o /dev/null http://127.0.0.1:8317/ ; then
    echo "CLIProxyAPI 就绪（PID $PROXY_PID）"
    READY=1
    break
  fi
  sleep 1
done
if [ "$READY" -eq 0 ]; then
  echo "警告：CLIProxyAPI 30 秒未就绪，机器人仍启动（Codex 调用失败时走快速通道兜底）" >&2
fi

# 3. 前台启动机器人（entrypoint.py 生成有效配置后 exec bot.py；容器停时两者一起停）
cd /app
exec uv run python entrypoint.py "$@"
