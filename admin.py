"""管理配置界面：在浏览器里人工修改部署需要的配置。

用法：
    uv run python admin.py [配置目录]
    # 打开 http://127.0.0.1:8081
端口：ADMIN_PORT（默认 8081）；只绑 127.0.0.1，不做公网暴露（无鉴权，
且 .env 里的密钥会明文显示，绝不要共享屏幕/截图、不要把端口暴露出去）。

可管理的文件（都在配置目录里，只认这四个名字，防路径穿越）：
- config.yaml               机器人配置（飞书/LLM/日志/仓库/提示词/Codex/回复）
- cliproxyapi-config.yaml   代理配置（端口/客户端 key/上游端点/模型别名）
- .env                      环境变量凭据（飞书 App 密钥、各 API key）
- codex-repos.txt           build 时烘焙进业务镜像的仓库清单（每行一个 URL）
- AGENTS.md                 Codex 全局指令（下次诊断即生效，无需重启）

安全机制：
- 保存前按类型校验（YAML 必须能解析；.env 每行必须是 KEY=VALUE），
  校验失败不写盘，原文件不动。
- 每次保存前自动备份旧文件（<文件名>.bak.<时间戳>，只保留最近 5 份），
  可一键恢复；恢复前也会先备份当前文件。
- 文件不存在时可从同名的 .example 示例创建（codex-repos.txt 无示例，建空文件）。

注意：保存只改文件，不重启服务。配置生效规则：
- config.yaml / cliproxyapi-config.yaml / .env：重启容器后生效
  （docker compose restart，或 docker compose up -d）。
- codex-repos.txt：需重新 build 业务镜像（scripts/build.sh）才能烘焙新仓库。
"""

import glob
import json
import logging
import os
import re
import shutil
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("feishu-diag-admin")

CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_BACKUPS = 5

# name -> (kind, hint)
FILES = {
    "config.yaml": ("yaml", "保存后重启容器生效：docker compose restart"),
    "cliproxyapi-config.yaml": ("yaml", "保存后重启容器生效：docker compose restart"),
    ".env": ("env", "保存后重启容器生效：docker compose restart"),
    "codex-repos.txt": ("text", "保存后需重新 build 业务镜像：scripts/build.sh"),
    "AGENTS.md": ("text", "保存后下次诊断即生效，无需重启（每次诊断重新读取）"),
}

ENV_LINE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")


def _path(name: str) -> str:
    """只允许 FILES 里的名字，杜绝路径穿越。"""
    if name not in FILES:
        raise ValueError(f"不允许的文件名：{name}")
    return os.path.join(CONFIG_DIR, name)


def validate(name: str, content: str) -> str | None:
    """按类型校验；返回 None 表示通过，否则返回错误说明。"""
    if name not in FILES:
        raise ValueError(f"不允许的文件名：{name}")
    kind = FILES[name][0]
    if kind == "yaml":
        try:
            yaml.safe_load(content)
        except yaml.YAMLError as e:
            return f"YAML 解析失败：{e}"
    elif kind == "env":
        for i, line in enumerate(content.splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if not ENV_LINE_RE.match(stripped):
                return f".env 第 {i} 行格式不对，应为 KEY=VALUE：{line}"
    return None


def _backup_path(name: str) -> str:
    # 微秒级时间戳：一秒内多次保存/恢复也不会撞名覆盖旧备份
    ts = datetime.now().strftime("%Y%m%d%H%M%S%f")
    return os.path.join(CONFIG_DIR, f"{name}.bak.{ts}")


def _prune_backups(name: str) -> None:
    pattern = os.path.join(CONFIG_DIR, f"{name}.bak.*")
    backups = sorted(glob.glob(pattern), reverse=True)
    for old in backups[MAX_BACKUPS:]:
        try:
            os.remove(old)
        except OSError:
            pass


def list_backups(name: str) -> list[str]:
    pattern = os.path.join(CONFIG_DIR, f"{name}.bak.*")
    return [os.path.basename(p) for p in sorted(glob.glob(pattern), reverse=True)]


def save_file(name: str, content: str) -> dict:
    """校验 -> 备份旧文件 -> 写盘。返回 {ok, backup}。"""
    path = _path(name)  # 先校验文件名，防路径穿越
    err = validate(name, content)
    if err:
        raise ValueError(err)
    backup = None
    if os.path.isfile(path):
        backup = _backup_path(name)
        shutil.copy2(path, backup)
        _prune_backups(name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    log.info("已保存 %s（备份：%s）", name, backup or "无")
    return {"ok": True, "backup": os.path.basename(backup) if backup else None}


def restore_file(name: str, backup: str) -> dict:
    """从备份恢复；恢复前先备份当前文件。"""
    _path(name)  # 先校验文件名，防路径穿越
    if backup not in list_backups(name):
        raise ValueError(f"备份不存在：{backup}")
    path = _path(name)
    if os.path.isfile(path):
        cur = _backup_path(name)
        shutil.copy2(path, cur)
        _prune_backups(name)
    shutil.copy2(os.path.join(CONFIG_DIR, backup), path)
    log.info("已从备份恢复 %s <- %s", name, backup)
    return {"ok": True}


def example_path(name: str) -> str:
    """示例文件名：config.yaml -> config.example.yaml，.env -> .env.example。"""
    if name == ".env":
        return os.path.join(CONFIG_DIR, ".env.example")
    stem, ext = os.path.splitext(name)
    return os.path.join(CONFIG_DIR, f"{stem}.example{ext}")


def create_from_example(name: str) -> dict:
    """文件不存在时从 .example 示例创建（codex-repos.txt 建空文件）。"""
    path = _path(name)
    if os.path.isfile(path):
        raise ValueError(f"{name} 已存在，无需从示例创建")
    example = example_path(name)
    if name == "codex-repos.txt":
        content = ""
    elif os.path.isfile(example):
        with open(example, encoding="utf-8") as f:
            content = f.read()
    else:
        raise ValueError(f"没有可用的示例文件：{os.path.basename(example)}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return {"ok": True}


PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>管理配置 · 诊断机器人</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;margin:0;background:#f4f5f7;color:#222}
header{background:#1a1a2e;color:#fff;padding:12px 16px;font-size:16px}
header small{display:block;font-size:12px;opacity:.85;margin-top:2px}
#main{max-width:900px;margin:0 auto;padding:16px 12px}
#tabs{display:flex;gap:6px;margin-bottom:12px;flex-wrap:wrap}
.tab{padding:8px 16px;border:1px solid #ccc;background:#fff;border-radius:8px;cursor:pointer;font-size:14px}
.tab.active{background:#3370ff;color:#fff;border-color:#3370ff}
#hint{font-size:13px;color:#666;margin-bottom:8px}
#editor{width:100%;height:55vh;box-sizing:border-box;padding:12px;border:1px solid #ccc;border-radius:8px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;line-height:1.5;white-space:pre;overflow:auto}
#bar{margin-top:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
button{padding:8px 18px;border:0;border-radius:8px;background:#3370ff;color:#fff;font-size:14px;cursor:pointer}
button.secondary{background:#fff;color:#3370ff;border:1px solid #3370ff}
button:disabled{opacity:.5;cursor:default}
#status{font-size:13px;margin-top:8px;min-height:20px}
#status.ok{color:#0a7a42}#status.err{color:#c00}
#backups{margin-top:12px;font-size:13px}
#backups li{margin:4px 0}
#missing{color:#c00;font-size:14px}
.warn{background:#fff3cd;border:1px solid #ffc107;border-radius:8px;padding:10px;font-size:13px;margin-bottom:12px}
</style></head>
<body>
<header>管理配置 · 诊断机器人<small>仅限本机访问。.env 里的密钥会明文显示，不要共享屏幕/截图、不要把端口暴露到公网。保存只改文件，不重启服务。</small></header>
<div id="main">
<div class="warn">⚠️ 保存前会自动备份旧文件（保留最近 5 份），校验失败不写盘。改完按下方提示重启或重新 build 才生效。</div>
<div id="tabs"></div>
<div id="hint"></div>
<div id="missing"></div>
<textarea id="editor" spellcheck="false"></textarea>
<div id="bar"><button id="save">保存</button><button id="reload" class="secondary">重新加载</button></div>
<div id="status"></div>
<div id="backups"></div>
</div>
<script>
const FILES = ["config.yaml", "cliproxyapi-config.yaml", ".env", "codex-repos.txt"];
let current = FILES[0];
async function api(path, payload){
  const opt = payload ? {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)} : {};
  const res = await fetch(path, opt);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || ('请求失败 ' + res.status));
  return data;
}
function setStatus(text, ok){ const s = document.getElementById('status'); s.textContent = text; s.className = ok ? 'ok' : 'err'; }
async function loadBackups(){
  const data = await api('/api/backups?name=' + encodeURIComponent(current));
  const box = document.getElementById('backups');
  if (!data.backups.length){ box.innerHTML = ''; return; }
  box.innerHTML = '<b>备份（点恢复）：</b><ul>' + data.backups.map(b =>
    '<li>' + b + ' <button class="secondary" onclick="restore(\\'' + b + '\\')">恢复</button></li>').join('') + '</ul>';
}
async function restore(backup){
  if (!confirm('从备份恢复 ' + backup + '？当前文件会先备份。')) return;
  await api('/api/restore', {name: current, backup});
  setStatus('已从备份恢复：' + backup, true);
  await loadFile();
}
async function loadFile(){
  const data = await api('/api/file?name=' + encodeURIComponent(current));
  document.getElementById('hint').textContent = data.hint;
  const miss = document.getElementById('missing');
  if (!data.exists){
    miss.innerHTML = '文件不存在。' + (data.has_example ? '<button class="secondary" onclick="createFromExample()">从示例创建</button>' : '<button class="secondary" onclick="createFromExample()">创建空文件</button>');
    document.getElementById('editor').value = '';
  } else { miss.innerHTML = ''; document.getElementById('editor').value = data.content; }
  setStatus('', true);
  await loadBackups();
}
async function createFromExample(){
  await api('/api/create-from-example', {name: current});
  setStatus('已创建：' + current, true);
  await loadFile();
}
async function select(name){
  current = name;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.name === name));
  await loadFile();
}
document.getElementById('tabs').innerHTML = FILES.map(f => '<div class="tab" data-name="' + f + '">' + f + '</div>').join('');
document.querySelectorAll('.tab').forEach(t => t.onclick = () => select(t.dataset.name));
document.getElementById('save').onclick = async () => {
  try { const r = await api('/api/file', {name: current, content: document.getElementById('editor').value});
    setStatus('已保存' + (r.backup ? '（备份：' + r.backup + '）' : ''), true); await loadBackups();
  } catch(e){ setStatus('保存失败：' + e.message, false); }
};
document.getElementById('reload').onclick = async () => { await loadFile(); setStatus('已重新加载（未保存的修改已丢弃）', true); };
select(current);
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

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            raise ValueError("bad json")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                body = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/api/files":
                self._send_json({"files": [
                    {"name": n, "kind": FILES[n][0], "exists": os.path.isfile(_path(n)), "hint": FILES[n][1]}
                    for n in FILES
                ]})
                return
            if parsed.path == "/api/file":
                name = parse_qs(parsed.query).get("name", [""])[0]
                path = _path(name)
                exists = os.path.isfile(path)
                content = ""
                if exists:
                    with open(path, encoding="utf-8") as f:
                        content = f.read()
                self._send_json({
                    "name": name, "exists": exists, "content": content,
                    "hint": FILES[name][1],
                    "has_example": name == "codex-repos.txt" or os.path.isfile(example_path(name)),
                })
                return
            if parsed.path == "/api/backups":
                name = parse_qs(parsed.query).get("name", [""])[0]
                _path(name)
                self._send_json({"name": name, "backups": list_backups(name)})
                return
            self._send_json({"error": "not found"}, 404)
        except ValueError as e:
            self._send_json({"error": str(e)}, 400)

    def do_POST(self) -> None:
        try:
            data = self._read_json()
            if urlparse(self.path).path == "/api/file":
                name = str(data.get("name") or "")
                content = str(data.get("content") or "")
                self._send_json(save_file(name, content))
                return
            if urlparse(self.path).path == "/api/restore":
                name = str(data.get("name") or "")
                backup = str(data.get("backup") or "")
                self._send_json(restore_file(name, backup))
                return
            if urlparse(self.path).path == "/api/create-from-example":
                name = str(data.get("name") or "")
                self._send_json(create_from_example(name))
                return
            self._send_json({"error": "not found"}, 404)
        except ValueError as e:
            self._send_json({"error": str(e)}, 400)


def create_server(host: str, port: int, config_dir: str) -> ThreadingHTTPServer:
    global CONFIG_DIR
    CONFIG_DIR = config_dir
    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    import sys

    config_dir = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
        "ADMIN_CONFIG_DIR", os.path.dirname(os.path.abspath(__file__))
    )
    host = os.environ.get("ADMIN_HOST", "127.0.0.1")
    port = int(os.environ.get("ADMIN_PORT", "8081"))
    server = create_server(host, port, config_dir)
    log.info("管理配置界面：http://%s:%s（配置目录：%s，仅限本机）",
             host, server.server_address[1], config_dir)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
