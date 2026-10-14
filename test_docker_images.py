"""两容器部署结构单测：Dockerfile / compose / config 的分拆约定。

不启动 Docker，只读文件断言关键行，防止分拆约定被无意改坏。
"""

import os

ROOT = os.path.dirname(os.path.abspath(__file__))


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def test_business_image_from_uv_and_installs_codex():
    dockerfile = _read("Dockerfile")
    assert "ghcr.io/astral-sh/uv:python3.12" in dockerfile, "业务镜像 FROM uv 镜像"
    assert "npm install -g @openai/codex" in dockerfile
    assert "bake_repos.py" in dockerfile, "业务镜像 build 时必须烘焙仓库"
    assert "webchat.py" in dockerfile, "网页聊天窗口必须拷进业务镜像"
    assert "admin.py" in dockerfile, "管理配置界面必须拷进业务镜像"
    assert 'ENTRYPOINT ["uv", "run", "python", "entrypoint.py"]' in dockerfile
    assert "eceasy/cli-proxy-api" not in dockerfile, "代理已拆出独立容器，不应进业务镜像"


def test_single_container_leftovers_are_gone():
    assert not os.path.exists(os.path.join(ROOT, "Dockerfile.base")), "二次基础镜像已废弃"
    assert not os.path.exists(os.path.join(ROOT, "start.sh")), "单容器启动脚本已废弃"


def test_compose_two_services_and_pinned_proxy():
    compose = _read("docker-compose.example.yml")
    assert "cliproxyapi:" in compose
    assert "feishu-diag-bot:" in compose
    assert "image: ghcr.io/bvans/feishu-diag-bot:latest" in compose, \
        "业务镜像默认拉 GHCR 预构建镜像，不再本地 build"
    for line in compose.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("build:"), \
            f"compose 里不应有生效的 build: 行（本地构建用注释）：{line}"
    assert "eceasy/cli-proxy-api:v8.0.28" in compose, "代理镜像钉到 v8.0.28（与 config-version: 8 对得上）"
    assert "./cliproxyapi-config.yaml:/CLIProxyAPI/config.yaml" in compose
    assert "AGENTS.md:/app/AGENTS.md" in compose, "全局 AGENTS.md 挂载说明应在 compose 里"
    assert "depends_on:" in compose


def test_config_points_to_proxy_service():
    config = _read("config.example.yaml")
    assert "http://cliproxyapi:8317/v1" in config
    assert "http://127.0.0.1:8317/v1" not in config, "代理已拆出独立容器，bot 应走 compose 服务名"


def test_build_script_builds_only_business_image():
    build = _read("scripts/build.sh")
    assert "Dockerfile.base" not in build, "二次基础镜像已废弃，build.sh 不应再构建它"
    assert "feishu-diag-bot" in build
    assert "ghcr.io/bvans/feishu-diag-bot:latest" in build, \
        "本地构建应同时打 GHCR tag，compose 可直接用"
    assert "GIT_SSH_KEY" in build


def test_github_action_builds_and_pushes_image():
    import yaml

    wf = _read(".github/workflows/docker-build.yml")
    data = yaml.safe_load(wf)
    on = data.get("on") or data.get(True)  # YAML 1.1 里 on 会被解析成 True
    assert "push" in on and "pull_request" in on and "workflow_dispatch" in on
    jobs = data["jobs"]
    assert "test" in jobs and "build" in jobs, "先跑单测，通过后才构建推送"
    assert jobs["build"].get("needs") == "test"
    build_steps = jobs["build"]["steps"]
    uses = [st.get("uses", "") for st in build_steps]
    assert any("docker/build-push-action" in u for u in uses), "用 build-push-action 构建推送"
    assert any("docker/login-action" in u for u in uses), "推送前需登录 GHCR"
    assert "ghcr.io/${{ github.repository }}" in wf, "镜像名应为 ghcr.io/<owner>/<repo>"
    assert "git_ssh_key" in wf, "私有仓库需传 GIT_SSH_KEY secret（BuildKit secret）"
    assert "packages: write" in wf, "推送 GHCR 需要 packages: write 权限"


def test_proxy_pin_version_matches_config_version():
    assert "config-version: 8" in _read("cliproxyapi-config.example.yaml")
    assert "v6.0.0" not in _read("docker-compose.example.yml"), "禁止回落到 v6 占位"


def _workflow():
    import yaml

    return yaml.safe_load(_read(".github/workflows/docker-build.yml"))


def test_workflow_tests_fail_fast_before_image_build():
    data = _workflow()
    assert data["permissions"] == {"contents": "read"}
    assert data["jobs"]["build"]["needs"] == "test"
    steps = data["jobs"]["test"]["steps"]
    runs = "\n".join(step.get("run", "") for step in steps)
    assert "uv sync" in runs
    assert "set -euo pipefail" in runs
    assert 'uv run python "$test_file"' in runs
    assert "test_*.py" in runs


def test_workflow_only_publishes_and_exposes_ssh_key_on_release_refs():
    steps = _workflow()["jobs"]["build"]["steps"]
    login = next(s for s in steps if "docker/login-action@" in s.get("uses", ""))
    build = next(s for s in steps if "docker/build-push-action@" in s.get("uses", ""))
    release_guard = login["if"]
    assert "github.event_name != 'pull_request'" in release_guard
    assert "github.ref == 'refs/heads/main'" in release_guard
    assert "startsWith(github.ref, 'refs/tags/v')" in release_guard
    assert build["with"]["push"] == "${{ " + release_guard + " }}"
    assert release_guard + " && secrets.GIT_SSH_KEY || ''" in build["with"]["secrets"]
    assert "secrets.GITHUB_TOKEN" in login["with"]["password"]
    assert all(s.get("with", {}).get("persist-credentials") is False
               for s in steps if s.get("uses", "").startswith("actions/checkout@"))


def test_pr_builds_cannot_bake_or_restore_private_repositories():
    steps = _workflow()["jobs"]["build"]["steps"]
    skip = next(s for s in steps if "Path('codex-repos.txt').write_text('')" in s.get("run", ""))
    assert "github.event_name == 'pull_request'" in skip["if"]
    build = next(s for s in steps if "docker/build-push-action@" in s.get("uses", ""))
    assert steps.index(skip) < steps.index(build)
    assert build["with"]["context"] == ".", "使用 checkout 后已清空仓库清单的上下文"
    assert "cache-to" not in build["with"], "不把烘焙的私有代码放入 PR 可读取的 Actions 缓存"
    assert "cache-from" not in build["with"]


def test_workflow_triggers_version_tags_and_keeps_latest_on_main():
    data = _workflow()
    triggers = data.get("on") or data[True]
    assert triggers["push"]["branches"] == ["main"]
    assert triggers["push"]["tags"] == ["v*"]
    assert ".github/workflows/docker-build.yml" in triggers["push"]["paths"]
    meta = next(s for s in data["jobs"]["build"]["steps"]
                if "docker/metadata-action@" in s.get("uses", ""))
    assert meta["with"]["flavor"] == "latest=false"
    assert "value=latest,enable=${{ github.ref == 'refs/heads/main' }}" in meta["with"]["tags"]
    assert "type=sha,prefix=sha-" in meta["with"]["tags"]
    assert "type=semver,pattern={{version}}" in meta["with"]["tags"]
    assert "type=semver,pattern={{major}}.{{minor}}" in meta["with"]["tags"]


def test_bake_uses_installed_dependencies_and_refreshes_for_each_ci_build():
    dockerfile = _read("Dockerfile")
    assert "/app/.venv/bin/python bake_repos.py" in dockerfile
    assert "python3 bake_repos.py" not in dockerfile
    assert "ARG REPOS_CACHE_BUST" in dockerfile
    steps = _workflow()["jobs"]["build"]["steps"]
    build = next(s for s in steps if "docker/build-push-action@" in s.get("uses", ""))
    assert "REPOS_CACHE_BUST=${{ github.run_id }}-${{ github.run_attempt }}" in build["with"]["build-args"]


def test_local_credentials_are_excluded_from_build_context():
    patterns = _read(".dockerignore").splitlines()
    assert ".env*" in patterns
    assert "!.env.example" in patterns
    assert "config.yaml" in patterns
    assert "cliproxyapi-config.yaml" in patterns
    assert ".local-tmp/" in patterns


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL PASS")
