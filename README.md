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
