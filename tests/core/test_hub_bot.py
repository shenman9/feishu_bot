"""
HubBot 消息路由单元测试
"""

import json

import pytest
from unittest.mock import MagicMock

from tests.conftest import StubPlugin


def _make_mock_event(
    sender_id: str,
    chat_id: str,
    text: str,
    *,
    message_id: str = "msg_unique",
    msg_type: str = "text",
    chat_type: str = "p2p",
    mentions: list | None = None,
):
    """构造 mock 的 P2ImMessageReceiveV1 事件对象"""
    event = MagicMock()
    event.event.sender.sender_id.user_id = sender_id
    event.event.message.chat_id = chat_id
    event.event.message.message_id = message_id
    event.event.message.message_type = msg_type
    event.event.message.chat_type = chat_type
    event.event.message.mentions = mentions
    event.event.message.content = json.dumps({"text": text})
    return event


def _make_mention(key: str):
    """构造 mock 的 MentionEvent 对象"""
    m = MagicMock()
    m.key = key
    return m


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
        assert ("user1", "chat1") not in bot.active_plugin


class TestPluginRouting:
    """插件消息路由测试"""

    def test_keyword_activates_plugin(self, bot_with_plugin):
        """发送关键词激活对应插件"""
        bot, plugin = bot_with_plugin
        bot.on_message("user1", "chat1", "test")
        assert bot.active_plugin.get(("user1", "chat1")) == "test"
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

        p1.deactivate_user.assert_called_once_with("user1", "chat1")
        assert mock_bot.active_plugin[("user1", "chat1")] == "b"

    def test_plugin_auto_deactivate_when_not_active(self, mock_bot):
        """插件 is_user_active 返回 False 时自动移除活跃状态"""
        p = StubPlugin(keyword="once")
        # is_user_active 始终返回 False（默认行为）
        mock_bot.register(p)

        mock_bot.on_message("user1", "chat1", "once")
        # 发送后续消息，插件处理后 is_user_active=False，应被移除
        mock_bot.on_message("user1", "chat1", "followup")
        assert ("user1", "chat1") not in mock_bot.active_plugin


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

        mock_bot.active_plugin[("u1", "c1")] = "game"
        mock_bot.on_card_action("u1", "c1", "m1", {"data": 1})
        p.handle_card_action.assert_called_once()

    def test_card_action_no_plugin_returns_toast(self, mock_bot):
        """无匹配插件时返回提示 toast"""
        mock_bot.make_card_response = MagicMock(return_value="toast_resp")
        result = mock_bot.on_card_action("u1", "c1", "m1", {})
        assert result == "toast_resp"


class TestFileMessageRouting:
    """文件消息路由测试"""

    def test_file_message_routes_to_active_plugin(self, bot_with_plugin):
        """文件消息转发给活跃插件"""
        bot, plugin = bot_with_plugin
        bot.on_message("u1", "c1", "test")  # 激活插件
        bot.on_file_message("u1", "c1", "msg1", "fk1", "a.txt")
        assert plugin.received_file_messages == [
            ("u1", "c1", "msg1", "fk1", "a.txt")
        ]

    def test_file_message_no_active_plugin_shows_hint(self, bot_with_plugin):
        """无活跃插件时提示用户"""
        bot, plugin = bot_with_plugin
        bot.on_file_message("u1", "c1", "msg1", "fk1", "a.txt")
        bot.reply.assert_called_once()
        msg = bot.reply.call_args[0][1]
        assert "文件阅读" in msg

    def test_file_message_auto_deactivate(self, mock_bot):
        """文件消息处理后插件 is_user_active=False 时自动移除"""
        p = StubPlugin(keyword="fp")
        mock_bot.register(p)
        mock_bot.on_message("u1", "c1", "fp")
        # StubPlugin.is_user_active 默认返回 False
        mock_bot.on_file_message("u1", "c1", "msg1", "fk1", "a.txt")
        assert ("u1", "c1") not in mock_bot.active_plugin


class TestBotMenuRouting:
    """机器人底部菜单栏事件路由测试"""

    def test_bot_menu_activates_plugin(self, bot_with_plugin):
        """菜单点击匹配插件 keyword 时激活插件"""
        bot, plugin = bot_with_plugin
        bot.on_bot_menu("user1", "ou_open1", "test")
        assert bot.active_plugin.get(("user1", "ou_open1")) == "test"
        assert plugin.received_messages == [("user1", "ou_open1", "test")]

    def test_bot_menu_switches_plugin(self, mock_bot):
        """菜单点击切换插件时旧插件被 deactivate"""
        p1 = StubPlugin(name="A", keyword="a")
        p2 = StubPlugin(name="B", keyword="b")
        p1.deactivate_user = MagicMock()
        mock_bot.register_all([p1, p2])

        mock_bot.on_bot_menu("user1", "ou_open1", "a")
        mock_bot.on_bot_menu("user1", "ou_open1", "b")

        p1.deactivate_user.assert_called_once_with("user1", "ou_open1")
        assert mock_bot.active_plugin[("user1", "ou_open1")] == "b"

    def test_bot_menu_unknown_key_shows_menu(self, bot_with_plugin):
        """菜单点击未匹配任何插件时展示菜单"""
        bot, plugin = bot_with_plugin
        bot.on_bot_menu("user1", "ou_open1", "不存在的功能")
        bot.reply_card.assert_called_once()

    def test_bot_menu_menu_keyword_shows_menu(self, bot_with_plugin):
        """菜单点击菜单关键词时展示功能菜单"""
        bot, plugin = bot_with_plugin
        bot.on_bot_menu("user1", "ou_open1", "菜单")
        bot.reply_card.assert_called_once()


class TestStripMentions:
    """@提及文本剥离测试"""

    def test_strip_single_mention(self, mock_bot):
        """剥离单个 @提及 占位符"""
        mentions = [_make_mention("@_user_1")]
        result = mock_bot._strip_mentions("@_user_1 你好", mentions)
        assert result == "你好"

    def test_strip_multiple_mentions(self, mock_bot):
        """剥离多个 @提及 占位符"""
        mentions = [_make_mention("@_user_1"), _make_mention("@_user_2")]
        result = mock_bot._strip_mentions("@_user_1 @_user_2 你好", mentions)
        assert result == "你好"

    def test_strip_mention_only_returns_empty(self, mock_bot):
        """仅有 @提及 无其他内容时返回空字符串"""
        mentions = [_make_mention("@_user_1")]
        result = mock_bot._strip_mentions("@_user_1", mentions)
        assert result == ""

    def test_strip_no_mentions_returns_original(self, mock_bot):
        """无 mentions 时文本不变"""
        result = mock_bot._strip_mentions("hello world", [])
        assert result == "hello world"

    def test_strip_mention_preserves_keyword(self, mock_bot):
        """剥离后保留关键词文本"""
        mentions = [_make_mention("@_user_1")]
        result = mock_bot._strip_mentions("@_user_1 Claude", mentions)
        assert result == "Claude"


class TestGroupChatRouting:
    """群聊 @机器人 消息路由测试（通过 _on_raw_message 端到端验证）"""

    def test_group_at_mention_keyword_activates_plugin(self, bot_with_plugin):
        """群聊中 @机器人+关键词 正确激活插件"""
        bot, plugin = bot_with_plugin
        event = _make_mock_event(
            "user1", "oc_group1", "@_user_1 test",
            message_id="grp_msg_001",
            chat_type="group",
            mentions=[_make_mention("@_user_1")],
        )
        bot._on_raw_message(event)
        assert bot.active_plugin.get(("user1", "oc_group1")) == "test"
        assert plugin.received_messages == [("user1", "oc_group1", "test")]

    def test_group_at_mention_forwards_to_active_plugin(self, bot_with_plugin):
        """群聊中 @机器人+消息 转发到活跃插件"""
        bot, plugin = bot_with_plugin
        # 先激活插件
        event1 = _make_mock_event(
            "user1", "oc_group1", "@_user_1 test",
            message_id="grp_msg_002",
            chat_type="group",
            mentions=[_make_mention("@_user_1")],
        )
        bot._on_raw_message(event1)

        # 发送后续消息
        event2 = _make_mock_event(
            "user1", "oc_group1", "@_user_1 你好世界",
            message_id="grp_msg_003",
            chat_type="group",
            mentions=[_make_mention("@_user_1")],
        )
        bot._on_raw_message(event2)
        assert len(plugin.received_messages) == 2
        assert plugin.received_messages[1] == ("user1", "oc_group1", "你好世界")

    def test_group_no_mention_ignored(self, bot_with_plugin):
        """群聊中无 @消息 被忽略，插件不收到任何消息"""
        bot, plugin = bot_with_plugin
        event = _make_mock_event(
            "user1", "oc_group1", "随便聊聊",
            message_id="grp_msg_004",
            chat_type="group",
            mentions=None,
        )
        bot._on_raw_message(event)
        assert len(plugin.received_messages) == 0
        bot.reply_card.assert_not_called()

    def test_group_empty_mentions_list_ignored(self, bot_with_plugin):
        """群聊中 mentions 为空列表时也被忽略"""
        bot, plugin = bot_with_plugin
        event = _make_mock_event(
            "user1", "oc_group1", "普通消息",
            message_id="grp_msg_005",
            chat_type="group",
            mentions=[],
        )
        bot._on_raw_message(event)
        assert len(plugin.received_messages) == 0

    def test_group_at_only_no_text_shows_menu(self, bot_with_plugin):
        """群聊中只 @ 无内容时展示菜单"""
        bot, plugin = bot_with_plugin
        event = _make_mock_event(
            "user1", "oc_group1", "@_user_1",
            message_id="grp_msg_006",
            chat_type="group",
            mentions=[_make_mention("@_user_1")],
        )
        bot._on_raw_message(event)
        # 剥离后 text 为空，空字符串不匹配任何关键词，应展示菜单
        bot.reply_card.assert_called_once()

    def test_group_at_menu_keyword(self, bot_with_plugin):
        """群聊中 @机器人+菜单关键词 展示菜单"""
        bot, plugin = bot_with_plugin
        event = _make_mock_event(
            "user1", "oc_group1", "@_user_1 菜单",
            message_id="grp_msg_007",
            chat_type="group",
            mentions=[_make_mention("@_user_1")],
        )
        bot._on_raw_message(event)
        bot.reply_card.assert_called_once()

    def test_group_multiple_mentions_stripped(self, bot_with_plugin):
        """群聊中 @多人 时所有 mention 占位符都被剥离"""
        bot, plugin = bot_with_plugin
        event = _make_mock_event(
            "user1", "oc_group1", "@_user_1 @_user_2 test",
            message_id="grp_msg_008",
            chat_type="group",
            mentions=[_make_mention("@_user_1"), _make_mention("@_user_2")],
        )
        bot._on_raw_message(event)
        # 剥离两个 mention 后得到 "test"，匹配插件关键词
        assert plugin.received_messages == [("user1", "oc_group1", "test")]

    def test_dm_message_unchanged(self, bot_with_plugin):
        """私聊消息行为不变（回归测试）"""
        bot, plugin = bot_with_plugin
        event = _make_mock_event(
            "user1", "ou_dm1", "test",
            message_id="dm_msg_001",
            chat_type="p2p",
        )
        bot._on_raw_message(event)
        assert plugin.received_messages == [("user1", "ou_dm1", "test")]

    def test_dm_message_no_chat_type_defaults_to_p2p(self, bot_with_plugin):
        """chat_type 为 None 时默认当作私聊处理"""
        bot, plugin = bot_with_plugin
        event = _make_mock_event(
            "user1", "ou_dm1", "test",
            message_id="dm_msg_002",
            chat_type="p2p",
        )
        # 模拟旧版 SDK 没有 chat_type 字段
        event.event.message.chat_type = None
        bot._on_raw_message(event)
        assert plugin.received_messages == [("user1", "ou_dm1", "test")]


class TestPluginExceptionHandling:
    """插件异常处理测试——验证插件抛异常时主进程不崩溃"""

    def test_handle_message_exception_does_not_crash(self, mock_bot):
        """handle_message 抛异常时主流程不崩溃，并回复错误提示"""
        p = StubPlugin(keyword="boom")
        p.handle_message = MagicMock(side_effect=RuntimeError("插件爆炸"))
        mock_bot.register(p)

        mock_bot.on_message("u1", "c1", "boom")
        # 不应崩溃，且回复了错误提示
        mock_bot.reply.assert_called_once()
        assert "遇到问题" in mock_bot.reply.call_args[0][1]

    def test_deactivate_user_exception_does_not_crash(self, mock_bot):
        """deactivate_user 抛异常时退出流程不崩溃"""
        p = StubPlugin(keyword="err")
        p.is_user_active = MagicMock(return_value=True)
        mock_bot.register(p)
        mock_bot.on_message("u1", "c1", "err")

        p.deactivate_user = MagicMock(side_effect=RuntimeError("清理爆炸"))
        # 退出不应崩溃
        mock_bot.on_message("u1", "c1", "退出")
        assert ("u1", "c1") not in mock_bot.active_plugin

    def test_is_user_active_exception_does_not_crash(self, mock_bot):
        """is_user_active 抛异常时不影响消息处理"""
        p = StubPlugin(keyword="flaky")
        mock_bot.register(p)
        mock_bot.on_message("u1", "c1", "flaky")

        p.is_user_active = MagicMock(side_effect=RuntimeError("状态查询爆炸"))
        # 后续消息不应崩溃
        mock_bot.on_message("u1", "c1", "后续消息")

    def test_card_action_exception_returns_toast(self, mock_bot):
        """handle_card_action 抛异常时返回错误 toast"""
        p = StubPlugin(keyword="card_err")
        p.handle_card_action = MagicMock(side_effect=RuntimeError("卡片处理爆炸"))
        mock_bot.register(p)

        result = mock_bot.on_card_action("u1", "c1", "m1", {"plugin": "card_err"})
        assert result is not None

    def test_file_message_exception_does_not_crash(self, mock_bot):
        """handle_file_message 抛异常时不崩溃"""
        p = StubPlugin(keyword="file_err")
        p.handle_file_message = MagicMock(side_effect=RuntimeError("文件处理爆炸"))
        p.is_user_active = MagicMock(return_value=True)
        mock_bot.register(p)
        mock_bot.active_plugin[("u1", "c1")] = "file_err"

        mock_bot.on_file_message("u1", "c1", "msg1", "fk1", "a.txt")
        mock_bot.reply.assert_called_once()
        assert "遇到问题" in mock_bot.reply.call_args[0][1]

    def test_register_all_skips_failed_plugin(self, mock_bot):
        """register_all 中某个插件注册失败时跳过，不影响其他插件"""
        p1 = StubPlugin(name="Good", keyword="good")
        p2 = StubPlugin(name="Bad", keyword="bad")
        p2.on_register = MagicMock(side_effect=RuntimeError("初始化爆炸"))
        p3 = StubPlugin(name="Also Good", keyword="also_good")

        mock_bot.register_all([p1, p2, p3])
        assert "good" in mock_bot.plugins
        assert "bad" not in mock_bot.plugins
        assert "also_good" in mock_bot.plugins
