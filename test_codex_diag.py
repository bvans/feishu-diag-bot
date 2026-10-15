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
    write_agents_md,
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


def test_build_codex_toml_model_providers():
    toml = build_codex_toml(
        {
            "model": "kimi-k2",
            "model_provider": "cliproxyapi",
            "model_providers": {
                "cliproxyapi": {
                    "name": "cliproxyapi",
                    "base_url": "http://cliproxyapi:8317/v1",
                    "env_key": "CLIPROXYAPI_API_KEY",
                    "wire_api": "responses",
                    "requires_openai_auth": True,
                    "model_catalog_url": "http://cliproxyapi:8317/v1/models",
                    "http_headers": {"X-Custom": "1"},
                },
                "bad": "not-a-dict",  # 非 dict 的 provider 应被跳过
                "": {"base_url": "http://x"},  # 空 id 应被跳过
            },
        }
    )
    assert 'model_provider = "cliproxyapi"' in toml
    assert '[model_providers."cliproxyapi"]' in toml
    assert 'base_url = "http://cliproxyapi:8317/v1"' in toml
    assert 'env_key = "CLIPROXYAPI_API_KEY"' in toml
    assert 'wire_api = "responses"' in toml
    assert "requires_openai_auth = true" in toml
    assert 'model_catalog_url = "http://cliproxyapi:8317/v1/models"' in toml
    assert '"X-Custom" = "1"' in toml
    assert "not-a-dict" not in toml
    assert "http://x" not in toml


def test_build_codex_toml_sandbox_mode():
    # 显式配置
    assert 'sandbox_mode = "danger-full-access"' in build_codex_toml(
        {"sandbox_mode": "danger-full-access"})
    # 默认 read-only
    assert 'sandbox_mode = "read-only"' in build_codex_toml({})
    # 非法值回落 read-only
    assert 'sandbox_mode = "read-only"' in build_codex_toml({"sandbox_mode": "bogus"})


def test_build_codex_toml_mcp_tool_allowlist():
    toml = build_codex_toml(
        {
            "mcp_servers": [
                {
                    "name": "logsearch",
                    "command": "npx",
                    "enabled_tools": ["search_logs"],
                    "disabled_tools": ["delete_log"],
                },
                {"name": "plain", "command": "npx"},  # 不带工具范围：两行都不出现
            ],
        }
    )
    assert 'enabled_tools = ["search_logs"]' in toml
    assert 'disabled_tools = ["delete_log"]' in toml
    assert toml.count("enabled_tools") == 1
    assert toml.count("disabled_tools") == 1
    # 非列表值应被忽略，不写脏数据
    toml2 = build_codex_toml(
        {"mcp_servers": [{"name": "x", "command": "npx", "enabled_tools": "search_logs"}]}
    )
    assert "enabled_tools" not in toml2


def test_build_codex_toml_web_search_and_tools():
    # 默认 web_search = "disabled"
    assert 'web_search = "disabled"' in build_codex_toml({})
    # 显式配置
    assert 'web_search = "live"' in build_codex_toml({"web_search": "live"})
    # 非法值回落 disabled
    assert 'web_search = "disabled"' in build_codex_toml({"web_search": "bogus"})
    # [tools]：只写布尔项；不配时不写
    toml = build_codex_toml({"tools": {"view_image": False, "web_search": True}})
    assert "[tools]" in toml
    assert "view_image = false" in toml
    assert "web_search = true" in toml
    assert "[tools]" not in build_codex_toml({})
    assert "[tools]" not in build_codex_toml({"tools": {"view_image": "yes"}})


def test_build_codex_toml_empty_model_omitted():
    toml = build_codex_toml({"model": "", "mcp_servers": []})
    assert "model =" not in toml
    assert "mcp_servers" not in toml


def test_write_agents_md_from_file():
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "AGENTS.md")
        with open(src, "w", encoding="utf-8") as f:
            f.write("# 全局指令\n")
        home = os.path.join(tmp, "codex-home")
        os.makedirs(home)
        write_agents_md({"agents_md_file": src}, home)
        with open(os.path.join(home, "AGENTS.md"), encoding="utf-8") as f:
            assert f.read() == "# 全局指令\n"


def test_write_agents_md_inline_and_missing_file():
    with tempfile.TemporaryDirectory() as tmp:
        home = os.path.join(tmp, "codex-home")
        os.makedirs(home)
        # 内联文本
        write_agents_md({"agents_md": "只查、不改"}, home)
        with open(os.path.join(home, "AGENTS.md"), encoding="utf-8") as f:
            assert f.read() == "只查、不改\n"
        # 文件优先于内联
        src = os.path.join(tmp, "AGENTS.md")
        with open(src, "w", encoding="utf-8") as f:
            f.write("文件版\n")
        write_agents_md({"agents_md_file": src, "agents_md": "内联版"}, home)
        with open(os.path.join(home, "AGENTS.md"), encoding="utf-8") as f:
            assert f.read() == "文件版\n"
        # 文件不存在：警告并跳过，不写文件、不抛异常
        home2 = os.path.join(tmp, "codex-home2")
        os.makedirs(home2)
        write_agents_md({"agents_md_file": "/nope/not-here.md"}, home2)
        assert not os.path.exists(os.path.join(home2, "AGENTS.md"))


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
        old_key = os.environ.get("OPENAI_API_KEY")
        os.environ["PATH"] = tmp + os.pathsep + old_path
        os.environ["OPENAI_API_KEY"] = "dummy-for-test"  # 认证预检需要 key（假二进制不真调 API）
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
            if old_key is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = old_key


def test_diagnose_with_codex_no_repos_uses_cwd():
    with tempfile.TemporaryDirectory() as tmp:
        _write_fake_codex(tmp)
        old_path = os.environ.get("PATH", "")
        old_key = os.environ.get("OPENAI_API_KEY")
        old_cwd = os.getcwd()
        os.environ["PATH"] = tmp + os.pathsep + old_path
        os.environ["OPENAI_API_KEY"] = "dummy-for-test"
        os.chdir(tmp)  # repos 为空时应以当前工作目录做 Codex 工作区
        try:
            cfg = _cfg(tmp)
            cfg["codex"]["repos"] = []
            cfg["repo"] = {}
            result = diagnose_with_codex("下单 500", cfg)
            assert result == "FAKE 诊断结论：根因是假的", result
            with open(os.path.join(tmp, "args.txt"), encoding="utf-8") as f:
                args = f.read()
            assert f"-C {tmp}" in args
            assert "--skip-git-repo-check" in args
        finally:
            os.chdir(old_cwd)
            os.environ["PATH"] = old_path
            if old_key is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = old_key


def test_diagnose_with_codex_model_provider_env_key_auth():
    # model_providers 的 env_key 有值时，认证预检应通过（不需要 OPENAI_API_KEY）
    with tempfile.TemporaryDirectory() as tmp:
        _write_fake_codex(tmp)
        old_path = os.environ.get("PATH", "")
        old_home = os.environ.get("HOME", "")
        old_key = os.environ.get("OPENAI_API_KEY")
        old_codex_home = os.environ.get("CODEX_HOME")
        old_proxy_key = os.environ.get("CLIPROXYAPI_API_KEY")
        os.environ["PATH"] = tmp + os.pathsep + old_path
        os.environ["HOME"] = tmp  # 空 HOME，没有 .codex/auth.json
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ.pop("CODEX_HOME", None)
        os.environ["CLIPROXYAPI_API_KEY"] = "proxy-dummy"
        try:
            cfg = _cfg(tmp)
            cfg["codex"]["model_provider"] = "cliproxyapi"
            cfg["codex"]["model_providers"] = {
                "cliproxyapi": {
                    "base_url": "http://cliproxyapi:8317/v1",
                    "env_key": "CLIPROXYAPI_API_KEY",
                    "wire_api": "responses",
                }
            }
            result = diagnose_with_codex("下单 500", cfg)
            assert result == "FAKE 诊断结论：根因是假的", result
        finally:
            os.environ["PATH"] = old_path
            os.environ["HOME"] = old_home
            if old_key is not None:
                os.environ["OPENAI_API_KEY"] = old_key
            if old_codex_home is not None:
                os.environ["CODEX_HOME"] = old_codex_home
            if old_proxy_key is None:
                os.environ.pop("CLIPROXYAPI_API_KEY", None)
            else:
                os.environ["CLIPROXYAPI_API_KEY"] = old_proxy_key


def test_diagnose_with_codex_no_auth_fails_fast():
    with tempfile.TemporaryDirectory() as tmp:
        _write_fake_codex(tmp)  # 二进制存在，但无认证 -> 应在启动前快速失败，不等 timeout
        old_path = os.environ.get("PATH", "")
        old_home = os.environ.get("HOME", "")
        old_key = os.environ.get("OPENAI_API_KEY")
        old_codex_home = os.environ.get("CODEX_HOME")
        os.environ["PATH"] = tmp + os.pathsep + old_path
        os.environ["HOME"] = tmp  # 空 HOME，没有 .codex/auth.json
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ.pop("CODEX_HOME", None)
        try:
            cfg = _cfg(tmp)
            try:
                diagnose_with_codex("下单 500", cfg)
            except CodexError as e:
                assert "未认证" in str(e)
            else:
                raise AssertionError("无认证时应抛 CodexError")
        finally:
            os.environ["PATH"] = old_path
            os.environ["HOME"] = old_home
            if old_key is not None:
                os.environ["OPENAI_API_KEY"] = old_key
            if old_codex_home is not None:
                os.environ["CODEX_HOME"] = old_codex_home


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


def test_build_prompt_uses_template():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        cfg["prompt"] = {"template": "问题：{issue}\n时间：{time}\n版本：{version}\n仓库：\n{repos}\n日志：\n{logs}"}
        prompt = build_prompt("下单 500，15:04，版本 v1.2.3，request_id=abc123", cfg)
        assert prompt.startswith("问题：下单 500")
        assert "15:04" in prompt and "v1.2.3" in prompt
        assert tmp in prompt and "/var/log/myapp/api.log" in prompt
        assert "只查、不改" not in prompt  # 模板接管，不含内置要求


def test_build_prompt_empty_template_falls_back():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        cfg["prompt"] = {"template": "   "}
        assert "只查、不改" in build_prompt("下单 500，15:04，版本 v1.2.3", cfg)


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL PASS")
