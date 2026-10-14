#!/bin/bash
# 构建业务镜像（Python 应用 + Codex CLI + build 时烘焙仓库）。代理用官方镜像，不用构建。
# 用法：
#   scripts/build.sh                        # 公开仓库
#   GIT_SSH_KEY=./id_diag scripts/build.sh  # 私有仓库（build 时传 SSH key，不进镜像层）
set -euo pipefail
cd "$(dirname "$0")/.."

BUSINESS_ARGS=()
if [ -n "${GIT_SSH_KEY:-}" ]; then
  BUSINESS_ARGS+=(--secret "id=git_ssh_key,src=$GIT_SSH_KEY")
fi
docker build "${BUSINESS_ARGS[@]}" -t feishu-diag-bot:latest .
