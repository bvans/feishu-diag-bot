"""Docker 入口：生成有效配置 -> 启动机器人。

用法（镜像内默认）：entrypoint.py [config.yaml]
- config.yaml 是用户挂载的配置文件（见 config.example.yaml）：里面可以是
  git 仓库地址（SSH/HTTPS URL）、本地路径、MCP 配置、提示词模板。
- 诊断仓库在 build 时已烘焙进镜像（bake_repos.py + codex-repos.txt 清单），
  启动时不再联网 clone/pull，内网可用。入口只把 config 里的 URL 映射到
  已烘焙的目录（按 <repos_dir>/.baked-manifest.json，缺了就按命名规则推导）。
- 本地路径的 repos 条目原样保留；URL 对应目录不存在时记警告并跳过，
  不让单个仓库拖垮机器人启动。
- DIAG_ENTRYPOINT_DRY_RUN=1 时只做到生成有效配置就退出（便于本地验证），不启动 bot。
"""

import copy
import json
import logging
import os
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


def git_env(key_path: str | None) -> dict:
    """key 存在时让 git 用它，并首次连接自动接受 host key（accept-new，不盲目信任所有）。
    供 bake_repos.py 在 build 时使用（BuildKit secret 挂在 /run/secrets/git_ssh_key）。"""
    if not key_path:
        return {}
    return {
        "GIT_SSH_COMMAND": (
            f"ssh -i {key_path} -o StrictHostKeyChecking=accept-new "
            "-o IdentitiesOnly=yes -o BatchMode=yes"
        )
    }


def _load_manifest(dest_dir: str) -> dict[str, str]:
    """读 build 时写的 URL -> 目录名映射；没有 manifest 时返回空 dict。"""
    path = os.path.join(dest_dir, ".baked-manifest.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): str(v) for k, v in data.items() if isinstance(v, str)} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        log.warning("烘焙 manifest 损坏，已忽略：%s", path)
        return {}


def resolve_baked_repos(cfg: dict, dest_dir: str) -> list[str]:
    """把 codex.repos 里的 URL 映射到已烘焙目录；本地路径原样保留。不联网。"""
    codex_cfg = cfg.get("codex") or {}
    entries = [str(x) for x in (codex_cfg.get("repos") or []) if str(x).strip()]
    if not entries:
        return []
    manifest = _load_manifest(dest_dir)
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
        name = manifest.get(entry) or derive_repo_dir(entry, used)
        target = os.path.join(dest_dir, name)
        if os.path.isdir(target):
            local_paths.append(target)
        else:
            log.warning(
                "仓库未在镜像里烘焙，已跳过：%s（build 时 codex-repos.txt 里没它，或 clone 失败）",
                entry,
            )
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
    codex_cfg = cfg.get("codex") or {}
    dest_dir = os.path.expanduser(str(codex_cfg.get("repos_dir") or DEFAULT_REPOS_DIR))
    local_repos = resolve_baked_repos(cfg, dest_dir)
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
