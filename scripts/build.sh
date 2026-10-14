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
# 同时打两个 tag：本地名（习惯用法）+ GHCR 名（compose 默认拉的镜像名，
# 本地构建后 docker compose up -d 可直接用，不用等 CI）。
docker build "${BUSINESS_ARGS[@]}" -t feishu-diag-bot:latest -t ghcr.io/bvans/feishu-diag-bot:latest .
