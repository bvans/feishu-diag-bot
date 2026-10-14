"""三层镜像结构单测：Dockerfile.base / Dockerfile / start.sh / compose / config 的分层约定。

不启动 Docker，只读文件断言关键行，防止分层约定被无意改坏。
"""

import os

ROOT = os.path.dirname(os.path.abspath(__file__))


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def test_base_image_from_cliproxyapi_and_installs_codex():
    dockerfile = _read("Dockerfile.base")
    assert "eceasy/cli-proxy-api" in dockerfile, "二次基础镜像必须 FROM CLIProxyAPI 官方镜像"
    assert "npm install -g @openai/codex" in dockerfile
    assert "uv python install 3.12" in dockerfile
    assert "nodejs" in dockerfile and "git" in dockerfile


def test_business_image_from_base_and_bakes_repos():
    dockerfile = _read("Dockerfile")
    assert "feishu-diag-base" in dockerfile, "业务镜像必须 FROM 二次基础镜像"
    assert "bake_repos.py" in dockerfile, "业务镜像 build 时必须烘焙仓库"
    assert 'ENTRYPOINT ["/app/start.sh"]' in dockerfile
    assert "eceasy/cli-proxy-api" not in dockerfile, "业务镜像不应直接 FROM 代理镜像"


def test_start_sh_starts_proxy_then_bot():
    start = _read("start.sh")
    assert "./CLIProxyAPI" in start, "start.sh 必须启动 CLIProxyAPI"
    assert "127.0.0.1:8317" in start, "start.sh 必须等 8317 就绪"
    assert "uv run python entrypoint.py" in start
    # 代理先于机器人启动：代理启动行在 entrypoint 行之前
    assert start.index("./CLIProxyAPI") < start.index("entrypoint.py")


def test_compose_single_service_and_mounts_proxy_config():
    compose = _read("docker-compose.example.yml")
    assert "cliproxyapi:" not in compose, "代理已进业务镜像，不再有独立 sidecar 服务"
    assert "./cliproxyapi-config.yaml:/CLIProxyAPI/config.yaml" in compose


def test_config_points_to_localhost_proxy():
    config = _read("config.example.yaml")
    assert "http://127.0.0.1:8317/v1" in config
    assert "http://cliproxyapi:8317" not in config, "代理已在同容器内，不再走 compose 服务名"


def test_build_script_builds_base_first():
    build = _read("scripts/build.sh")
    assert build.index("Dockerfile.base") < build.index("feishu-diag-bot"), \
        "build.sh 必须先构建二次基础镜像，再构建业务镜像"


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL PASS")


def test_proxy_pin_version_is_v8():
    """钉版示例必须与 config-version: 8 对得上，禁止悄悄回落到 v6 占位。"""
    for name in ("Dockerfile.base", "scripts/build.sh", "README.md"):
        text = _read(name)
        assert "eceasy/cli-proxy-api:v8.0.28" in text, f"{name} 里钉版示例不是 v8.0.28"
        assert "v6.0.0" not in text, f"{name} 里还有过期的 v6.0.0 占位"
    assert "config-version: 8" in _read("cliproxyapi-config.example.yaml")
