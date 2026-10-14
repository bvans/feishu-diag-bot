"""entrypoint.py 单测：URL 判定、目录命名、SSH key、clone/pull、有效配置、dry-run。"""

import os
import stat
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


def test_setup_ssh_key_inline():
    with tempfile.TemporaryDirectory() as home:
        key = "-----BEGIN OPENSSH PRIVATE KEY-----\nFAKE\n-----END OPENSSH PRIVATE KEY-----\n"
        path = entrypoint.setup_ssh_key({"git_ssh_key": key}, home)
        assert path == os.path.join(home, ".ssh", "id_diag")
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"
        with open(path, encoding="utf-8") as f:
            assert "FAKE" in f.read()


def test_setup_ssh_key_file():
    with tempfile.TemporaryDirectory() as home:
        src = os.path.join(home, "id_src")
        with open(src, "w", encoding="utf-8") as f:
            f.write("FAKEKEY")
        path = entrypoint.setup_ssh_key({"git_ssh_key_file": src}, home)
        assert path == os.path.join(home, ".ssh", "id_diag")
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"
        assert entrypoint.setup_ssh_key({}, home) is None
        assert entrypoint.setup_ssh_key({"git_ssh_key_file": "/nope/missing"}, home) is None


def test_build_effective_config_strips_key():
    cfg = {"codex": {"repos": ["git@github.com:o/r.git"]}, "git_ssh_key": "SECRET"}
    eff = entrypoint.build_effective_config(cfg, ["/app/repos/r"])
    assert eff["codex"]["repos"] == ["/app/repos/r"]
    assert "git_ssh_key" not in eff
    assert cfg["git_ssh_key"] == "SECRET"  # 原配置不被改动


def _git(args, cwd):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git"] + args, cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=60)


def _make_local_repo(base):
    """建一个可 clone 的本地仓库，返回 file:// URL。"""
    src = os.path.join(base, "src-repo")
    os.makedirs(src)
    assert _git(["init", "-q"], src).returncode == 0
    with open(os.path.join(src, "hello.txt"), "w", encoding="utf-8") as f:
        f.write("hi")
    assert _git(["add", "."], src).returncode == 0
    assert _git(["commit", "-q", "-m", "init"], src).returncode == 0
    return f"file://{src}"


def test_ensure_repos_clones_file_url_and_pulls():
    import shutil

    if shutil.which("git") is None:
        print("SKIP test_ensure_repos_clones_file_url_and_pulls (no git)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        url = _make_local_repo(tmp)
        dest = os.path.join(tmp, "repos")
        paths = entrypoint.ensure_repos({"codex": {"repos": [url]}}, dest, {})
        assert len(paths) == 1
        assert os.path.isfile(os.path.join(paths[0], "hello.txt"))
        # 第二次：已存在 -> pull，仍返回同一路径
        assert entrypoint.ensure_repos({"codex": {"repos": [url]}}, dest, {}) == paths


def test_ensure_repos_local_path_passthrough():
    with tempfile.TemporaryDirectory() as tmp:
        assert entrypoint.ensure_repos({"codex": {"repos": [tmp, "/nope/missing"]}}, tmp, {}) == [tmp]


def test_entrypoint_dry_run_end_to_end():
    import shutil

    if shutil.which("git") is None:
        print("SKIP test_entrypoint_dry_run_end_to_end (no git)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        url = _make_local_repo(tmp)
        home = os.path.join(tmp, "home")
        os.makedirs(home)
        cfg = {
            "codex": {
                "enabled": True,
                "repos": [url],
                "repos_dir": os.path.join(tmp, "repos"),
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
        assert len(eff["codex"]["repos"]) == 1
        assert os.path.isfile(os.path.join(eff["codex"]["repos"][0], "hello.txt"))
        assert eff["prompt"]["template"].startswith("问题：")


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL PASS")
