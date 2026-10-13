"""飞书诊断机器人入口：WebSocket 长连接收 @消息 -> 后台线程诊断 -> 群内回复。

关键点：
- 用飞书官方 SDK 的 WebSocket 长连接收事件，机器人只需出网，不用公网回调地址。
- 事件处理必须在 3 秒内返回，否则飞书会超时重试：handler 里只做解析 + 起后台线程，
  真正的诊断（查日志、查代码、调 LLM）在线程里做。
- 必填项（问题描述 / 时间 / 版本）缺失时先追问，不做弱诊断；同一人 30 分钟内
  再次 @机器人 视为补充信息，自动合并，集齐后再诊断。
- 先回一条 "收到，正在诊断" 的确认，避免测试人员以为机器人没反应。
"""

import json
import logging
import os
import re
import threading
import time

import yaml
import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)
from slots import (
    SESSION_TTL_SECONDS,
    extract_slots,
    format_missing_reply,
    merge_slots,
    required_missing,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("feishu-diag-bot")

CONFIG_PATH = os.environ.get("DIAG_BOT_CONFIG", os.path.join(os.path.dirname(__file__), "config.yaml"))

# 飞书重发事件去重（内存集合，重启即清；够用，重发只会导致一次重复诊断）
_seen_message_ids: set[str] = set()

# 待完善的问题会话：(chat_id, sender_id) -> {"text": str, "slots": dict, "updated": float}
# 内存存储，重启即清；30 分钟内同一人再次 @机器人 视为补充信息
_sessions: dict[tuple, dict] = {}

MENTION_TOKEN_RE = re.compile(r"@_user_\d+")


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    # 凭据从环境变量取，不写进配置文件
    cfg.setdefault("feishu", {})
    cfg["feishu"]["app_id"] = os.environ.get("FEISHU_APP_ID", cfg["feishu"].get("app_id", ""))
    cfg["feishu"]["app_secret"] = os.environ.get("FEISHU_APP_SECRET", cfg["feishu"].get("app_secret", ""))
    return cfg


CFG = load_config(CONFIG_PATH)

client = (
    lark.Client.builder()
    .app_id(CFG["feishu"]["app_id"])
    .app_secret(CFG["feishu"]["app_secret"])
    .build()
)


def send_text(chat_id: str, text: str, reply_to_message_id: str | None = None) -> None:
    """发文本消息。reply_to_message_id 有值时作为该消息的楼中楼回复。"""
    content = json.dumps({"text": text}, ensure_ascii=False)
    if reply_to_message_id:
        request = (
            ReplyMessageRequest.builder()
            .message_id(reply_to_message_id)
            .request_body(
                ReplyMessageRequestBody.builder().msg_type("text").content(content).build()
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
                .msg_type("text")
                .content(content)
                .build()
            )
            .build()
        )
        resp = client.im.v1.message.create(request)
    if not resp.success():
        log.error("send message failed: %s", resp.msg)


def strip_mentions(text: str) -> str:
    """去掉 @机器人 的占位符，剩下测试人员的问题描述。"""
    return MENTION_TOKEN_RE.sub("", text).strip()


def handle_issue(chat_id: str, message_id: str, sender_id: str, issue_text: str) -> None:
    """后台线程：必填项（描述/时间/版本）缺失时先追问；集齐后再诊断。"""
    from diagnose import diagnose

    key = (chat_id, sender_id)
    now = time.time()
    session = _sessions.get(key)
    if session and now - session["updated"] > SESSION_TTL_SECONDS:
        session = None  # 超时，旧问题作废

    fresh_slots = extract_slots(issue_text)
    if not required_missing(fresh_slots):
        # 新消息本身信息齐全：当作新问题，丢弃旧会话
        text, slots = issue_text, fresh_slots
        _sessions.pop(key, None)
    elif session:
        # 合并到待完善的问题里
        text, slots = merge_slots(session["text"], session["slots"], issue_text)
    else:
        text, slots = issue_text, fresh_slots

    missing = required_missing(slots)
    if missing:
        _sessions[key] = {"text": text, "slots": slots, "updated": now}
        send_text(chat_id, format_missing_reply(slots, missing), reply_to_message_id=message_id)
        return

    _sessions.pop(key, None)
    ack = CFG.get("reply", {}).get("ack_text", "收到，正在结合日志和代码诊断…")
    send_text(chat_id, ack, reply_to_message_id=message_id)
    try:
        result = diagnose(text, CFG)
    except Exception:  # noqa: BLE001 - 诊断异常也要回群里，不能悄悄丢掉
        log.exception("diagnose failed for message %s", message_id)
        result = "诊断过程出错，请查看机器人日志后重试。"
    send_text(chat_id, result, reply_to_message_id=message_id)


def do_p2_im_message_receive_v1(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
    """im.message.receive_v1 事件处理：只处理文本消息，其余让测试人员补文字。"""
    try:
        msg = data.event.message
        message_id = getattr(msg, "message_id", "")
        chat_id = getattr(msg, "chat_id", "")
        message_type = getattr(msg, "message_type", "")
        if not message_id or message_id in _seen_message_ids:
            return
        _seen_message_ids.add(message_id)
        if len(_seen_message_ids) > 1000:  # 防止集合无限增长
            _seen_message_ids.clear()

        if message_type != "text":
            send_text(
                chat_id,
                "只收到非文本消息（图片/文件）。请把报错原文、时间、请求 ID 以文字形式发出来，我再诊断。",
                reply_to_message_id=message_id,
            )
            return

        try:
            content = json.loads(getattr(msg, "content", "{}") or "{}")
            raw_text = content.get("text", "")
        except json.JSONDecodeError:
            raw_text = ""
        issue_text = strip_mentions(raw_text)
        if not issue_text:
            return

        # 3 秒超时：只起线程就返回，诊断在线程里做
        sender = getattr(data.event, "sender", None)
        sender_id = getattr(getattr(sender, "sender_id", None), "open_id", "") or chat_id
        threading.Thread(
            target=handle_issue, args=(chat_id, message_id, sender_id, issue_text), daemon=True
        ).start()
    except Exception:  # noqa: BLE001 - handler 抛异常会触发飞书重试，必须吞掉
        log.exception("event handler failed")


def main() -> None:
    if not CFG["feishu"]["app_id"] or not CFG["feishu"]["app_secret"]:
        raise SystemExit("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET 环境变量")
    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1)
        .build()
    )
    ws_client = lark.ws.Client(
        CFG["feishu"]["app_id"],
        CFG["feishu"]["app_secret"],
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
    )
    log.info("飞书诊断机器人启动，等待 @消息…")
    ws_client.start()


if __name__ == "__main__":
    main()
