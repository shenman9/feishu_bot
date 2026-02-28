"""
config.py 单元测试
"""

import pytest
from unittest.mock import patch


def test_load_config_success(tmp_path):
    """系统配置文件存在且字段完整时正常加载"""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "system.yaml").write_text("app_id: test_id\napp_secret: test_secret\n")

    with patch("config._CONFIG_DIR", config_dir):
        from config import load_config
        cfg = load_config()

    assert cfg["app_id"] == "test_id"
    assert cfg["app_secret"] == "test_secret"


def test_load_config_file_not_found(tmp_path):
    """系统配置文件不存在时抛出 FileNotFoundError"""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    # 不创建 system.yaml

    with patch("config._CONFIG_DIR", config_dir):
        from config import load_config
        with pytest.raises(FileNotFoundError):
            load_config()


def test_load_config_missing_fields(tmp_path):
    """系统配置文件缺少必填字段时抛出 ValueError"""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "system.yaml").write_text("app_id: some_id\n")

    with patch("config._CONFIG_DIR", config_dir):
        from config import load_config
        with pytest.raises(ValueError):
            load_config()


def test_load_config_empty_file(tmp_path):
    """系统配置文件为空时抛出 ValueError"""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "system.yaml").write_text("")

    with patch("config._CONFIG_DIR", config_dir):
        from config import load_config
        with pytest.raises(ValueError):
            load_config()


def test_load_plugin_config_success(tmp_path):
    """插件配置文件存在时正常加载"""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "claude_code.yaml").write_text("timeout: 3600\nrun_as_user: bot\n")

    with patch("config._CONFIG_DIR", config_dir):
        from config import load_plugin_config
        cfg = load_plugin_config("claude_code")

    assert cfg["timeout"] == 3600
    assert cfg["run_as_user"] == "bot"


def test_load_plugin_config_file_not_found(tmp_path):
    """插件配置文件不存在时返回空字典"""
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    with patch("config._CONFIG_DIR", config_dir):
        from config import load_plugin_config
        cfg = load_plugin_config("nonexistent_plugin")

    assert cfg == {}


def test_load_plugin_config_empty_file(tmp_path):
    """插件配置文件为空时返回空字典"""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "claude_chat.yaml").write_text("")

    with patch("config._CONFIG_DIR", config_dir):
        from config import load_plugin_config
        cfg = load_plugin_config("claude_chat")

    assert cfg == {}
