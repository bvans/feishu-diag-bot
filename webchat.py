"""网页聊天窗口：不用飞书，在浏览器里直接和诊断机器人对话。

和飞书群里 @机器人 是同一套逻辑：必填项（问题描述 / 时间 / 版本）缺失时先追问，
30 分钟内补充自动合并，集齐后查日志查代码做诊断。

用法：
    uv run python webchat.py [config.yaml]
    # 打开 http://127.0.0.1:8080
端口：WEBCHAT_PORT（默认 8080）；只绑 127.0.0.1，不做公网暴露（无鉴权）。
不需要飞书凭据，只读 config.yaml 里的诊断配置（codex / llm / logs / repo）。

接口：
- GET /                            聊天页面
- POST /api/chat {session_id, text} -> {"type": "ask", "reply"}（缺必填项，立即返回）
                                  -> {"type": "ack", "task_id", "reply"}（已开始诊断）
- GET /api/result?task_id=..       -> {"status": "pending"} / {"status": "done", "reply"}
"""

import json
import logging
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import yaml

from slots import (
    SESSION_TTL_SECONDS,
    extract_slots,
    format_missing_reply,
    merge_slots,
    required_missing,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("feishu-diag-webchat")

CFG: dict = {}
_lock = threading.Lock()
_sessions: dict[str, dict] = {}  # session_id -> {"text": str, "slots": dict, "updated": float}
_tasks: dict[str, dict] = {}  # task_id -> {"status": "pending"|"done", "reply": str}


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def run_diagnosis(text: str, cfg: dict) -> str:
    """跑一次诊断（单测可 monkeypatch）。"""
    from diagnose import diagnose

    return diagnose(text, cfg)


def _diagnose_in_background(task_id: str, text: str, cfg: dict) -> None:
    try:
        reply = run_diagnosis(text, cfg)
    except Exception:  # noqa: BLE001 - 诊断异常也要回给页面，不能悄悄丢掉
        log.exception("diagnose failed for task %s", task_id)
        reply = "诊断过程出错，请查看日志后重试。"
    with _lock:
        _tasks[task_id] = {"status": "done", "reply": reply}


def handle_chat(session_id: str, text: str) -> dict:
    """和 bot.handle_issue 相同的会话逻辑，只是返回字符串而不是发飞书消息。"""
    import time

    now = time.time()
    with _lock:
        session = _sessions.get(session_id)
        if session and now - session["updated"] > SESSION_TTL_SECONDS:
            session = None  # 超时，旧问题作废

    fresh_slots = extract_slots(text)
    if not required_missing(fresh_slots):
        # 新消息本身信息齐全：当作新问题，丢弃旧会话
        full_text, slots = text, fresh_slots
        with _lock:
            _sessions.pop(session_id, None)
    elif session:
        full_text, slots = merge_slots(session["text"], session["slots"], text)
    else:
        full_text, slots = text, fresh_slots

    missing = required_missing(slots)
    if missing:
        with _lock:
            _sessions[session_id] = {"text": full_text, "slots": slots, "updated": now}
        return {"type": "ask", "reply": format_missing_reply(slots, missing)}

    with _lock:
        _sessions.pop(session_id, None)
    task_id = uuid.uuid4().hex
    with _lock:
        _tasks[task_id] = {"status": "pending", "reply": ""}
    threading.Thread(target=_diagnose_in_background, args=(task_id, full_text, CFG), daemon=True).start()
    ack = CFG.get("reply", {}).get("ack_text", "收到，正在结合日志和代码诊断…")
    return {"type": "ack", "task_id": task_id, "reply": ack}


PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>诊断机器人 · 网页聊天</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;margin:0;background:#f4f5f7;color:#222}
header{background:#3370ff;color:#fff;padding:12px 16px;font-size:16px}
header small{display:block;font-size:12px;opacity:.85;margin-top:2px}
#chat{max-width:760px;margin:0 auto;padding:16px 12px 110px}
.msg{max-width:80%;padding:10px 14px;border-radius:12px;margin:8px 0;white-space:pre-wrap;word-break:break-word;line-height:1.6;font-size:14px}
.user{background:#3370ff;color:#fff;margin-left:auto;border-bottom-right-radius:2px}
.bot{background:#fff;margin-right:auto;border-bottom-left-radius:2px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
#bar{position:fixed;left:0;right:0;bottom:0;background:#fff;border-top:1px solid #ddd;padding:10px}
#bar-inner{max-width:760px;margin:0 auto;display:flex;gap:8px}
#input{flex:1;resize:none;padding:10px;border:1px solid #ccc;border-radius:8px;font-size:14px}
button{padding:0 18px;border:0;border-radius:8px;background:#3370ff;color:#fff;font-size:14px;cursor:pointer}
button:disabled{opacity:.5;cursor:default}
#new{margin:8px 0 0;background:#fff;color:#3370ff;border:1px solid #3370ff;padding:6px 12px;border-radius:6px;cursor:pointer;font-size:13px}
#status{font-size:12px;color:#888;text-align:center;margin:8px 0}
</style></head>
<body>
<header>诊断机器人 · 网页聊天<small>和飞书群里 @机器人 同一套逻辑：先说清问题描述、时间、版本，集齐后开始诊断。诊断可能需要一两分钟，请耐心等待。</small></header>
<div id="chat"></div>
<div id="bar"><div id="bar-inner">
<textarea id="input" rows="2" placeholder="例如：下单接口返回 500，点支付没反应，15:04 左右，版本 v1.2.3"></textarea>
<button id="send">发送</button></div>
<div style="max-width:760px;margin:0 auto"><button id="new">新会话</button></div></div>
<script>
let sessionId = localStorage.getItem('webchat_session') || (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()));
localStorage.setItem('webchat_session', sessionId);
const chat = document.getElementById('chat'), input = document.getElementById('input'),
      sendBtn = document.getElementById('send');
function addMsg(text, cls){ const d = document.createElement('div'); d.className = 'msg ' + cls; d.textContent = text; chat.appendChild(d); window.scrollTo(0, document.body.scrollHeight); return d; }
function setBusy(b){ input.disabled = b; sendBtn.disabled = b; }
async function poll(taskId, statusEl){
  while (true){
    await new Promise(r => setTimeout(r, 2000));
    const res = await fetch('/api/result?task_id=' + encodeURIComponent(taskId));
    const data = await res.json();
    if (data.status === 'done'){ statusEl.remove(); addMsg(data.reply, 'bot'); setBusy(false); break; }
  }
}
async function send(){
  const text = input.value.trim(); if (!text) return;
  addMsg(text, 'user'); input.value = ''; setBusy(true);
  try {
    const res = await fetch('/api/chat', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({session_id: sessionId, text})});
    const data = await res.json();
    if (data.type === 'ask'){ addMsg(data.reply, 'bot'); setBusy(false); }
    else { addMsg(data.reply, 'bot'); const s = addMsg('正在诊断…', 'bot'); s.id = 'status'; poll(data.task_id, s); }
  } catch(e){ addMsg('请求失败：' + e, 'bot'); setBusy(false); }
}
sendBtn.onclick = send;
input.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); send(); } });
document.getElementById('new').onclick = () => {
  sessionId = crypto.randomUUID ? crypto.randomUUID() : String(Date.now());
  localStorage.setItem('webchat_session', sessionId);
  chat.innerHTML = ''; addMsg('新会话已开始，请描述问题（描述 / 时间 / 版本）。', 'bot');
};
addMsg('你好，请描述问题（描述 / 时间 / 版本），例如：下单接口返回 500，15:04 左右，版本 v1.2.3', 'bot');
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 安静：不把每次请求打到日志里
        pass

    def _send_json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/result":
            task_id = parse_qs(parsed.query).get("task_id", [""])[0]
            with _lock:
                task = _tasks.get(task_id)
            if task is None:
                self._send_json({"error": "unknown task_id"}, 404)
                return
            self._send_json(task)
            return
        self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/chat":
            self._send_json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            self._send_json({"error": "bad json"}, 400)
            return
        session_id = str(data.get("session_id") or "").strip()
        text = str(data.get("text") or "").strip()
        if not session_id or not text:
            self._send_json({"error": "session_id and text are required"}, 400)
            return
        self._send_json(handle_chat(session_id, text))


def create_server(host: str, port: int, cfg: dict) -> ThreadingHTTPServer:
    global CFG
    CFG = cfg
    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    import sys

    config_path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
        "DIAG_BOT_CONFIG", os.path.join(os.path.dirname(__file__), "config.yaml")
    )
    cfg = load_config(config_path)
    host = os.environ.get("WEBCHAT_HOST", "127.0.0.1")
    port = int(os.environ.get("WEBCHAT_PORT", "8080"))
    server = create_server(host, port, cfg)
    log.info("网页聊天窗口：http://%s:%s", host, server.server_address[1])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
