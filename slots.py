"""必填信息槽位：问题描述 / 时间 / 版本。

机器人不做"弱诊断"：必填项缺失时先追问，集齐后再查日志查代码。
支持分多次补充：同一个人（同群）在 30 分钟内再次 @机器人 时，自动合并到待完善的问题里。
"""

import re
from datetime import datetime

SESSION_TTL_SECONDS = 30 * 60

REQUIRED = ("description", "time", "version")

LABEL = {
    "description": "问题描述",
    "time": "时间",
    "version": "版本",
}

# ---------- 时间 ----------

_FULL_DT = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?")
_HM = re.compile(r"(?<!\d)(\d{1,2}:\d{2}(?::\d{2})?)(?!\d)")
_DAY_WORD = re.compile(r"(今天|今日|昨天|前天)")
_NOW_WORD = re.compile(r"(刚才|刚刚)")


def extract_time(text: str) -> str:
    """抽时间。完整日期时间 / 某天 + 时分 / 单独时分 / 刚才 都算；光说"今天"不算。"""
    m = _FULL_DT.search(text)
    if m:
        return m.group(0)
    if _NOW_WORD.search(text):
        return f"刚才（{datetime.now().strftime('%H:%M')} 左右）"
    day = _DAY_WORD.search(text)
    hm = _HM.search(text)
    if day and hm:
        return f"{day.group(1)} {hm.group(1)}"
    if hm:
        return f"今天 {hm.group(1)}"
    return ""


# ---------- 版本 ----------

_VERSION = re.compile(
    r"(?:版本|version|ver\.?|build|版)\s*[:：=]?\s*(v?\d+\.\d+(?:\.\d+)?(?:[-.][A-Za-z0-9]+)?)"
    r"|(?<![\d.])v\d+\.\d+(?:\.\d+)?(?!\.\d)"
    r"|最新版|最新版本|latest",
    re.I,
)


def extract_version(text: str) -> str:
    m = _VERSION.search(text)
    return m.group(0) if m else ""


# ---------- 问题描述 ----------

_GENERIC_WORDS = [
    "问题", "挂了", "报错", "崩了", "不行", "异常", "失败", "错误",
    "有问题", "出问题", "坏了", "不能用", "无法使用", "闪退", "卡了",
    "卡死", "白屏", "出bug", "bug",
]


def description_ok(desc: str) -> bool:
    """有效描述：长度够，且不是只剩泛泛而谈的词。"""
    if len(desc) < 8:
        return False
    tmp = desc
    for w in _GENERIC_WORDS:
        tmp = tmp.replace(w, "")
    tmp = re.sub(r"[，。、；：？！,.!?:;@\s]", "", tmp)
    return len(tmp) >= 4


# ---------- 环境 / 请求 ID（选填，诊断时用得上） ----------

_ENV = re.compile(r"(生产|预发|测试环境|测试|staging|prod|test)", re.I)
_TRACE_ID = re.compile(
    r"\b(?:trace[_-]?id|request[_-]?id|req[_-]?id)\s*[:=]\s*([A-Za-z0-9\-_]{6,64})", re.I
)


def extract_slots(text: str) -> dict:
    """从一条消息中提取槽位。"""
    desc = re.sub(r"\s+", " ", text).strip()
    env_m = _ENV.search(text)
    trace_m = _TRACE_ID.search(text)
    return {
        "description": desc if description_ok(desc) else "",
        "time": extract_time(text),
        "version": extract_version(text),
        "env": env_m.group(1) if env_m else "",
        "request_id": trace_m.group(1) if trace_m else "",
    }


def required_missing(slots: dict) -> list[str]:
    """返回缺失的必填项 key 列表。"""
    return [k for k in REQUIRED if not slots.get(k)]


def merge_slots(old_text: str, old_slots: dict, new_text: str) -> tuple[str, dict]:
    """把补充消息合并进待完善的问题：文本追加，槽位优先取新值。"""
    new_slots = extract_slots(new_text)
    merged_text = (old_text + "\n" + new_text).strip()
    merged = {}
    for k in ("description", "time", "version", "env", "request_id"):
        merged[k] = new_slots.get(k) or old_slots.get(k, "")
    # 描述取合并后的全文（只要全文够具体）
    merged["description"] = merged_text if description_ok(merged_text) else ""
    return merged_text, merged


def format_missing_reply(slots: dict, missing: list[str]) -> str:
    """追问文案：列出缺什么 + 格式示例 + 已收到什么。"""
    hint = {
        "description": "问题描述：报错原文 + 现象，如“下单接口返回 500，点支付没反应”",
        "time": "时间：问题发生的时间，如 15:04 或 2026-10-13 15:04",
        "version": "版本：如 v1.2.3 或 版本 1.2.3",
    }
    lines = ["信息不全，先不诊断，免得误诊。请 @我 补充以下信息："]
    lines += [f"- {hint[k]}" for k in missing]
    got = [LABEL[k] for k in REQUIRED if k not in missing]
    if got:
        lines.append(f"已收到：{'、'.join(got)}")
    return "\n".join(lines)
