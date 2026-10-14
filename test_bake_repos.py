"""bake_repos.py 单测：build 时 clone 清单里的仓库、写 manifest。"""

import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, __file__.rsplit("/", 1)[0])

import bake_repos


def _git(args, cwd):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git"] + args, cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=60)


def _make_local_repo(base):
    src = os.path.join(base, "src-repo")
    os.makedirs(src)
    assert _git(["init", "-q"], src).returncode == 0
    with open(os.path.join(src, "hello.txt"), "w", encoding="utf-8") as f:
        f.write("hi")
    assert _git(["add", "."], src).returncode == 0
    assert _git(["commit", "-q", "-m", "init"], src).returncode == 0
    return f"file://{src}"


def test_bake_clones_and_writes_manifest():
    import shutil

    if shutil.which("git") is None:
        print("SKIP test_bake_clones_and_writes_manifest (no git)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        url = _make_local_repo(tmp)
        repos_file = os.path.join(tmp, "codex-repos.txt")
        with open(repos_file, "w", encoding="utf-8") as f:
            f.write("# 注释行应被跳过\n\n" + url + "\n")
        dest = os.path.join(tmp, "repos")
        urls = bake_repos.read_repo_list(repos_file)
        assert urls == [url]
        manifest = bake_repos.bake(urls, dest, depth=1)
        target = os.path.join(dest, manifest[url])
        assert os.path.isfile(os.path.join(target, "hello.txt"))
        with open(os.path.join(dest, ".baked-manifest.json"), encoding="utf-8") as f:
            assert json.load(f) == manifest


def test_bake_empty_list_noop():
    with tempfile.TemporaryDirectory() as tmp:
        dest = os.path.join(tmp, "repos")
        assert bake_repos.bake([], dest, depth=1) == {}


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL PASS")
