"""
配置加载模块
从 config.yaml 读取 APP_ID、APP_SECRET 等配置。
"""

from pathlib import Path

import yaml

_CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config() -> dict:
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"配置文件不存在: {_CONFIG_PATH}\n"
            "请复制 config.yaml.example 为 config.yaml 并填入实际值。"
        )

    with open(_CONFIG_PATH) as f:
        config = yaml.safe_load(f) or {}

    if not config.get("app_id") or not config.get("app_secret"):
        raise ValueError("config.yaml 中 app_id 和 app_secret 不能为空")

    return config
