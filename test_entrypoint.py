"""entrypoint.py 单测：URL 判定、目录命名、已烘焙仓库映射、有效配置、dry-run。"""

import os
import subprocess
import sys
import tempfile

import yaml

sys.path.insert(0, __file__.rsplit("/", 1)[0])

import entrypoint


def test_is_url():
    assert entrypoint.is_url("git@github.com:org/repo.git")
    assert entrypoint.is_url("https://github.com/org/repo.git")
    assert entrypoint.is_url("ssh://git@github.com/org/repo.git")
    assert entrypoint.is_url("file:///tmp/repo.git")
    assert not entrypoint.is_url("/opt/myapp")
    assert not entrypoint.is_url("./rel/path")
    assert not entrypoint.is_url("")


def test_derive_repo_dir():
    used: set[str] = set()
    assert entrypoint.derive_repo_dir("git@github.com:org/myapp.git", used) == "myapp"
    assert entrypoint.derive_repo_dir("https://github.com/org/myapp.git", used) == "org-myapp"
    assert entrypoint.derive_repo_dir("https://github.com/other/lib.git", used) == "lib"


def test_build_effective_config_strips_key():
    cfg = {"codex": {"repos": ["git@github.com:o/r.git"]}, "git_ssh_key": "SECRET"}
    eff = entrypoint.build_effective_config(cfg, ["/app/repos/r"])
    assert eff["codex"]["repos"] == ["/app/repos/r"]
    assert "git_ssh_key" not in eff
    assert cfg["git_ssh_key"] == "SECRET"  # 原配置不被改动


def test_resolve_baked_repos_maps_url_to_baked_dir():
    with tempfile.TemporaryDirectory() as tmp:
        dest = os.path.join(tmp, "repos")
        os.makedirs(os.path.join(dest, "myapp", ".git"))
        os.makedirs(os.path.join(dest, "lib", ".git"))
        cfg = {"codex": {"repos": [
            "git@github.com:org/myapp.git",       # -> dest/myapp（已烘焙）
            "https://github.com/other/lib.git",   # -> dest/lib（已烘焙）
            "https://github.com/nope/missing.git",  # 未烘焙 -> 跳过
            tmp,                                 # 本地路径原样保留
            "/nope/missing-dir",                 # 本地路径不存在 -> 跳过
        ]}}
        assert entrypoint.resolve_baked_repos(cfg, dest) == [
            os.path.join(dest, "myapp"),
            os.path.join(dest, "lib"),
            tmp,
        ]


def test_resolve_baked_repos_uses_manifest():
    import json

    with tempfile.TemporaryDirectory() as tmp:
        dest = os.path.join(tmp, "repos")
        # manifest 把 URL 映射到自定义目录名，优先于命名规则推导
        os.makedirs(os.path.join(dest, "custom-name", ".git"))
        with open(os.path.join(dest, ".baked-manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"git@github.com:org/myapp.git": "custom-name"}, f)
        cfg = {"codex": {"repos": ["git@github.com:org/myapp.git"]}}
        assert entrypoint.resolve_baked_repos(cfg, dest) == [os.path.join(dest, "custom-name")]


def test_entrypoint_dry_run_end_to_end():
    with tempfile.TemporaryDirectory() as tmp:
        dest = os.path.join(tmp, "repos")
        baked = os.path.join(dest, "myapp")
        os.makedirs(os.path.join(baked, ".git"))
        with open(os.path.join(baked, "hello.txt"), "w", encoding="utf-8") as f:
            f.write("hi")
        home = os.path.join(tmp, "home")
        os.makedirs(home)
        cfg = {
            "codex": {
                "enabled": True,
                "repos": ["git@github.com:org/myapp.git"],
                "repos_dir": dest,
                "mcp_servers": [{"name": "logsearch", "command": "npx", "args": ["-y", "x"]}],
            },
            "git_ssh_key": "SHOULD-BE-STRIPPED",
            "prompt": {"template": "问题：{issue}\n仓库：\n{repos}"},
        }
        cfg_path = os.path.join(tmp, "config.yaml")
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)
        eff_path = os.path.join(tmp, "effective.yaml")
        env = dict(os.environ, HOME=home, DIAG_ENTRYPOINT_DRY_RUN="1",
                   DIAG_EFFECTIVE_CONFIG=eff_path)
        proc = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "entrypoint.py"), cfg_path],
            capture_output=True, text=True, timeout=120, env=env,
        )
        assert proc.returncode == 0, proc.stderr
        with open(eff_path, encoding="utf-8") as f:
            eff = yaml.safe_load(f)
        assert "git_ssh_key" not in eff
        assert eff["codex"]["repos"] == [baked]
        assert os.path.isfile(os.path.join(eff["codex"]["repos"][0], "hello.txt"))
        assert eff["prompt"]["template"].startswith("问题：")


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL PASS")
