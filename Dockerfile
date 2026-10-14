# 业务镜像：飞书诊断机器人（Python 应用 + Codex CLI）。
# 诊断仓库在 build 时烘焙进镜像，运行时不再联网 clone，内网可用。
# 代理（CLIProxyAPI）跑在独立的 cliproxyapi 容器里（见 docker-compose.example.yml），不在本镜像内。
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Codex CLI + npx 类 MCP 需要 Node；git + openssh-client 用于 build 时烘焙仓库
RUN apt-get update && apt-get install -y --no-install-recommends \
    git openssh-client nodejs npm ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @openai/codex

WORKDIR /app

# 先拷依赖定义，利用缓存。uv.lock 存在时锁定版本，不存在时现场解析。
COPY pyproject.toml .python-version uv.lock* ./
RUN uv sync

COPY bot.py diagnose.py slots.py card_form.py codex_diag.py entrypoint.py bake_repos.py webchat.py ./
COPY scripts/ ./scripts/

# 烘焙诊断仓库：codex-repos.txt 每行一个 git URL；私有仓库用 BuildKit secret 传 SSH key：
#   docker build --secret id=git_ssh_key,src=./id_diag .
# 公开仓库不需要 secret。clone 失败即 build 失败，不生成缺仓库的镜像。
COPY codex-repos.txt ./
RUN --mount=type=secret,id=git_ssh_key,required=false \
    python3 bake_repos.py --repos-file codex-repos.txt --repos-dir /app/repos

# entrypoint.py：生成有效配置（URL -> 已烘焙目录）-> 启动 bot.py
ENTRYPOINT ["uv", "run", "python", "entrypoint.py"]
CMD ["/app/config.yaml"]
