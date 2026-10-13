"""codex_diag.py 单测：TOML 生成、提示词、仓库解析、假 codex 二进制调用、兜底。"""

import os
import stat
import sys
import tempfile

sys.path.insert(0, __file__.rsplit("/", 1)[0])

from codex_diag import (
    CodexError,
    build_codex_home,
    build_codex_toml,
    build_prompt,
    diagnose_with_codex,
    resolve_repos,
)


def _cfg(tmp, **over):
    cfg = {
        "codex": {"enabled": True, "repos": [tmp], "timeout_seconds": 30},
        "logs": [{"name": "api", "path": "/var/log/myapp/api.log"}],
        "repo": {"path": tmp},
        "llm": {"base_url": "http://127.0.0.1:9", "api_key_env": "NOPE", "model": "x"},
    }
    cfg["codex"].update(over)
    return cfg


def test_build_codex_toml():
    toml = build_codex_toml(
        {
            "model": "gpt-5.3-codex",
            "reasoning_effort": "medium",
            "mcp_servers": [
                {
                    "name": "logsearch",
                    "command": "npx",
                    "args": ["-y", "example-log-mcp"],
                    "env": {"TOKEN": "abc"},
                    "startup_timeout_sec": 20,
                },
                {"name": "", "command": "npx"},  # 无名 MCP 应被跳过
                {"name": "no-cmd", "command": ""},  # 无命令 MCP 应被跳过
            ],
        }
    )
    assert 'approval_policy = "never"' in toml
    assert 'sandbox_mode = "read-only"' in toml
    assert 'model = "gpt-5.3-codex"' in toml
    assert 'model_reasoning_effort = "medium"' in toml
    assert '[mcp_servers."logsearch"]' in toml
    assert 'command = "npx"' in toml
    assert 'args = ["-y", "example-log-mcp"]' in toml
    assert '"TOKEN" = "abc"' in toml
    assert "startup_timeout_sec = 20" in toml
    assert "no-cmd" not in toml


def test_build_codex_toml_empty_model_omitted():
    toml = build_codex_toml({"model": "", "mcp_servers": []})
    assert "model =" not in toml
    assert "mcp_servers" not in toml


def test_build_codex_home_writes_config():
    with tempfile.TemporaryDirectory() as tmp:
        out = build_codex_home({"model": "m"}, tmp)
        assert out == tmp
        with open(os.path.join(tmp, "config.toml"), encoding="utf-8") as f:
            assert 'model = "m"' in f.read()


def test_build_prompt_contains_slots_repos_logs():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        cfg["codex"]["repos"] = [tmp, tmp]  # 两个仓库：主 + 附加
        prompt = build_prompt(
            "下单接口返回 500，点支付没反应，15:04 左右，版本 v1.2.3，request_id=abc123",
            cfg,
        )
        assert "下单接口返回 500" in prompt
        assert "15:04" in prompt and "v1.2.3" in prompt and "abc123" in prompt
        assert tmp in prompt and "/var/log/myapp/api.log" in prompt
        assert "只查、不改" in prompt


def test_resolve_repos_skips_missing_and_falls_back():
    with tempfile.TemporaryDirectory() as tmp:
        assert resolve_repos({"repos": ["/nope/not-here", tmp]}, {}) == [tmp]
        # codex.repos 为空时回落旧 repo.path
        assert resolve_repos({"repos": []}, {"repo": {"path": tmp}}) == [tmp]
        assert resolve_repos({"repos": []}, {}) == []


def _write_fake_codex(tmp):
    """假 codex 二进制：解析 --output-last-message 写结论，并把全部参数记到 args.txt。"""
    fake = os.path.join(tmp, "codex")
    with open(fake, "w", encoding="utf-8") as f:
        f.write(
            "#!/usr/bin/env python3\n"
            "import os, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "base = os.path.dirname(os.path.abspath(__file__))\n"
            "pathlib.Path(os.path.join(base, 'args.txt')).write_text(' '.join(args), encoding='utf-8')\n"
            "out = None\n"
            "for i, a in enumerate(args):\n"
            "    if a == '--output-last-message':\n"
            "        out = args[i + 1]\n"
            "if out:\n"
            "    pathlib.Path(out).write_text('FAKE 诊断结论：根因是假的', encoding='utf-8')\n"
            "sys.exit(0)\n"
        )
    os.chmod(fake, stat.S_IRWXU)
    return fake


def test_diagnose_with_codex_fake_binary():
    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "repo1")
        repo2 = os.path.join(tmp, "repo2")
        os.makedirs(repo)
        os.makedirs(repo2)
        _write_fake_codex(tmp)
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = tmp + os.pathsep + old_path
        try:
            cfg = _cfg(repo)
            cfg["codex"]["repos"] = [repo, repo2]
            result = diagnose_with_codex("下单 500，15:04，版本 v1.2.3", cfg)
            assert result == "FAKE 诊断结论：根因是假的", result
            with open(os.path.join(tmp, "args.txt"), encoding="utf-8") as f:
                args = f.read()
            assert f"-C {repo}" in args and f"--add-dir {repo2}" in args
            assert "--output-last-message" in args
        finally:
            os.environ["PATH"] = old_path


def test_diagnose_with_codex_missing_binary():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        cfg["codex"]["binary"] = "codex-definitely-not-here"
        try:
            diagnose_with_codex("下单 500", cfg)
        except CodexError as e:
            assert "未找到" in str(e)
        else:
            raise AssertionError("应抛 CodexError")


def test_diagnose_falls_back_when_codex_missing():
    from diagnose import diagnose

    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        cfg["codex"]["binary"] = "codex-definitely-not-here"
        # Codex 缺失 -> 兜底快速通道；LLM 指向不可达地址 -> 降级结论，不抛异常
        result = diagnose("下单接口 500，15:04，版本 v1.2.3", cfg)
        assert isinstance(result, str) and "根因" in result, result


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL PASS")
