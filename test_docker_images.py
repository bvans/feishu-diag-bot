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
    assert 'ENTRYPOINT ["uv", "run", "python", "entrypoint.py"]' in dockerfile
    assert "eceasy/cli-proxy-api" not in dockerfile, "代理已拆出独立容器，不应进业务镜像"


def test_single_container_leftovers_are_gone():
    assert not os.path.exists(os.path.join(ROOT, "Dockerfile.base")), "二次基础镜像已废弃"
    assert not os.path.exists(os.path.join(ROOT, "start.sh")), "单容器启动脚本已废弃"


def test_compose_two_services_and_pinned_proxy():
    compose = _read("docker-compose.example.yml")
    assert "cliproxyapi:" in compose
    assert "feishu-diag-bot:" in compose
    assert "eceasy/cli-proxy-api:v8.0.28" in compose, "代理镜像钉到 v8.0.28（与 config-version: 8 对得上）"
    assert "./cliproxyapi-config.yaml:/CLIProxyAPI/config.yaml" in compose
    assert "depends_on:" in compose


def test_config_points_to_proxy_service():
    config = _read("config.example.yaml")
    assert "http://cliproxyapi:8317/v1" in config
    assert "http://127.0.0.1:8317/v1" not in config, "代理已拆出独立容器，bot 应走 compose 服务名"


def test_build_script_builds_only_business_image():
    build = _read("scripts/build.sh")
    assert "Dockerfile.base" not in build, "二次基础镜像已废弃，build.sh 不应再构建它"
    assert "feishu-diag-bot" in build
    assert "GIT_SSH_KEY" in build


def test_proxy_pin_version_matches_config_version():
    assert "config-version: 8" in _read("cliproxyapi-config.example.yaml")
    assert "v6.0.0" not in _read("docker-compose.example.yml"), "禁止回落到 v6 占位"


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL PASS")
