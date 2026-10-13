"""card_form.py 单测：卡片结构、回调解析、校验、合并、handler（用假依赖，不连飞书）。"""

import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])

from card_form import (
    FIELD_DESCRIPTION,
    FIELD_ENV,
    FIELD_REQUEST_ID,
    FIELD_TIME,
    FIELD_VERSION,
    build_issue_card,
    make_card_action_handler,
    merge_with_session,
    parse_form_value,
    submitted_card,
    validate_slots,
)


def test_card_structure():
    card = build_issue_card(prefill={"description": "下单接口 500", "version": "v1.2.3"}, missing=["time"])
    assert card["schema"] == "2.0"
    form = card["body"]["elements"][0]
    assert form["tag"] == "form"
    names = [e.get("name") for e in form["elements"]]
    assert names == [FIELD_DESCRIPTION, FIELD_TIME, FIELD_VERSION, FIELD_REQUEST_ID, FIELD_ENV, "submit"], names
    assert len(set(n for n in names if n)) == len([n for n in names if n])  # name 唯一非空
    submit = form["elements"][-1]
    assert submit["action_type"] == "form_submit"
    # 必填三项都有 required
    required = {e["name"] for e in form["elements"] if e.get("required")}
    assert required == {FIELD_DESCRIPTION, FIELD_TIME, FIELD_VERSION}, required
    # 回填生效
    assert form["elements"][0]["default_value"] == "下单接口 500"
    assert form["elements"][2]["default_value"] == "v1.2.3"
    assert "时间" in card["header"]["subtitle"]["content"]


def test_parse_form_value_strips_timezone():
    slots = parse_form_value(
        {
            FIELD_DESCRIPTION: "下单接口返回 500，点支付没反应",
            FIELD_TIME: "2026-10-13 15:04 +0800",
            FIELD_VERSION: "v1.2.3",
            FIELD_REQUEST_ID: "abc123",
            FIELD_ENV: "prod",
        }
    )
    assert slots["time"] == "2026-10-13 15:04", slots
    assert slots["version"] == "v1.2.3"
    assert slots["request_id"] == "abc123"
    assert slots["env"] == "prod"
    assert validate_slots(slots) == []


def test_validate_missing():
    assert set(validate_slots({"description": "挂了", "time": "", "version": ""})) == {
        "description",
        "time",
        "version",
    }
    assert validate_slots({"description": "下单接口返回 500，点支付没反应", "time": "今天 15:04", "version": "v1.2.3"}) == []


def test_merge_prefers_form_falls_back_to_session():
    text, merged = merge_with_session(
        {"description": "旧描述下单 500", "time": "今天 15:00", "version": "", "env": "", "request_id": ""},
        {"description": "下单接口返回 500，点支付没反应", "time": "", "version": "v1.2.3", "env": "", "request_id": ""},
        "旧描述下单 500",
    )
    assert merged["time"] == "今天 15:00"  # 表单没填时间，沿用会话
    assert merged["version"] == "v1.2.3"
    assert "下单接口返回 500" in text


def test_handler_missing_returns_warning_toast_and_no_diagnose():
    import lark_oapi as lark  # noqa: F401 - handler 工厂需要 SDK 在场
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger

    calls = {"diagnose": 0}

    def fake_diagnose(text):
        calls["diagnose"] += 1
        return "ok"

    handler = make_card_action_handler(
        get_session=lambda c, o: None,
        save_session=lambda *a: None,
        clear_session=lambda *a: None,
        diagnose=fake_diagnose,
        send_text=lambda *a, **k: None,
    )
    data = P2CardActionTrigger(
        {
            "event": {
                "operator": {"open_id": "ou_test"},
                "action": {"form_value": {FIELD_DESCRIPTION: "挂了", FIELD_TIME: "", FIELD_VERSION: ""}},
                "context": {"open_chat_id": "oc_test", "open_message_id": "om_test"},
            }
        }
    )
    resp = handler(data)
    assert resp.toast.type == "warning", resp.toast
    assert calls["diagnose"] == 0  # 缺项不诊断


def test_handler_complete_spawns_diagnose():
    import threading
    import time

    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger

    sent = []

    def fake_diagnose(text):
        assert "下单接口返回 500" in text
        return "诊断结论"

    handler = make_card_action_handler(
        get_session=lambda c, o: None,
        save_session=lambda *a: None,
        clear_session=lambda *a: None,
        diagnose=fake_diagnose,
        send_text=lambda chat_id, text, **k: sent.append(text),
    )
    data = P2CardActionTrigger(
        {
            "event": {
                "operator": {"open_id": "ou_test"},
                "action": {
                    "form_value": {
                        FIELD_DESCRIPTION: "下单接口返回 500，点支付没反应",
                        FIELD_TIME: "2026-10-13 15:04 +0800",
                        FIELD_VERSION: "v1.2.3",
                    }
                },
                "context": {"open_chat_id": "oc_test", "open_message_id": "om_test"},
            }
        }
    )
    resp = handler(data)
    assert resp.toast.type == "success"
    assert resp.card.type == "raw" and resp.card.data["schema"] == "2.0"
    # 后台线程把诊断结果发到群里
    for _ in range(50):
        if sent:
            break
        time.sleep(0.1)
    assert sent == ["诊断结论"], sent


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL PASS")
    print("submitted_card schema:", submitted_card("x")["schema"])
