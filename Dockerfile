# 业务镜像：飞书诊断机器人（Python 应用），基于二次基础镜像（CLIProxyAPI + Codex）。
# 诊断仓库在 build 时烘焙进镜像，运行时不再联网 clone，内网可用。
# 构建：先 scripts/build.sh 建好二次基础镜像，再 docker build -t feishu-diag-bot .
ARG BASE_IMAGE=feishu-diag-base:latest
FROM ${BASE_IMAGE}

WORKDIR /app

# 先拷依赖定义，利用缓存。uv.lock 存在时锁定版本，不存在时现场解析。
COPY pyproject.toml .python-version uv.lock* ./
RUN uv sync

COPY bot.py diagnose.py slots.py card_form.py codex_diag.py entrypoint.py bake_repos.py start.sh ./
COPY scripts/ ./scripts/
RUN chmod +x /app/start.sh

# 烘焙诊断仓库：codex-repos.txt 每行一个 git URL；私有仓库用 BuildKit secret 传 SSH key：
#   docker build --secret id=git_ssh_key,src=./id_diag .
# 公开仓库不需要 secret。clone 失败即 build 失败，不生成缺仓库的镜像。
COPY codex-repos.txt ./
RUN --mount=type=secret,id=git_ssh_key,required=false \
    uv run python bake_repos.py --repos-file codex-repos.txt --repos-dir /app/repos

# start.sh：启动 CLIProxyAPI（后台）-> 等代理就绪 -> entrypoint.py 生成有效配置 -> 启动 bot.py
# 代理配置由运行时挂载到 /CLIProxyAPI/config.yaml（见 docker-compose.example.yml）
ENTRYPOINT ["/app/start.sh"]
CMD ["/app/config.yaml"]
