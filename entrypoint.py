"""Docker 入口：SSH key -> clone/pull 仓库 -> 生成有效配置 -> 启动机器人。

用法（镜像内默认）：entrypoint.py [config.yaml]
- config.yaml 是用户挂载的配置文件（见 config.example.yaml）：里面可以是
  git 仓库地址（SSH/HTTPS URL）、本地路径、MCP 配置、SSH 密钥、提示词模板。
- 流程：配好 SSH key -> 把 URL 仓库 clone/pull 到 repos_dir（默认 /app/repos）->
  生成去掉内嵌密钥的有效配置 -> 以 DIAG_BOT_CONFIG 指向它启动 bot.py。
- 本地路径的 repos 条目不 clone，原样保留；clone/pull 失败的仓库记警告并跳过，
  不让单个仓库拖垮机器人启动。
- DIAG_ENTRYPOINT_DRY_RUN=1 时只做到生成有效配置就退出（便于本地验证），不启动 bot。
"""

import copy
import logging
import os
import shutil
import subprocess
import sys

import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("feishu-diag-bot-entrypoint")

DEFAULT_CONFIG = "/app/config.yaml"
DEFAULT_REPOS_DIR = "/app/repos"
DEFAULT_EFFECTIVE_CONFIG = "/tmp/effective-config.yaml"


def is_url(entry: str) -> bool:
    """判断 repos 条目是待 clone 的 URL 还是本地路径。"""
    entry = (entry or "").strip()
    if not entry:
        return False
    if entry.startswith(("/", ".", "~")):
        return False
    if "://" in entry:
        return True
    if entry.endswith(".git"):
        return True
    return "@" in entry and ":" in entry  # 如 git@github.com:org/repo.git


def derive_repo_dir(url: str, used: set[str]) -> str:
    """从仓库 URL 取目录名（去掉 .git）；重名时加 org 前缀，如 org-repo。"""
    base = url.rstrip("/").rsplit("/", 1)[-1]
    if ":" in base and "/" not in base:  # SCP 语法 git@host:org/repo.git 的尾巴
        base = base.rsplit(":", 1)[-1]
    if base.endswith(".git"):
        base = base[: -len(".git")]
    name = base or "repo"
    if name in used:
        org = ""
        rest = url.rstrip("/")
        if "://" in rest:  # https://host/org/repo（先判 URL，避免 https: 里的冒号被当成 SCP 语法）
            parts = rest.split("://", 1)[-1].split("/")
            org = parts[1] if len(parts) > 1 else ""
        elif ":" in rest:  # git@host:org/repo
            org = rest.rsplit(":", 1)[-1].rsplit("/", 1)[0]
        if org:
            name = f"{org}-{name}"
    used.add(name)
    return name


def setup_ssh_key(cfg: dict, home: str) -> str | None:
    """配好 SSH key，返回 key 文件路径；没配 key 返回 None（走已有 ~/.ssh 或 HTTPS）。"""
    ssh_dir = os.path.join(home, ".ssh")
    os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
    key = cfg.get("git_ssh_key") or ""
    key_file = cfg.get("git_ssh_key_file") or ""
    dest = os.path.join(ssh_dir, "id_diag")
    if key:
        log.warning("git_ssh_key 内嵌在配置里：勿提交到 Git，建议改用 git_ssh_key_file 挂载密钥文件")
        with open(dest, "w", encoding="utf-8") as f:
            f.write(key if key.endswith("\n") else key + "\n")
        os.chmod(dest, 0o600)
        return dest
    if key_file:
        src = os.path.expanduser(str(key_file))
        if not os.path.isfile(src):
            log.warning("git_ssh_key_file 不存在：%s，SSH 仓库可能 clone 失败", src)
            return None
        shutil.copyfile(src, dest)
        os.chmod(dest, 0o600)
        return dest
    return None


def git_env(key_path: str | None) -> dict:
    """key 存在时让 git 用它，并首次连接自动接受 host key（accept-new，不盲目信任所有）。"""
    if not key_path:
        return {}
    return {
        "GIT_SSH_COMMAND": (
            f"ssh -i {key_path} -o StrictHostKeyChecking=accept-new "
            "-o IdentitiesOnly=yes -o BatchMode=yes"
        )
    }


def _run_git(args: list[str], env: dict, cwd: str | None = None) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    full_env.update(env)
    return subprocess.run(
        ["git"] + args, capture_output=True, text=True, timeout=300, env=full_env, cwd=cwd
    )


def ensure_repos(cfg: dict, dest_dir: str, env: dict) -> list[str]:
    """把 codex.repos 里的 URL clone/pull 到 dest_dir；本地路径原样保留。返回本地路径列表。"""
    codex_cfg = cfg.get("codex") or {}
    entries = [str(x) for x in (codex_cfg.get("repos") or []) if str(x).strip()]
    if not entries:
        return []
    os.makedirs(dest_dir, exist_ok=True)
    used: set[str] = set()
    local_paths: list[str] = []
    for entry in entries:
        if not is_url(entry):
            path = os.path.expanduser(entry)
            if os.path.isdir(path):
                local_paths.append(path)
            else:
                log.warning("本地仓库不存在，已跳过：%s", path)
            continue
        name = derive_repo_dir(entry, used)
        target = os.path.join(dest_dir, name)
        if os.path.isdir(os.path.join(target, ".git")):
            proc = _run_git(["-C", target, "pull", "--ff-only"], env)
            if proc.returncode == 0:
                log.info("仓库已更新：%s -> %s", entry, target)
                local_paths.append(target)
            else:
                log.warning("仓库 pull 失败，沿用旧 checkout：%s（%s）", target, proc.stderr.strip()[-300:])
                local_paths.append(target)
            continue
        if os.path.exists(target):
            log.warning("目标已存在且不是 git 仓库，已跳过：%s", target)
            continue
        proc = _run_git(["clone", entry, target], env)
        if proc.returncode == 0:
            log.info("仓库已 clone：%s -> %s", entry, target)
            local_paths.append(target)
        else:
            log.warning("仓库 clone 失败，已跳过：%s（%s）", entry, proc.stderr.strip()[-300:])
    return local_paths


def build_effective_config(cfg: dict, local_repos: list[str]) -> dict:
    """有效配置：codex.repos 换成本地路径；删掉内嵌的 git_ssh_key（不留明文密钥）。"""
    effective = copy.deepcopy(cfg)
    effective.setdefault("codex", {})["repos"] = local_repos
    effective.pop("git_ssh_key", None)
    return effective


def main() -> int:
    config_path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DIAG_BOT_CONFIG", DEFAULT_CONFIG)
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    home = os.environ.get("HOME") or os.path.expanduser("~")
    key_path = setup_ssh_key(cfg, home)
    env = git_env(key_path)

    codex_cfg = cfg.get("codex") or {}
    dest_dir = os.path.expanduser(str(codex_cfg.get("repos_dir") or DEFAULT_REPOS_DIR))
    local_repos = ensure_repos(cfg, dest_dir, env)
    effective = build_effective_config(cfg, local_repos)

    effective_path = os.environ.get("DIAG_EFFECTIVE_CONFIG", DEFAULT_EFFECTIVE_CONFIG)
    with open(effective_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(effective, f, allow_unicode=True, sort_keys=False)
    log.info("有效配置已生成：%s（仓库 %d 个）", effective_path, len(local_repos))

    if os.environ.get("DIAG_ENTRYPOINT_DRY_RUN") == "1":
        print(f"DRY RUN: effective config at {effective_path}")
        return 0

    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)
    os.environ["DIAG_BOT_CONFIG"] = effective_path
    os.execvpe("uv", ["uv", "run", "bot.py"], os.environ)
    return 0  # exec 成功时不会到这里


if __name__ == "__main__":
    raise SystemExit(main())
