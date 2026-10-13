#!/usr/bin/env bash
# 安装 Codex CLI（机器人诊断用）。手动跑一次即可；机器人启动时只检查，不自动安装。
set -euo pipefail

if command -v codex >/dev/null 2>&1; then
  echo "codex 已安装：$(codex --version)"
  exit 0
fi
if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  echo "需要先装 Node.js（含 npm），再跑本脚本。见 https://nodejs.org/"
  exit 1
fi
npm install -g @openai/codex
echo "安装完成：$(codex --version)"
echo "下一步：在 config.yaml 里配 codex.repos / codex.mcp_servers，并确保 Codex CLI 能认证（OPENAI_API_KEY 或 ChatGPT 登录）。"
