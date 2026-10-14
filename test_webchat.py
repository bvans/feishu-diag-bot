"""webchat.py 单测：页面可达、缺必填项追问、集齐后后台诊断、会话隔离（诊断函数 stub，不调 LLM）。"""

import json
import os
import sys
import threading
import time
import urllib.request

sys.path.insert(0, __file__.rsplit("/", 1)[0])

import webchat


def _req(method, url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read() or b"{}")


def test_webchat_flow():
    webchat.run_diagnosis = lambda text, cfg: f"诊断结论：{text[:20]}"
    server = webchat.create_server("127.0.0.1", 0, {"reply": {"ack_text": "ACK"}})
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(base + "/", timeout=10) as resp:
            assert resp.status == 200 and "网页聊天" in resp.read().decode("utf-8")

        # 缺必填项 -> 追问
        r = _req("POST", base + "/api/chat", {"session_id": "s1", "text": "下单接口返回 500，点支付没反应"})
        assert r["type"] == "ask" and "信息不全" in r["reply"]

        # 补充集齐 -> ack + 轮询拿到诊断结论
        r = _req("POST", base + "/api/chat", {"session_id": "s1", "text": "15:04 左右，版本 v1.2.3"})
        assert r["type"] == "ack" and r["reply"] == "ACK"
        deadline = time.time() + 10
        while True:
            got = _req("GET", base + f"/api/result?task_id={r['task_id']}")
            if got["status"] == "done":
                assert got["reply"].startswith("诊断结论：")
                break
            assert time.time() < deadline
            time.sleep(0.2)

        # 一条消息信息齐全 -> 直接 ack
        r = _req("POST", base + "/api/chat",
                 {"session_id": "s2", "text": "下单接口返回 500，点支付没反应，15:04 左右，版本 v1.2.3"})
        assert r["type"] == "ack"

        # 不同会话不合并：s3 只发了半句，仍追问
        r = _req("POST", base + "/api/chat", {"session_id": "s3", "text": "下单接口返回 500，点支付没反应"})
        assert r["type"] == "ask"
    finally:
        server.shutdown()


if __name__ == "__main__":
    test_webchat_flow()
    print("PASS test_webchat_flow")
    print("ALL PASS")
