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


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL PASS")
