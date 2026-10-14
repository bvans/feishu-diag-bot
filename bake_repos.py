"""Build 时烘焙诊断仓库进镜像：读 codex-repos.txt（每行一个 git URL），clone 到 repos_dir。

用法（Dockerfile 里）：
    python3 bake_repos.py --repos-file codex-repos.txt --repos-dir /app/repos
私有仓库用 BuildKit secret 传 SSH key（挂载在 /run/secrets/git_ssh_key）：
    docker build --secret id=git_ssh_key,src=./id_diag .
公开仓库不需要 secret。

- 目录命名与 entrypoint 保持一致（entrypoint.derive_repo_dir），并写
  <repos_dir>/.baked-manifest.json（URL -> 目录名），启动时按 manifest 映射，
  不受两个文件里仓库顺序不同的影响。
- clone 失败即退出非零：build 失败，不生成缺仓库的镜像。
- 默认浅 clone（--depth 1，工作区够诊断用）；要完整历史用 --depth 0。
"""

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import entrypoint

SECRET_KEY_PATH = "/run/secrets/git_ssh_key"
MANIFEST_NAME = ".baked-manifest.json"


def read_repo_list(path: str) -> list[str]:
    if not os.path.isfile(path):
        raise SystemExit(f"仓库清单不存在：{path}")
    urls = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return urls


def bake(urls: list[str], dest_dir: str, depth: int) -> dict[str, str]:
    """clone 全部 URL，返回 {url: 目录名} 的 manifest。"""
    os.makedirs(dest_dir, exist_ok=True)
    env = dict(os.environ)
    if os.path.isfile(SECRET_KEY_PATH):
        env.update(entrypoint.git_env(SECRET_KEY_PATH))
    used: set[str] = set()
    manifest: dict[str, str] = {}
    for url in urls:
        name = entrypoint.derive_repo_dir(url, used)
        target = os.path.join(dest_dir, name)
        if os.path.isdir(os.path.join(target, ".git")):
            print(f"已存在，跳过：{url} -> {target}")
            manifest[url] = name
            continue
        args = ["git", "clone"]
        if depth > 0:
            args += ["--depth", str(depth)]
        args += [url, target]
        proc = subprocess.run(args, capture_output=True, text=True, timeout=600, env=env)
        if proc.returncode != 0:
            raise SystemExit(f"仓库 clone 失败：{url}\n{proc.stderr.strip()[-500:]}")
        print(f"已烘焙：{url} -> {target}")
        manifest[url] = name
    with open(os.path.join(dest_dir, MANIFEST_NAME), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description="build 时烘焙诊断仓库进镜像")
    ap.add_argument("--repos-file", default="codex-repos.txt")
    ap.add_argument("--repos-dir", default="/app/repos")
    ap.add_argument("--depth", type=int, default=1, help="浅 clone 深度，0 为完整 clone")
    args = ap.parse_args()
    urls = read_repo_list(args.repos_file)
    if not urls:
        print("仓库清单为空，不烘焙任何仓库")
        return 0
    bake(urls, args.repos_dir, args.depth)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
