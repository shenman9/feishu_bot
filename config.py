"""
配置加载模块

从 config/ 目录读取系统配置和插件配置。
- config/system.yaml: 全局配置（app_id, app_secret）
- config/{plugin_name}.yaml: 各插件独立配置
"""

from pathlib import Path

import yaml

_CONFIG_DIR = Path(__file__).parent / "config"


def load_config() -> dict:
    """加载全局系统配置 (config/system.yaml)"""
    path = _CONFIG_DIR / "system.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"系统配置文件不存在: {path}\n"
            "请复制 config/system.yaml.example 为 config/system.yaml 并填入实际值。"
        )

    with open(path) as f:
        config = yaml.safe_load(f) or {}

    if not config.get("app_id") or not config.get("app_secret"):
        raise ValueError("config/system.yaml 中 app_id 和 app_secret 不能为空")

    return config


def load_plugin_config(plugin_name: str) -> dict:
    """加载指定插件的配置 (config/{plugin_name}.yaml)

    文件不存在时返回空字典，插件将使用各自的默认值。
    """
    path = _CONFIG_DIR / f"{plugin_name}.yaml"
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}
