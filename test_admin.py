"""admin.py 单测：文件列表/读写、校验拦截、备份恢复、从示例创建、防路径穿越。"""

import glob
import json
import os
import sys
import tempfile
import threading
import urllib.request

sys.path.insert(0, __file__.rsplit("/", 1)[0])

import admin


def _req(method, url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def test_admin_flow():
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "config.yaml"), "w", encoding="utf-8") as f:
            f.write("llm:\n  model: old\n")
        with open(os.path.join(tmp, "config.example.yaml"), "w", encoding="utf-8") as f:
            f.write("llm:\n  model: example\n")
        with open(os.path.join(tmp, "cliproxyapi-config.example.yaml"), "w", encoding="utf-8") as f:
            f.write("config-version: 8\n")
        with open(os.path.join(tmp, ".env"), "w", encoding="utf-8") as f:
            f.write("FOO=bar\n")
        server = admin.create_server("127.0.0.1", 0, tmp)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}"
        try:
            # 页面
            status, body = _req("GET", base + "/")
            assert status == 200 and "管理配置" in body

            # 文件列表
            status, body = _req("GET", base + "/api/files")
            files = {f["name"]: f for f in json.loads(body)["files"]}
            assert files["config.yaml"]["exists"] is True
            assert files["cliproxyapi-config.yaml"]["exists"] is False
            assert files["codex-repos.txt"]["exists"] is False

            # 读文件
            status, body = _req("GET", base + "/api/file?name=config.yaml")
            assert json.loads(body)["content"] == "llm:\n  model: old\n"

            # 保存非法 YAML -> 400，原文件不动
            status, body = _req("POST", base + "/api/file",
                                 {"name": "config.yaml", "content": "llm: [unclosed"})
            assert status == 400 and "YAML" in body
            with open(os.path.join(tmp, "config.yaml"), encoding="utf-8") as f:
                assert f.read() == "llm:\n  model: old\n"

            # 保存非法 .env -> 400
            status, body = _req("POST", base + "/api/file",
                                 {"name": ".env", "content": "NOT-A-KEY-VALUE\n"})
            assert status == 400

            # 保存合法内容 -> 成功 + 产生备份
            status, body = _req("POST", base + "/api/file",
                                 {"name": "config.yaml", "content": "llm:\n  model: new\n"})
            assert status == 200 and json.loads(body)["ok"] is True
            with open(os.path.join(tmp, "config.yaml"), encoding="utf-8") as f:
                assert f.read() == "llm:\n  model: new\n"
            assert len(glob.glob(os.path.join(tmp, "config.yaml.bak.*"))) == 1

            # 从备份恢复
            status, body = _req("GET", base + "/api/backups?name=config.yaml")
            backup = json.loads(body)["backups"][0]
            status, body = _req("POST", base + "/api/restore",
                                 {"name": "config.yaml", "backup": backup})
            assert status == 200
            with open(os.path.join(tmp, "config.yaml"), encoding="utf-8") as f:
                assert f.read() == "llm:\n  model: old\n"

            # 不存在的文件从示例创建
            status, body = _req("POST", base + "/api/create-from-example",
                                 {"name": "cliproxyapi-config.yaml"})
            assert status == 200
            assert os.path.isfile(os.path.join(tmp, "cliproxyapi-config.yaml"))

            # codex-repos.txt 建空文件
            status, body = _req("POST", base + "/api/create-from-example",
                                 {"name": "codex-repos.txt"})
            assert status == 200
            with open(os.path.join(tmp, "codex-repos.txt"), encoding="utf-8") as f:
                assert f.read() == ""

            # 路径穿越 -> 400
            status, body = _req("GET", base + "/api/file?name=../etc/passwd")
            assert status == 400
            status, body = _req("POST", base + "/api/file",
                                 {"name": "../../tmp/evil", "content": "x"})
            assert status == 400
            assert not os.path.exists("/tmp/evil")
        finally:
            server.shutdown()


if __name__ == "__main__":
    test_admin_flow()
    print("PASS test_admin_flow")
    print("ALL PASS")
