"""
石头剪刀布插件单元测试
"""

import pytest
from unittest.mock import MagicMock, patch

from plugins.rps_game.rps_plugin import RPSPlugin, CHOICES, OUTCOMES, _game_card


class TestGameCard:
    """卡片构造函数测试"""

    def test_card_has_buttons_by_default(self):
        """默认卡片包含按钮"""
        card = _game_card("标题", "内容")
        actions = [e for e in card["elements"] if e.get("tag") == "action"]
        assert len(actions) == 1

    def test_card_no_buttons_when_disabled(self):
        """show_buttons=False 时无按钮"""
        card = _game_card("标题", "内容", show_buttons=False)
        actions = [e for e in card["elements"] if e.get("tag") == "action"]
        assert len(actions) == 0

    def test_card_header(self):
        """卡片标题正确"""
        card = _game_card("我的标题", "body")
        assert card["header"]["title"]["content"] == "我的标题"


class TestRPSPlugin:
    """RPSPlugin 核心逻辑测试"""

    @pytest.fixture
    def rps(self):
        plugin = RPSPlugin()
        bot = MagicMock()
        plugin.on_register(bot)
        return plugin

    def test_properties(self, rps):
        """插件元信息正确"""
        assert rps.name == "石头剪刀布"
        assert rps.keyword == "石头剪刀布"
        assert rps.description

    def test_keyword_starts_game(self, rps):
        """发送关键词开始游戏"""
        rps.handle_message("u1", "c1", "石头剪刀布")
        assert rps.is_user_active("u1") is True
        rps.bot.reply_card.assert_called_once()

    def test_valid_choice_during_game(self, rps):
        """游戏中发送有效选项正常处理"""
        rps.handle_message("u1", "c1", "石头剪刀布")
        rps.bot.reply_card.reset_mock()

        rps.handle_message("u1", "c1", "石头")
        rps.bot.reply_card.assert_called_once()

    def test_invalid_choice_during_game(self, rps):
        """游戏中发送无效输入提示用户"""
        rps.handle_message("u1", "c1", "石头剪刀布")
        rps.bot.reply.reset_mock()

        rps.handle_message("u1", "c1", "无效输入")
        rps.bot.reply.assert_called_once()

    def test_end_game(self, rps):
        """发送结束退出游戏"""
        rps.handle_message("u1", "c1", "石头剪刀布")
        rps.handle_message("u1", "c1", "结束")
        assert rps.is_user_active("u1") is False

    def test_deactivate_user_clears_state(self, rps):
        """deactivate_user 清理用户状态"""
        rps.handle_message("u1", "c1", "石头剪刀布")
        rps.deactivate_user("u1")
        assert "u1" not in rps.user_states

    def test_not_playing_prompt(self, rps):
        """未开始游戏时提示用户"""
        rps.handle_message("u1", "c1", "石头")
        rps.bot.reply.assert_called_once()

    def test_outcomes_table_complete(self):
        """胜负表覆盖所有组合"""
        for bot_choice in CHOICES:
            for user_choice in CHOICES:
                assert bot_choice in OUTCOMES
                assert user_choice in OUTCOMES[bot_choice]

    def test_card_action_during_game(self, rps):
        """游戏中卡片按钮点击正常处理"""
        rps.handle_message("u1", "c1", "石头剪刀布")
        rps.bot.reply_card.reset_mock()

        rps.handle_card_action("u1", "c1", "m1", {"choice": "剪刀"})
        rps.bot.reply_card.assert_called_once()

    def test_card_action_not_playing(self, rps):
        """未在游戏中时卡片点击返回提示"""
        result = rps.handle_card_action("u1", "c1", "m1", {"choice": "石头"})
        rps.bot.make_card_response.assert_called_once()
