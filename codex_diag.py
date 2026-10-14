"""Codex CLI 诊断：机器人把问题交给 Codex CLI，Codex 在配置的仓库 + MCP 帮助下自主查证。

设计要点：
- 配置驱动：仓库列表（codex.repos）+ MCP 服务器列表（codex.mcp_servers）都在
  config.yaml 里配；机器人不硬编码任何仓库或 MCP。
- 不碰用户现有的 ~/.codex：每次诊断生成一个临时 CODEX_HOME，里面只写本次诊断
  需要的 config.toml（模型、沙箱、审批策略、MCP 服务器），诊断完随临时目录删掉。
- 只查不改：config.toml 里 approval_policy="never"、sandbox_mode="read-only"，
  Codex 只能读、不能改文件、不能弹授权框。
- 失败不丢诊断：codex 二进制缺失 / 超时 / 非零退出 / 无输出时抛 CodexError，
  由 diagnose.py 兜底走内置快速通道（正则 + grep + 一次 LLM 调用）。
- 认证：Codex CLI 自己负责（OPENAI_API_KEY 环境变量，或 ChatGPT 登录），
  机器人不处理凭据，只透传环境变量。
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile

log = logging.getLogger("feishu-diag-bot")

DEFAULT_TIMEOUT_SECONDS = 180


class CodexError(Exception):
    """Codex CLI 诊断失败（调用方兜底处理）。"""


def _toml_str(value) -> str:
    """转 TOML 基本字符串 / 字符串数组。JSON 的转义与 TOML 双引号字符串兼容。"""
    return json.dumps(value, ensure_ascii=False)


def build_codex_toml(codex_cfg: dict) -> str:
    """生成 Codex config.toml 文本：模型、沙箱、审批策略、MCP 服务器。"""
    lines = [
        "# 由 feishu-diag-bot 生成（临时 CODEX_HOME），不要手改。",
        'approval_policy = "never"',
        'sandbox_mode = "read-only"',
    ]
    model = (codex_cfg.get("model") or "").strip()
    if model:
        lines.append(f"model = {_toml_str(model)}")
    effort = (codex_cfg.get("reasoning_effort") or "").strip()
    if effort:
        lines.append(f"model_reasoning_effort = {_toml_str(effort)}")
    for server in codex_cfg.get("mcp_servers") or []:
        name = (server.get("name") or "").strip()
        command = (server.get("command") or "").strip()
        if not name or not command:
            continue
        lines.append("")
        lines.append(f"[mcp_servers.{_toml_str(name)}]")
        lines.append(f"command = {_toml_str(command)}")
        args = server.get("args") or []
        lines.append(f"args = {_toml_str([str(a) for a in args])}")
        env = server.get("env") or {}
        if env:
            pairs = ", ".join(f"{_toml_str(str(k))} = {_toml_str(str(v))}" for k, v in env.items())
            lines.append(f"env = {{ {pairs} }}")
        for key in ("startup_timeout_sec", "tool_timeout_sec"):
            if server.get(key) is not None:
                lines.append(f"{key} = {server[key]}")
    return "\n".join(lines) + "\n"


def build_codex_home(codex_cfg: dict, base_dir: str) -> str:
    """在 base_dir 写 config.toml，返回 base_dir（调用方设为 CODEX_HOME）。"""
    path = os.path.join(base_dir, "config.toml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(build_codex_toml(codex_cfg))
    return base_dir


def build_prompt(issue_text: str, cfg: dict) -> str:
    """给 Codex 的诊断提示词：问题 + 已知槽位 + 仓库/日志位置 + 只查不改约束。"""
    from slots import extract_slots

    slots = extract_slots(issue_text)
    codex_cfg = cfg.get("codex") or {}
    repos = resolve_repos(codex_cfg, cfg)
    log_paths = [str(x.get("path", "")) for x in (cfg.get("logs") or []) if x.get("path")]

    template = ((cfg.get("prompt") or {}).get("template") or "").strip()
    if template:
        repos_text = "\n".join(f"- {p}" for p in repos) if repos else "（未配置仓库，只能按问题描述分析）"
        logs_text = "\n".join(f"- {p}" for p in log_paths) if log_paths else "（未配置日志文件）"
        return (
            template.replace("{issue}", issue_text)
            .replace("{time}", slots.get("time") or "未提供")
            .replace("{version}", slots.get("version") or "未提供")
            .replace("{env}", slots.get("env") or "未提供")
            .replace("{request_id}", slots.get("request_id") or "未提供")
            .replace("{repos}", repos_text)
            .replace("{logs}", logs_text)
        )

    lines = [
        "你是一名资深值班工程师。测试人员反馈了一个线上问题，请你结合代码仓库、"
        "服务器日志和可用的 MCP 工具自主查证，给出诊断结论。",
        "",
        "【问题反馈】",
        issue_text,
        "",
        "【已知信息】",
        f"- 时间：{slots.get('time') or '未提供'}",
        f"- 版本：{slots.get('version') or '未提供'}",
        f"- 环境：{slots.get('env') or '未提供'}",
        f"- 请求 ID：{slots.get('request_id') or '未提供'}",
        "",
        "【代码仓库】",
    ]
    if repos:
        lines.append(f"- 主仓库（当前工作目录）：{repos[0]}")
        for extra in repos[1:]:
            lines.append(f"- 其他仓库（已用 --add-dir 挂载）：{extra}")
    else:
        lines.append("- （未配置仓库，只能按问题描述分析）")
    if log_paths:
        lines.append("")
        lines.append("【服务器日志文件】（用 grep / 读文件查，不要假设日志内容）")
        lines += [f"- {p}" for p in log_paths]
    lines += [
        "",
        "【要求】",
        "1. 只查、不改：不要修改任何文件，不要执行写操作，不要向线上服务发请求。",
        "2. 先用 grep / 读文件定位相关代码和日志；MCP 工具能帮上忙时就用。",
        "3. 证据必须注明出处（文件:行号、日志行、MCP 工具返回）。",
        "4. 输出结构：根因 / 证据 / 修复建议（按优先级）/ 置信度（高/中/低 + 理由）/"
        "还需要的信息。用简体中文，结构清晰，不要废话。",
        "5. 证据不足时明确说证据不足，不要编造根因。",
    ]
    return "\n".join(lines)


def resolve_repos(codex_cfg: dict, cfg: dict) -> list[str]:
    """诊断可用的仓库：优先 codex.repos；为空时回落到旧的 repo.path。只保留存在的目录。"""
    repos = [str(p) for p in (codex_cfg.get("repos") or []) if str(p).strip()]
    if not repos:
        legacy = (cfg.get("repo") or {}).get("path", "")
        if legacy:
            repos = [str(legacy)]
    ok = []
    for p in repos:
        if os.path.isdir(p):
            ok.append(p)
        else:
            log.warning("codex 仓库不存在，已跳过：%s", p)
    return ok


def diagnose_with_codex(issue_text: str, cfg: dict) -> str:
    """调 codex exec 做诊断，返回最终消息文本。失败抛 CodexError，由调用方兜底。"""
    codex_cfg = cfg.get("codex") or {}
    binary = codex_cfg.get("binary") or "codex"
    binary_path = shutil.which(binary)
    if not binary_path:
        raise CodexError(f"未找到 Codex CLI（{binary}），先跑 scripts/setup-codex.sh 安装")
    repos = resolve_repos(codex_cfg, cfg)
    if not repos:
        raise CodexError("codex.repos 为空且无可用仓库，未配置诊断仓库")

    # 认证预检：无 key、无登录时 codex exec 会一直等待而不是快速失败，
    # 必须在启动前拦住（不要等到 timeout 才兜底）。
    merged_env = dict(os.environ)
    merged_env.update({str(k): str(v) for k, v in (codex_cfg.get("env") or {}).items()})
    auth_src = os.path.join(
        os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex"),
        "auth.json",
    )
    has_key = bool(merged_env.get("OPENAI_API_KEY"))
    has_login = os.path.isfile(auth_src)
    if not has_key and not has_login:
        raise CodexError(
            "Codex 未认证：请设置 OPENAI_API_KEY，或用 ChatGPT 登录 Codex（否则 codex exec 会一直等待）"
        )

    timeout = int(codex_cfg.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
    with tempfile.TemporaryDirectory(prefix="feishu-diag-codex-") as tmp:
        codex_home = os.path.join(tmp, "codex-home")
        os.makedirs(codex_home, exist_ok=True)
        os.chmod(codex_home, 0o700)
        build_codex_home(codex_cfg, codex_home)
        if has_login:
            # ChatGPT 登录的凭据在 ~/.codex/auth.json；临时 CODEX_HOME 里也要有一份，
            # 否则 Codex 找不到登录状态。诊断完随临时目录删掉。
            auth_dst = os.path.join(codex_home, "auth.json")
            shutil.copyfile(auth_src, auth_dst)
            os.chmod(auth_dst, 0o600)
        out_file = os.path.join(tmp, "final.md")
        cmd = [binary_path, "exec", "-C", repos[0]]
        for extra in repos[1:]:
            cmd += ["--add-dir", extra]
        cmd += ["--output-last-message", out_file, build_prompt(issue_text, cfg)]
        env = dict(merged_env)
        env["CODEX_HOME"] = codex_home
        log.info("codex exec 启动：主仓库=%s 附加仓库=%d 超时=%ss", repos[0], len(repos) - 1, timeout)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            raise CodexError(f"Codex 诊断超时（>{timeout}s），已终止")
        except OSError as e:
            raise CodexError(f"启动 Codex CLI 失败：{e}")
        if proc.returncode != 0:
            raise CodexError(
                f"Codex CLI 退出码 {proc.returncode}：{(proc.stderr or '').strip()[-500:]}"
            )
        if not os.path.isfile(out_file):
            raise CodexError(f"Codex 未输出诊断结论（stdout 尾部：{(proc.stdout or '').strip()[-300:]}）")
        with open(out_file, encoding="utf-8") as f:
            result = f.read().strip()
        if not result:
            raise CodexError("Codex 输出的诊断结论为空")
        return result
