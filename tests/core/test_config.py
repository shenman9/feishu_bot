"""
config.py 单元测试
"""

import pytest
from unittest.mock import patch, mock_open


def test_load_config_success(tmp_path):
    """配置文件存在且字段完整时正常加载"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("app_id: test_id\napp_secret: test_secret\n")

    with patch("config._CONFIG_PATH", config_file):
        from config import load_config
        cfg = load_config()

    assert cfg["app_id"] == "test_id"
    assert cfg["app_secret"] == "test_secret"


def test_load_config_file_not_found(tmp_path):
    """配置文件不存在时抛出 FileNotFoundError"""
    missing = tmp_path / "nonexistent.yaml"

    with patch("config._CONFIG_PATH", missing):
        from config import load_config
        with pytest.raises(FileNotFoundError):
            load_config()


def test_load_config_missing_fields(tmp_path):
    """配置文件缺少必填字段时抛出 ValueError"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("app_id: some_id\n")

    with patch("config._CONFIG_PATH", config_file):
        from config import load_config
        with pytest.raises(ValueError):
            load_config()


def test_load_config_empty_file(tmp_path):
    """配置文件为空时抛出 ValueError"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("")

    with patch("config._CONFIG_PATH", config_file):
        from config import load_config
        with pytest.raises(ValueError):
            load_config()
