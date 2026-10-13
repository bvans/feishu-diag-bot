"""飞书交互卡片表单（可选，未默认启用）。

背景：bot.py 目前缺必填项时用文字追问；本模块提供等价的卡片表单版：
测试人员填表点提交 -> card.action.trigger 回调 -> 校验 -> 集齐后进诊断。

设计要点（已按飞书官方文档 + lark-oapi SDK 实测核对）：
- 卡片是 JSON 2.0：schema "2.0"，config.update_multi=True，body.elements 里放一个
  tag="form" 的表单容器；表单内每个组件的 name 唯一且非空（为空会报 200530）。
- 提交按钮用 action_type="form_submit"（不是 behaviors 回调），提交后回调的
  action.form_value 是 {name: value}，picker_datetime 的值形如 "2024-04-29 07:07 +0800"。
- 回调必须在 3 秒内返回：handler 只做校验 + 起后台线程做诊断，立即回 toast。
  SDK 侧用 register_p2_card_action_trigger 注册，返回 P2CardActionTriggerResponse
 （toast + 可选 card；只回 toast 则卡片保持原样，可再次提交）。
- 回调订阅是独立配置：飞书后台「事件与回调 -> 回调配置」里订阅
  card.action.trigger 并选长连接，和 im.message.receive_v1 的事件订阅是两回事；
  没配会报 200340。配好后要发布新版本才生效。

本模块不依赖 lark_oapi（纯函数 + 可注入的 diagnose/send 依赖），可单测。
接线方法见 README 末尾「卡片表单（可选）」一节，默认不改 bot.py 现有行为。
"""

import re
import threading

# 表单字段名（= 回调 form_value 的 key，必须与 build_issue_card 里的 name 一致）
FIELD_DESCRIPTION = "description"
FIELD_TIME = "occurred_at"
FIELD_VERSION = "version"
FIELD_REQUEST_ID = "request_id"
FIELD_ENV = "env"

FORM_NAME = "diag_issue_form"
SUBMIT_KIND = "diag_form_submit"

ENV_OPTIONS = [
    ("生产", "prod"),
    ("预发", "staging"),
    ("测试", "test"),
]

# picker_datetime 回调值："2024-04-29 07:07 +0800" -> 去掉末尾时区，留 "2024-04-29 07:07"
_DATETIME_TZ_SUFFIX = re.compile(r"\s*[+-]\d{4}\s*$")
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def build_issue_card(prefill: dict | None = None, missing: list[str] | None = None) -> dict:
    """生成补填表单卡片。prefill：已收到的槽位，用于回填；missing：缺失项，用于标题提示。"""
    prefill = prefill or {}
    missing = missing or []
    if missing:
        hint = "还缺：" + "、".join(
            {"description": "问题描述", "time": "时间", "version": "版本"}.get(m, m) for m in missing
        )
        subtitle = f"{hint}，填完点提交即开始诊断。"
    else:
        subtitle = "填完点提交即开始诊断。"
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "enable_forward": True},
        "header": {
            "title": {"tag": "plain_text", "content": "补充问题信息"},
            "subtitle": {"tag": "plain_text", "content": subtitle},
            "template": "orange",
        },
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 12px 12px",
            "elements": [
                {
                    "tag": "form",
                    "name": FORM_NAME,
                    "elements": [
                        {
                            "tag": "input",
                            "name": FIELD_DESCRIPTION,
                            "label": {"tag": "plain_text", "content": "问题描述（必填）"},
                            "placeholder": {"tag": "plain_text", "content": "报错原文 + 现象，如：下单接口返回 500，点支付没反应"},
                            "input_type": "text",
                            "rows": 4,
                            "max_length": 1000,
                            "required": True,
                            "default_value": str(prefill.get("description", "") or ""),
                        },
                        {
                            "tag": "picker_datetime",
                            "name": FIELD_TIME,
                            "placeholder": {"tag": "plain_text", "content": "选择问题发生时间（必填）"},
                            "required": True,
                        },
                        {
                            "tag": "input",
                            "name": FIELD_VERSION,
                            "label": {"tag": "plain_text", "content": "版本（必填）"},
                            "placeholder": {"tag": "plain_text", "content": "如 v1.2.3"},
                            "input_type": "text",
                            "max_length": 100,
                            "required": True,
                            "default_value": str(prefill.get("version", "") or ""),
                        },
                        {
                            "tag": "input",
                            "name": FIELD_REQUEST_ID,
                            "label": {"tag": "plain_text", "content": "请求 ID / trace ID（选填）"},
                            "placeholder": {"tag": "plain_text", "content": "如 request_id=abc123 中的 abc123"},
                            "input_type": "text",
                            "max_length": 200,
                            "default_value": str(prefill.get("request_id", "") or ""),
                        },
                        {
                            "tag": "select_static",
                            "name": FIELD_ENV,
                            "placeholder": {"tag": "plain_text", "content": "选择环境（选填）"},
                            "options": [
                                {"text": {"tag": "plain_text", "content": label}, "value": value}
                                for label, value in ENV_OPTIONS
                            ],
                        },
                        {
                            "tag": "button",
                            "name": "submit",
                            "text": {"tag": "plain_text", "content": "提交，开始诊断"},
                            "type": "primary",
                            "action_type": "form_submit",
                        },
                    ],
                }
            ],
        },
    }


def _clean_str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        # 多选类组件在 form_value 里是数组；本表单没有多选，防御性取第一个
        return str(value[0]) if value else ""
    return str(value).strip()


def _clean_time(value) -> str:
    """picker_datetime 回调值去时区后缀；只选了日期时原样保留。"""
    v = _clean_str(value)
    if not v:
        return ""
    v = _DATETIME_TZ_SUFFIX.sub("", v).strip()
    return v


def parse_form_value(form_value: dict | None) -> dict:
    """把 card.action.trigger 的 action.form_value 转成 slots 字典（与 slots.py 同 shape）。"""
    form_value = form_value or {}
    return {
        "description": _clean_str(form_value.get(FIELD_DESCRIPTION)),
        "time": _clean_time(form_value.get(FIELD_TIME)),
        "version": _clean_str(form_value.get(FIELD_VERSION)),
        "env": _clean_str(form_value.get(FIELD_ENV)),
        "request_id": _clean_str(form_value.get(FIELD_REQUEST_ID)),
    }


def validate_slots(slots: dict) -> list[str]:
    """返回缺失/无效的必填项 key。描述复用 slots.description_ok 的标准（太短或只剩泛泛词不算）。"""
    from slots import description_ok

    missing: list[str] = []
    if not description_ok(slots.get("description", "")):
        missing.append("description")
    if not slots.get("time"):
        missing.append("time")
    if not slots.get("version"):
        missing.append("version")
    return missing


def merge_with_session(session_slots: dict | None, form_slots: dict, session_text: str = "") -> tuple[str, dict]:
    """表单值优先于会话旧值；描述取表单描述（表单就是让人重填的），其余字段表单为空时沿用旧值。"""
    old = session_slots or {}
    merged = {
        "description": form_slots.get("description") or old.get("description", ""),
        "time": form_slots.get("time") or old.get("time", ""),
        "version": form_slots.get("version") or old.get("version", ""),
        "env": form_slots.get("env") or old.get("env", ""),
        "request_id": form_slots.get("request_id") or old.get("request_id", ""),
    }
    text = merged["description"]
    if session_text and session_text not in text:
        text = (session_text + "\n" + merged["description"]).strip() if merged["description"] else session_text
    return text, merged


def submitted_card(message: str) -> dict:
    """提交成功后把卡片换成一行状态（避免重复提交），诊断结果仍以消息形式发到群里。"""
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": "已收到"},
            "template": "green",
        },
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 12px 12px",
            "elements": [
                {"tag": "markdown", "content": message},
            ],
        },
    }


def make_card_action_handler(*, get_session, save_session, clear_session, diagnose, send_text):
    """构造 card.action.trigger 的 SDK handler（依赖注入，便于单测与接线）。

    get_session(chat_id, open_id) -> (text, slots) | None
    save_session(chat_id, open_id, text, slots) -> None
    clear_session(chat_id, open_id) -> None
    diagnose(text, cfg) -> str（复用 diagnose.py 的 diagnose，cfg 由调用方闭包传入）
    send_text(chat_id, text, reply_to_message_id=None) -> None
    返回的 handler(data) -> P2CardActionTriggerResponse，3 秒内返回 toast，
    诊断在后台线程做、结果以消息发到群里。
    """
    import lark_oapi as lark
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

    def handler(data) -> P2CardActionTriggerResponse:
        try:
            event = data.event
            action = getattr(event, "action", None)
            form_value = getattr(action, "form_value", None) if action else None
            operator = getattr(event, "operator", None)
            context = getattr(event, "context", None)
            chat_id = getattr(context, "open_chat_id", "") or ""
            open_id = getattr(operator, "open_id", "") or ""
            form_slots = parse_form_value(dict(form_value) if form_value else {})
            session = get_session(chat_id, open_id) if chat_id else None
            old_text, old_slots = session if session else ("", None)
            text, merged = merge_with_session(old_slots, form_slots, old_text)
            missing = validate_slots(merged)
            if missing:
                labels = {"description": "问题描述", "time": "时间", "version": "版本"}
                return P2CardActionTriggerResponse(
                    {
                        "toast": {
                            "type": "warning",
                            "content": "还缺：" + "、".join(labels[m] for m in missing) + "，请补填后再次提交",
                        }
                    }
                )
            clear_session(chat_id, open_id)

            def _run():
                try:
                    result = diagnose(text)
                except Exception:  # noqa: BLE001 - 诊断异常也要回群里
                    result = "诊断过程出错，请查看机器人日志后重试。"
                send_text(chat_id, result)

            threading.Thread(target=_run, daemon=True).start()
            return P2CardActionTriggerResponse(
                {
                    "toast": {"type": "success", "content": "已收到，正在诊断"},
                    "card": {"type": "raw", "data": submitted_card("已收到，正在结合日志和代码诊断，结论会发到群里。")},
                }
            )
        except Exception:  # noqa: BLE001 - 回调抛异常会触发飞书重试，必须吞掉
            import logging

            logging.getLogger("feishu-diag-bot").exception("card action handler failed")
            return P2CardActionTriggerResponse(
                {"toast": {"type": "error", "content": "处理失败，请重试"}}
            )

    return handler


def send_card(client, chat_id: str, card: dict, reply_to_message_id: str | None = None) -> bool:
    """发交互卡片。reply_to_message_id 有值时作为该消息的楼中楼回复。"""
    import json

    from lark_oapi.api.im.v1 import (
        CreateMessageRequest,
        CreateMessageRequestBody,
        ReplyMessageRequest,
        ReplyMessageRequestBody,
    )

    content = json.dumps({"card": card}, ensure_ascii=False)
    if reply_to_message_id:
        request = (
            ReplyMessageRequest.builder()
            .message_id(reply_to_message_id)
            .request_body(
                ReplyMessageRequestBody.builder().msg_type("interactive").content(content).build()
            )
            .build()
        )
        resp = client.im.v1.message.reply(request)
    else:
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(content)
                .build()
            )
            .build()
        )
        resp = client.im.v1.message.create(request)
    return bool(resp.success())
