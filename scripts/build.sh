#!/bin/bash
# 构建两层镜像：先二次基础镜像（CLIProxyAPI + Codex + 工具链），再业务镜像（Python 应用 + 烘焙仓库）。
# 用法：
#   scripts/build.sh                                    # 公开仓库
#   GIT_SSH_KEY=./id_diag scripts/build.sh              # 私有仓库（build 时传 SSH key，不进镜像层）
#   CLIPROXYAPI_IMAGE=eceasy/cli-proxy-api:v8.0.28 scripts/build.sh  # 把代理钉到具体版本
set -euo pipefail
cd "$(dirname "$0")/.."

docker build -f Dockerfile.base \
  --build-arg "CLIPROXYAPI_IMAGE=${CLIPROXYAPI_IMAGE:-eceasy/cli-proxy-api:latest}" \
  -t feishu-diag-base:latest .

BUSINESS_ARGS=()
if [ -n "${GIT_SSH_KEY:-}" ]; then
  BUSINESS_ARGS+=(--secret "id=git_ssh_key,src=$GIT_SSH_KEY")
fi
docker build "${BUSINESS_ARGS[@]}" -t feishu-diag-bot:latest .
