"""bot.py load_config 单测：飞书凭据环境变量优先于 config.yaml。"""

import os
import sys
import tempfile

sys.path.insert(0, __file__.rsplit("/", 1)[0])

# import bot 会在模块级 load_config(DIAG_BOT_CONFIG)，先给一个存在的最小配置。
_boot_dir = tempfile.mkdtemp(prefix="feishu-diag-bot-boot-")
_boot_cfg = os.path.join(_boot_dir, "config.yaml")
with open(_boot_cfg, "w", encoding="utf-8") as f:
    f.write('feishu:\n  app_id: "boot"\n  app_secret: "boot"\n')
os.environ["DIAG_BOT_CONFIG"] = _boot_cfg

import bot  # noqa: E402


def _write_cfg(tmp):
    path = os.path.join(tmp, "config.yaml")
    with open(path, "w", encoding="utf-8") as f:
        f.write('feishu:\n  app_id: "from-file"\n  app_secret: "file-secret"\n')
    return path


def test_load_config_env_overrides_file():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_cfg(tmp)
        old_id = os.environ.get("FEISHU_APP_ID")
        old_secret = os.environ.get("FEISHU_APP_SECRET")
        os.environ["FEISHU_APP_ID"] = "from-env"
        os.environ["FEISHU_APP_SECRET"] = "env-secret"
        try:
            cfg = bot.load_config(path)
            assert cfg["feishu"]["app_id"] == "from-env"
            assert cfg["feishu"]["app_secret"] == "env-secret"
        finally:
            if old_id is None:
                os.environ.pop("FEISHU_APP_ID", None)
            else:
                os.environ["FEISHU_APP_ID"] = old_id
            if old_secret is None:
                os.environ.pop("FEISHU_APP_SECRET", None)
            else:
                os.environ["FEISHU_APP_SECRET"] = old_secret


def test_load_config_file_fallback_without_env():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_cfg(tmp)
        old_id = os.environ.get("FEISHU_APP_ID")
        old_secret = os.environ.get("FEISHU_APP_SECRET")
        os.environ.pop("FEISHU_APP_ID", None)
        os.environ.pop("FEISHU_APP_SECRET", None)
        try:
            cfg = bot.load_config(path)
            assert cfg["feishu"]["app_id"] == "from-file"
            assert cfg["feishu"]["app_secret"] == "file-secret"
        finally:
            if old_id is not None:
                os.environ["FEISHU_APP_ID"] = old_id
            if old_secret is not None:
                os.environ["FEISHU_APP_SECRET"] = old_secret


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL PASS")
