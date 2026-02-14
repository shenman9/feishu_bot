"""
HubBot 消息路由单元测试
"""

import pytest
from unittest.mock import MagicMock

from tests.conftest import StubPlugin


class TestPluginRegistration:
    """插件注册相关测试"""

    def test_register_single_plugin(self, mock_bot):
        """注册单个插件后可通过 keyword 查找"""
        p = StubPlugin(keyword="ping")
        mock_bot.register(p)
        assert "ping" in mock_bot.plugins
        assert p.bot is mock_bot

    def test_register_all(self, mock_bot):
        """批量注册多个插件"""
        p1 = StubPlugin(name="A", keyword="a")
        p2 = StubPlugin(name="B", keyword="b")
        mock_bot.register_all([p1, p2])
        assert "a" in mock_bot.plugins
        assert "b" in mock_bot.plugins


class TestMenuRouting:
    """菜单关键词路由测试"""

    @pytest.mark.parametrize("text", ["菜单", "menu", "帮助", "help"])
    def test_menu_keywords_trigger_menu(self, bot_with_plugin, text):
        """菜单关键词触发菜单卡片"""
        bot, plugin = bot_with_plugin
        bot.on_message("user1", "chat1", text)
        bot.reply_card.assert_called_once()

    def test_no_context_shows_menu(self, bot_with_plugin):
        """无活跃插件且不匹配关键词时展示菜单"""
        bot, plugin = bot_with_plugin
        bot.on_message("user1", "chat1", "随便说点什么")
        bot.reply_card.assert_called_once()


class TestExitRouting:
    """退出关键词路由测试"""

    @pytest.mark.parametrize("text", ["退出", "exit", "返回"])
    def test_exit_keywords(self, bot_with_plugin, text):
        """退出关键词清除活跃插件并回复"""
        bot, plugin = bot_with_plugin
        # 先激活插件
        bot.on_message("user1", "chat1", plugin.keyword)
        bot.reply.reset_mock()

        bot.on_message("user1", "chat1", text)
        bot.reply.assert_called_once()
        assert "user1" not in bot.active_plugin


class TestPluginRouting:
    """插件消息路由测试"""

    def test_keyword_activates_plugin(self, bot_with_plugin):
        """发送关键词激活对应插件"""
        bot, plugin = bot_with_plugin
        bot.on_message("user1", "chat1", "test")
        assert bot.active_plugin.get("user1") == "test"
        assert plugin.received_messages == [("user1", "chat1", "test")]

    def test_active_plugin_receives_followup(self, bot_with_plugin):
        """活跃插件接收后续消息"""
        bot, plugin = bot_with_plugin
        bot.on_message("user1", "chat1", "test")
        bot.on_message("user1", "chat1", "后续消息")
        assert len(plugin.received_messages) == 2
        assert plugin.received_messages[1] == ("user1", "chat1", "后续消息")

    def test_switching_plugin_deactivates_previous(self, mock_bot):
        """切换插件时旧插件被 deactivate"""
        p1 = StubPlugin(name="A", keyword="a")
        p2 = StubPlugin(name="B", keyword="b")
        p1.deactivate_user = MagicMock()
        mock_bot.register_all([p1, p2])

        mock_bot.on_message("user1", "chat1", "a")
        mock_bot.on_message("user1", "chat1", "b")

        p1.deactivate_user.assert_called_once_with("user1")
        assert mock_bot.active_plugin["user1"] == "b"

    def test_plugin_auto_deactivate_when_not_active(self, mock_bot):
        """插件 is_user_active 返回 False 时自动移除活跃状态"""
        p = StubPlugin(keyword="once")
        # is_user_active 始终返回 False（默认行为）
        mock_bot.register(p)

        mock_bot.on_message("user1", "chat1", "once")
        # 发送后续消息，插件处理后 is_user_active=False，应被移除
        mock_bot.on_message("user1", "chat1", "followup")
        assert "user1" not in mock_bot.active_plugin


class TestCardActionRouting:
    """卡片事件路由测试"""

    def test_card_action_routes_by_plugin_field(self, mock_bot):
        """卡片 action_value 中的 plugin 字段用于路由"""
        p = StubPlugin(keyword="game")
        p.handle_card_action = MagicMock(return_value=None)
        mock_bot.register(p)

        mock_bot.on_card_action("u1", "c1", "m1", {"plugin": "game", "data": 1})
        p.handle_card_action.assert_called_once_with("u1", "c1", "m1", {"plugin": "game", "data": 1})

    def test_card_action_fallback_to_active_plugin(self, mock_bot):
        """无 plugin 字段时回退到活跃插件"""
        p = StubPlugin(keyword="game")
        p.handle_card_action = MagicMock(return_value=None)
        p.is_user_active = MagicMock(return_value=True)
        mock_bot.register(p)

        mock_bot.active_plugin["u1"] = "game"
        mock_bot.on_card_action("u1", "c1", "m1", {"data": 1})
        p.handle_card_action.assert_called_once()

    def test_card_action_no_plugin_returns_toast(self, mock_bot):
        """无匹配插件时返回提示 toast"""
        mock_bot.make_card_response = MagicMock(return_value="toast_resp")
        result = mock_bot.on_card_action("u1", "c1", "m1", {})
        assert result == "toast_resp"
