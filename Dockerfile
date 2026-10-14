# 飞书诊断机器人：Docker 镜像。运行时挂载 config.yaml + .env（见 README「Docker 部署」）。
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Codex CLI + npx 类 MCP 需要 Node；git + openssh-client 用于 clone 仓库
RUN apt-get update && apt-get install -y --no-install-recommends \
    git openssh-client nodejs npm ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @openai/codex

WORKDIR /app

# 先拷依赖定义，利用缓存。uv.lock 存在时锁定版本，不存在时现场解析。
COPY pyproject.toml .python-version uv.lock* ./
RUN uv sync

COPY bot.py diagnose.py slots.py card_form.py codex_diag.py entrypoint.py ./
COPY scripts/ ./scripts/

# entrypoint.py：配 SSH key -> clone/pull 仓库 -> 生成有效配置 -> 启动 bot.py
ENTRYPOINT ["uv", "run", "python", "entrypoint.py"]
CMD ["/app/config.yaml"]
