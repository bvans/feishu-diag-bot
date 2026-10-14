# 飞书诊断机器人

测试人员在飞书群里 @机器人 反馈问题，机器人自动结合**服务器日志 + 代码库**做诊断，
把结论回在原消息下面。

```
测试人员 @机器人：下单接口 500 了，request_id=abc123，15:04 左右
        │
        ▼  im.message.receive_v1（WebSocket 长连接，机器人只需出网）
   bot.py：解析消息 → 起后台线程（3 秒内返回，避免飞书超时重试）
        │
        ▼
   slots.py：必填项（描述/时间/版本）齐了吗？
   缺 → 追问缺什么，等测试人员 @补充（30 分钟内自动合并）
   齐 → 继续
        │
        ▼
   diagnose.py
   1. 信号提取（正则）：异常名、栈追踪文件:行号、request/trace ID、时间戳、报错关键词
   2. 查日志：对信号词 grep 各日志文件，取命中行
   3. 查代码：栈追踪文件直接读上下文；异常名/关键词 grep 仓库
   4. LLM 综合：问题描述 + 日志证据 + 代码证据 → 结构化诊断
        │
        ▼
   群内楼中楼回复：根因 + 证据 + 修复建议 + 置信度 + 还需要什么信息
```

## 诊断质量取决于输入

检索是关键词 grep，不是语义搜索。**必填三项，缺一不可**：

| 必填项 | 要求 | 例子 |
|---|---|---|
| 问题描述 | 报错原文 + 现象，不能只说"挂了" | 下单接口返回 500，点支付没反应 |
| 时间 | 问题发生的时间 | 15:04 或 2026-10-13 15:04 |
| 版本 | 出问题的版本 | v1.2.3 或 版本 1.2.3 |

**必填项缺失时机器人不会硬猜**，会直接追问缺什么。测试人员补充时**再次 @机器人**即可，
30 分钟内同一人的补充会自动合并到同一个问题里，集齐三项后才开始诊断。
一次发全最好：`@机器人 下单接口返回 500，点支付没反应，15:04 左右，版本 v1.2.3`

选填但很有用：请求 ID / trace ID（最精确的检索词）、环境（生产/预发/测试）。

## 飞书应用配置（你手动做一次）

1. [飞书开放平台](https://open.feishu.cn/)建应用：选**企业自建应用**（WebSocket 长连接只支持这种）。
2. **权限管理**里加：
   - `im:message`（接收消息）
   - `im:message.group_at_msg:readonly`（群里接收 @机器人的消息）
   - `im:message.p2p_msg:readonly`（私聊，选配）
   - `im:message:send_as_bot`（以机器人身份发消息）
3. **事件订阅** → 订阅方式选**使用长连接接收事件** → 添加事件 `im.message.receive_v1`。
4. **发布应用**（新版发布，权限才生效）。
5. 把机器人拉进测试群。凭据在应用的**凭证与基础信息**页：App ID、App Secret。

## 部署（你的服务器）

```bash
cd feishu-diag-bot
cp config.example.yaml config.yaml
cp .env.example .env   # 填真实值：FEISHU_APP_ID、FEISHU_APP_SECRET、GEMINI_API_KEY
# 按你的实际环境改 config.yaml：日志路径、代码库路径
uv sync
uv run bot.py
```

常驻用 systemd（`/etc/systemd/system/feishu-diag-bot.service`）：

```ini
[Unit]
Description=Feishu diag bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/feishu-diag-bot
EnvironmentFile=/opt/feishu-diag-bot/.env
ExecStart=/opt/feishu-diag-bot/.venv/bin/python /opt/feishu-diag-bot/bot.py   # .venv 由 uv sync 生成，运行时不需要 uv
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`.env` 里放三个环境变量（`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`GEMINI_API_KEY`）。

## Docker 部署（推荐）

两个容器，各做一件事：

```
cliproxyapi（eceasy/cli-proxy-api:v8.0.28）  # 协议转换代理：Chat Completions <-> Responses
feishu-diag-bot（业务镜像）                    # Python 应用 + Codex CLI + build 时烘焙诊断仓库
```

诊断仓库在 **build 时烘焙进业务镜像**，运行时不再联网 clone，内网可用。

```bash
# 1. 准备配置和凭据
cp config.example.yaml config.yaml   # 改成你的：仓库地址、MCP、提示词
cp cliproxyapi-config.example.yaml cliproxyapi-config.yaml  # 填上游端点和两个 key
cp .env.example .env                 # 填 FEISHU_APP_ID、FEISHU_APP_SECRET、CLIPROXYAPI_API_KEY 等
# 诊断仓库清单（每行一个 git URL）：编辑 codex-repos.txt

# 2. 拉取业务镜像（GitHub Actions 自动构建并推送到 GHCR，见下「容器镜像」一节）
docker compose pull
# 部署机首次拉私有包需先登录：docker login ghcr.io
#   （用有 read:packages 权限的 Personal Access Token，用户名填 GitHub 用户名）
# 本地构建（改完代码不想等 CI 时用）：scripts/build.sh
#   （私有仓库：GIT_SSH_KEY=./id_diag scripts/build.sh，build 时传 SSH key，BuildKit secret，不进镜像层）

# 3. 启动（两个容器）
docker compose up -d
```

内网部署：有网的机器上 `docker pull ghcr.io/bvans/feishu-diag-bot:latest`
后 `docker save ghcr.io/bvans/feishu-diag-bot:latest > bot.tar`，
拷到内网机器 `docker load < bot.tar` 即可运行（代理镜像
`eceasy/cli-proxy-api:v8.0.28` 同样需要 `docker save/load` 搬过去，或内网已有镜像仓库时
直接拉取）。运行时仍需访问飞书 API 和模型 API（或走代理/中转），但不再需要访问 git 仓库。

**启动流程**（`entrypoint.py`）：把 `config.yaml` 里 `codex.repos` 的 URL
映射到 build 时已烘焙的目录（`/app/repos/<目录>`，按镜像内
`.baked-manifest.json`）→ 生成有效配置 → 启动 `bot.py`。本地路径的 repos 条目
原样使用；URL 对应目录不存在时记警告并跳过，不阻止机器人启动。
仓库更新靠重新构建镜像：改 `codex-repos.txt` 后 push 到 main 触发 CI，
或本地 `scripts/build.sh`（都会重新 clone 最新版）。

**容器镜像（GitHub Actions 自动构建）**：`.github/workflows/docker-build.yml`，
push 到 main（改了 `Dockerfile` / `*.py` / `codex-repos.txt` 等镜像相关文件）/
打 `v*` tag / 手动触发（Actions → Run workflow）时，先跑全部单测，通过后构建
业务镜像并推送到 GHCR：`ghcr.io/bvans/feishu-diag-bot:latest`（另有 `sha-<短提交>`
和 tag 对应的 `1.2.3` / `1.2` 标签）。PR 只构建不推送。
私有诊断仓库需在仓库 Settings → Secrets → Actions 里加 `GIT_SSH_KEY`
（只读 deploy key 的私钥，build 时经 BuildKit secret 传入，不进镜像层）；
公开仓库（https URL）不用配。

**代理配置**：`cliproxyapi-config.yaml` 运行时挂载到 cliproxyapi 容器的
`/CLIProxyAPI/config.yaml`，不进镜像。`config.yaml` 里 `codex.model_providers`
指向 compose 内的代理服务（`base_url: http://cliproxyapi:8317/v1`，
`env_key: CLIPROXYAPI_API_KEY`）。不用代理时（直连 OpenAI / ChatGPT 登录）删掉
cliproxyapi 服务，并把 `model_providers` 改回直连或删掉。

**飞书凭据走环境变量**：`FEISHU_APP_ID` / `FEISHU_APP_SECRET` 在 `.env` 里填，
环境变量优先于 `config.yaml` 里的 `feishu` 一节。

**仓库可选**：`codex.repos` 可空，不配时 Codex 以当前工作目录（容器里是 `/app`，
镜像里已装好 Codex CLI，可直接跑）做工作区。

**安全注意**：

- `config.yaml` / `.env` / `cliproxyapi-config.yaml` 不进镜像（`.dockerignore` 已排除），只在运行时挂载。
- BuildKit secret（`--secret id=git_ssh_key`）不会留在镜像层里，比把 key COPY 进镜像安全。
- 直接 `uv run bot.py` 时 `codex.repos` 只能用本地路径（没有 build 时烘焙）。
- `cliproxyapi-config.yaml` 里也有 key（上游 key + 访问代理的客户端 key），同样勿提交到 Git。
- compose 里 8317 只绑 `127.0.0.1`，不要公网暴露。

## 网页聊天窗口（本地测试用，不用飞书）

和飞书群里 @机器人 同一套逻辑（必填项追问 → 30 分钟内补充合并 → 集齐后诊断），
只是在浏览器里对话，方便不进飞书群直接测试：

```bash
uv run python webchat.py [config.yaml]   # 默认 http://127.0.0.1:8080
# 端口：WEBCHAT_PORT=8081 uv run python webchat.py
```

- 不需要飞书凭据，只读 `config.yaml` 里的诊断配置（codex / llm / logs / repo）。
- 只绑 `127.0.0.1`，不要公网暴露（无鉴权）。
- Docker 里跑：`docker run --rm -p 127.0.0.1:8080:8080 -v ./config.yaml:/app/config.yaml:ro -v /var/log/myapp:/var/log/myapp:ro ghcr.io/bvans/feishu-diag-bot:latest uv run python webchat.py`（用业务镜像，命令覆盖启动 webchat；诊断要读的仓库已烘焙在镜像里）。

## 管理配置界面（人工改部署配置，不用手写文件）

在浏览器里改部署需要的五个配置，不用直接编辑文件：

```bash
uv run python admin.py [配置目录]   # 默认当前目录，打开 http://127.0.0.1:8081
# 端口：ADMIN_PORT=8082 uv run python admin.py
```

- **config.yaml**：机器人配置（飞书/LLM/日志/仓库/提示词/Codex/回复）。
- **cliproxyapi-config.yaml**：代理配置（端口/客户端 key/上游端点/模型别名）。
- **.env**：环境变量凭据（飞书 App 密钥、各 API key）。
- **codex-repos.txt**：build 时烘焙进业务镜像的仓库清单（每行一个 URL）。
- **AGENTS.md**：Codex 全局指令（保存后下次诊断即生效，无需重启）。

机制：保存前按类型校验（YAML 必须能解析，`.env` 每行必须是 `KEY=VALUE`），
校验失败不写盘；每次保存前自动备份旧文件（保留最近 5 份），可一键恢复；
文件不存在时可从同名 `.example` 示例创建（`codex-repos.txt` 建空文件）。

注意：
- 只绑 `127.0.0.1`，不要公网暴露（无鉴权，且 `.env` 密钥会明文显示）。
- 保存只改文件，不重启服务：改 `config.yaml` / `cliproxyapi-config.yaml` / `.env`
  后重启容器生效（`docker compose restart`）；改 `codex-repos.txt` 后需重新构建
  镜像（push 触发 CI，或本地 `scripts/build.sh`）才能烘焙新仓库。
- Docker 里跑：`docker run --rm -p 127.0.0.1:8081:8081 -v ./:/app/config-dir:rw feishu-diag-bot uv run python admin.py /app/config-dir`
  （配置目录需可写挂载，默认 compose 里是只读挂载，admin 在 Docker 里改不了）。

## Codex CLI 诊断（可选，默认开启）

`config.yaml` 里 `codex.enabled=true` 时，诊断走 Codex CLI；未安装 / 失败 / 超时时
自动兜底到内置快速通道（正则 + grep + 一次 LLM 调用），不会丢诊断。

**配置**（`config.yaml` 的 `codex` 一节）：

- `repos`：诊断时 Codex 可访问的代码仓库列表。第一个为工作目录（`-C`），其余用
  `--add-dir` 挂载。可空：不配时以当前工作目录（容器里是 `/app`）做工作区。
- `model_providers`：模型提供方（`model_provider` 选一个，`model_providers.<id>` 里配
  `base_url` / `env_key` / `wire_api` 等）。**必须用它**：实测 Codex 0.149 会忽略
  `OPENAI_BASE_URL`、仍连 api.openai.com，光设 baseURL 不生效。
- `mcp_servers`：辅助诊断的 MCP 服务器列表（Codex 原生支持），每项配 `name` /
  `command` / `args` / `env`。不需要就删掉整节。
- `model`：留空用 Codex 默认模型；`reasoning_effort`：low / medium / high；
  `timeout_seconds`：单次诊断超时（默认 180 秒）。

**安装**：

```bash
bash scripts/setup-codex.sh   # 检查 Node.js，npm install -g @openai/codex
```

**全局 AGENTS.md**：Codex 启动时从 `CODEX_HOME` 读 `AGENTS.md`（有
`AGENTS.override.md` 时优先读它）。机器人每次诊断用临时 `CODEX_HOME`，
所以全局文件需由机器人拷进去：复制 `AGENTS.md.example` 为 `AGENTS.md`，
按需改写，挂载进容器（compose 里已留好 `./AGENTS.md:/app/AGENTS.md:ro`），
`config.yaml` 里配 `codex.agents_md_file: "/app/AGENTS.md"`（或用内联文本
`codex.agents_md`）。仓库根目录的 `AGENTS.md` 由 Codex 自动从仓库里加载，
适合写该仓库特有的约定，不用机器人处理。

**认证**：Codex CLI 自己负责——`OPENAI_API_KEY` 环境变量（见 `.env.example`），
或按 Codex 官方文档用 ChatGPT 登录，或 `model_providers` 里 `env_key` /
`experimental_bearer_token` 指定的凭据（如 CLIProxyAPI 的客户端 key）。

**机器人怎么调它**：每次诊断生成临时 `CODEX_HOME`（不碰你的 `~/.codex`），
里面只写本次的 `config.toml`（`approval_policy="never"`、`sandbox_mode="read-only"`、
模型、MCP 服务器）；命令是
`codex exec --skip-git-repo-check -C <主仓库> --add-dir <其他仓库> --output-last-message <临时文件> "<提示词>"`。
机器人启动时若 `codex.enabled=true` 但找不到 `codex` 二进制，只记一条警告，
不影响运行。

## 换 LLM

`config.yaml` 里 `llm` 一节改 `base_url` + `model` 即可，接口是 OpenAI 兼容的：
LLM 的 key 从哪个环境变量读由 `llm.api_key_env` 决定（默认 `GEMINI_API_KEY`）；
用 OpenAI 模型时把 `api_key_env` 改成 `OPENAI_API_KEY`，并在 `.env` 里填 `OPENAI_API_KEY`。
- Gemini：`https://generativelanguage.googleapis.com/v1beta/openai/` + `gemini-2.5-flash`
- OpenAI：`https://api.openai.com/v1` + `gpt-4o-mini`（`api_key_env` 改成 `OPENAI_API_KEY`）

## v1 的边界（有意没做的）

- **图片 OCR**：测试人员只发截图不贴文字时，机器人会回"请粘贴报错原文"。要支持截图需调飞书下载图片接口 + 视觉模型。
- **语义检索**：目前是关键词 grep；日志/代码量大时可升级为向量检索（embed + 向量库）。
- **多机器日志**：bot 只读本机日志。日志在别的机器上时，把 bot 部署到那台机器，或把日志同步过来。
- **权限**：bot 看得到什么取决于运行它的系统账号；不要用 root 跑，用只能读日志和代码的专用账号。

## 卡片表单（可选，未默认启用）

缺必填项时，除了文字追问，也可用交互卡片表单让测试人员填表提交。
`card_form.py` 已写好并单测通过（`uv run test_card_form.py`，6 项全过），
默认不改变现有文字追问行为；确认要切到卡片表单时再接线：

**卡片内容**：问题描述（多行必填）、时间（日期时间选择器必填，回调值带 `+0800` 时区后缀，解析时已去掉）、
版本（必填）、请求 ID（选填）、环境（下拉选填：生产/预发/测试），提交按钮 `action_type=form_submit`。

**行为**：提交后 3 秒内回 toast；缺项回 warning toast 且卡片保留可重填，不诊断；
齐了回 success toast + 卡片换成"已收到，正在诊断"，诊断在后台线程做、结论以消息发到群里；
表单值优先于会话旧值，空字段沿用会话里已收到的内容。

**接线（两步）**：
1. 飞书后台「事件与回调 → 回调配置」订阅 `card.action.trigger` 并选长连接（和事件订阅是两处配置），再发布新版本；没配点提交会报 200340。
2. `bot.py` 里做三处改动：缺项时改调 `card_form.send_card(client, chat_id, build_issue_card(prefill, missing), reply_to_message_id=message_id)` 发卡；
   用 `card_form.make_card_action_handler(...)` 构造回调 handler（传入会话 get/save/clear、`diagnose`、`send_text`）；
   在 `EventDispatcherHandler` 上加 `.register_p2_card_action_trigger(card_handler)`。
   细节见 `card_form.py` 模块 docstring。
