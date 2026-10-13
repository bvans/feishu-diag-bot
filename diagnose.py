"""诊断引擎：从测试人员的问题描述中提取信号 -> 查服务器日志 -> 查代码 -> LLM 诊断。

设计要点：
- 信号提取是纯规则（正则），不靠 LLM，保证快且可解释。
- 日志 / 代码检索是关键词 grep，不是语义搜索：测试人员给的报错原文越完整，
  命中率越高。README 里有给测试人员的发消息规范。
- LLM 只做综合判断：输入 = 问题描述 + 日志证据 + 代码证据，输出结构化诊断。
"""

import json
import os
import re
import subprocess

# ---------- 信号提取 ----------

# 栈追踪里的文件:行号，如 app/api/order.py:128
STACK_FILE_RE = re.compile(r"([A-Za-z0-9_./\-]+\.(?:py|go|js|ts|java|rb|php|cs|kt|rs)):(\d+)")
# 异常类名，如 NullPointerException / TimeoutError / panic
EXCEPTION_RE = re.compile(r"\b([A-Z][A-Za-z]*?(?:Error|Exception|Failure|Timeout|Panic))\b")
# 请求 / 追踪 ID，如 request_id=abc123、trace-id: xyz
TRACE_ID_RE = re.compile(
    r"\b(?:trace[_-]?id|request[_-]?id|req[_-]?id)\s*[:=]\s*([A-Za-z0-9\-_]{6,64})", re.I
)
# 时间戳 2026-10-13 15:04:05 或 2026-10-13T15:04:05
TIME_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\b")
# 明显的报错关键词（中英）
KEYWORD_RE = re.compile(
    r"(500|502|503|504|404|timeout|超时|连接拒绝|connection refused|"
    r"oom|内存溢出|out of memory|deadlock|死锁|崩溃|crash|闪退|"
    r"白屏|卡死|无响应|not found|unauthorized|forbidden|"
    r"数据库|db|sql|redis|mq|kafka|nginx)",
    re.I,
)


def extract_signals(text: str) -> dict:
    """从问题描述中提取可用于检索的信号。"""
    files = [(m.group(1), int(m.group(2))) for m in STACK_FILE_RE.finditer(text)]
    exceptions = sorted(set(EXCEPTION_RE.findall(text)))
    trace_ids = sorted(set(TRACE_ID_RE.findall(text)))
    times = TIME_RE.findall(text)
    keywords = sorted(set(k for k in KEYWORD_RE.findall(text) if len(k) >= 2))

    # 检索词：追踪 ID 最精确，其次异常名，再是关键词。去重保序。
    terms = []
    for t in trace_ids + exceptions + keywords:
        if t not in terms:
            terms.append(t)
    return {
        "terms": terms[:12],
        "files": files[:5],
        "exceptions": exceptions,
        "trace_ids": trace_ids,
        "times": times,
    }


# ---------- 日志检索 ----------


def _grep_file_literal(path: str, term: str, max_hits: int) -> list[str]:
    """在单个日志文件里按字面量搜 term，返回命中的行（带行号）。"""
    try:
        proc = subprocess.run(
            ["grep", "-F", "-i", "-n", "-m", str(max_hits), term, path],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    if proc.returncode != 0:
        return []
    return [line.rstrip("\n")[:500] for line in proc.stdout.splitlines()]


def search_logs(signals: dict, log_configs: list[dict], max_total: int = 40) -> list[str]:
    """在各日志文件里搜信号词，返回去重后的证据行。"""
    evidence: list[str] = []
    seen: set[str] = set()
    for log in log_configs:
        path = log.get("path", "")
        if not path or not os.path.isfile(path):
            continue
        for term in signals["terms"]:
            for line in _grep_file_literal(path, term, max_hits=10):
                key = f"{log.get('name', path)}:{line}"
                if key not in seen:
                    seen.add(key)
                    evidence.append(f"[{log.get('name', path)}] {line}")
                if len(evidence) >= max_total:
                    return evidence
    return evidence


# ---------- 代码检索 ----------


def _read_code_snippet(repo_path: str, rel_path: str, lineno: int, before: int = 10, after: int = 15) -> str:
    """按栈追踪的文件:行号读出上下文代码。"""
    full = os.path.join(repo_path, rel_path)
    if not os.path.isfile(full):
        # 可能是相对仓库根目录以外的路径，尝试按文件名尾巴匹配
        base = os.path.basename(rel_path)
        for root, _dirs, files in os.walk(repo_path):
            if base in files:
                full = os.path.join(root, base)
                break
        else:
            return ""
    try:
        with open(full, encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except OSError:
        return ""
    start = max(0, lineno - 1 - before)
    end = min(len(lines), lineno + after)
    numbered = "".join(f"{i + 1:>5}: {lines[i]}" for i in range(start, end))
    return f"--- {full}:{lineno} ---\n{numbered}"


def search_code(signals: dict, repo_cfg: dict, max_hits: int = 20) -> list[str]:
    """按栈追踪文件定位 + 关键词 grep 代码库，返回证据片段。"""
    repo_path = repo_cfg.get("path", "")
    if not repo_path or not os.path.isdir(repo_path):
        return []
    evidence: list[str] = []

    # 1) 栈追踪文件直接读上下文（最准）
    for rel_path, lineno in signals["files"]:
        snippet = _read_code_snippet(repo_path, rel_path, lineno)
        if snippet:
            evidence.append(snippet)

    # 2) 关键词 grep（异常名 / 检索词）
    include_args: list[str] = []
    for ext in repo_cfg.get("include_ext", [".py", ".go", ".js", ".ts", ".java"]):
        include_args += ["--include", f"*{ext}"]
    for term in signals["exceptions"] + signals["terms"]:
        if len(term) < 3:
            continue
        try:
            proc = subprocess.run(
                ["grep", "-rn", "-F", "-i", "-m", "5"] + include_args + [term, repo_path],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
        for line in proc.stdout.splitlines():
            line = line[:500]
            if line not in evidence:
                evidence.append(line)
            if len(evidence) >= max_hits:
                return evidence
    return evidence


# ---------- LLM 诊断 ----------

SYSTEM_PROMPT = """你是一名资深值班工程师（on-call），负责根据测试人员反馈的问题、
服务器日志证据和相关代码片段，给出诊断结论。

要求：
1. 先给根因判断，再列证据（引用日志行 / 代码行，注明出处）。
2. 给出可执行的修复建议，按优先级排序。
3. 如果证据不足以定论，明确说"证据不足"，并列出还需要什么信息（比如
   具体的请求 ID、报错时间、复现步骤），不要编造根因。
4. 置信度只能是 高 / 中 / 低，并说明理由。
5. 用简体中文，结构清晰，不要废话。

只输出 JSON，格式：
{
  "root_cause": "一句话根因",
  "evidence": ["证据1", "证据2"],
  "fix": ["修复建议1", "修复建议2"],
  "confidence": "高|中|低",
  "confidence_reason": "为什么是这个置信度",
  "need_more_info": ["还需要的信息1"]
}
"""


def llm_diagnose(issue_text: str, log_evidence: list[str], code_evidence: list[str], llm_cfg: dict) -> dict:
    """调用 OpenAI 兼容接口做诊断。失败时返回降级结果，不抛异常。"""
    from openai import OpenAI

    user_content = (
        f"【测试人员反馈】\n{issue_text}\n\n"
        f"【服务器日志证据】\n"
        + ("\n".join(log_evidence) if log_evidence else "（日志中未命中相关行）")
        + "\n\n【相关代码证据】\n"
        + ("\n".join(code_evidence) if code_evidence else "（代码库中未命中相关片段）")
    )
    try:
        client = OpenAI(
            api_key=os.environ.get(llm_cfg.get("api_key_env", "LLM_API_KEY"), ""),
            base_url=llm_cfg.get("base_url", ""),
        )
        resp = client.chat.completions.create(
            model=llm_cfg.get("model", "gemini-2.5-flash"),
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            temperature=0.2,
            timeout=60,
        )
        raw = resp.choices[0].message.content or ""
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            # 模型没按 JSON 返回时，把原文当根因返回，不丢失信息
            result = {
                "root_cause": raw[:1000],
                "evidence": [],
                "fix": [],
                "confidence": "低",
                "confidence_reason": "模型未返回结构化 JSON",
                "need_more_info": [],
            }
        return result
    except Exception as e:  # noqa: BLE001 - 诊断失败也要给测试人员一个交代
        return {
            "root_cause": f"诊断引擎调用失败：{e}",
            "evidence": [],
            "fix": ["请检查机器人日志与 LLM 配置后重试"],
            "confidence": "低",
            "confidence_reason": "LLM 调用异常",
            "need_more_info": [],
        }


def format_reply(result: dict) -> str:
    """把结构化诊断转成发到群里的中文文本。"""
    lines = ["【诊断结论】", f"根因：{result.get('root_cause', '未知')}"]
    ev = result.get("evidence") or []
    if ev:
        lines.append("证据：")
        lines += [f"- {e}" for e in ev[:8]]
    fix = result.get("fix") or []
    if fix:
        lines.append("修复建议：")
        lines += [f"{i + 1}. {f}" for i, f in enumerate(fix[:5])]
    lines.append(
        f"置信度：{result.get('confidence', '低')}（{result.get('confidence_reason', '')}）"
    )
    need = result.get("need_more_info") or []
    if need:
        lines.append("还需要补充：")
        lines += [f"- {n}" for n in need[:5]]
    return "\n".join(lines)


def diagnose(issue_text: str, cfg: dict) -> str:
    """完整诊断流水线：信号 -> 日志 -> 代码 -> LLM -> 回复文本。"""
    signals = extract_signals(issue_text)
    logs = search_logs(signals, cfg.get("logs", []), max_total=cfg.get("reply", {}).get("max_log_lines", 40))
    code = search_code(signals, cfg.get("repo", {}), max_hits=cfg.get("reply", {}).get("max_code_hits", 20))
    result = llm_diagnose(issue_text, logs, code, cfg.get("llm", {}))
    return format_reply(result)
