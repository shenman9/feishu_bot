"""
Plugin 基类单元测试
"""

import pytest
from unittest.mock import MagicMock

from tests.conftest import StubPlugin


def test_plugin_properties():
    """插件属性正确返回"""
    p = StubPlugin(name="A", keyword="a", description="desc")
    assert p.name == "A"
    assert p.keyword == "a"
    assert p.description == "desc"


def test_plugin_bot_initially_none():
    """插件创建后 bot 引用为 None"""
    p = StubPlugin()
    assert p.bot is None


def test_on_register_injects_bot():
    """on_register 注入 bot 引用"""
    p = StubPlugin()
    fake_bot = MagicMock()
    p.on_register(fake_bot)
    assert p.bot is fake_bot


def test_is_user_active_default_false():
    """默认 is_user_active 返回 False"""
    p = StubPlugin()
    assert p.is_user_active("user1") is False


def test_deactivate_user_no_error():
    """默认 deactivate_user 不报错"""
    p = StubPlugin()
    p.deactivate_user("user1")  # 不应抛异常


def test_handle_message_records():
    """StubPlugin 记录收到的消息"""
    p = StubPlugin()
    p.handle_message("u1", "c1", "hello")
    assert p.received_messages == [("u1", "c1", "hello")]
