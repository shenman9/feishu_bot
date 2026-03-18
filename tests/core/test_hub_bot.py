"""
HubBot 消息路由单元测试
"""

import json
import time

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
    content_override: dict | None = None,
):
    """构造 mock 的 P2ImMessageReceiveV1 事件对象

    content_override: 若指定则替代默认的 {"text": text} 作为 message.content
    """
    event = MagicMock()
    event.event.sender.sender_id.user_id = sender_id
    event.event.message.chat_id = chat_id
    event.event.message.message_id = message_id
    event.event.message.message_type = msg_type
    event.event.message.chat_type = chat_type
    event.event.message.mentions = mentions
    if content_override is not None:
        event.event.message.content = json.dumps(content_override)
    else:
        event.event.message.content = json.dumps({"text": text})
    return event


def _make_mention(key: str, open_id: str = "ou_bot_open_id"):
    """构造 mock 的 MentionEvent 对象

    open_id: 被 @ 用户的 open_id，默认为机器人的 open_id
    """
    m = MagicMock()
    m.key = key
    m.id.open_id = open_id
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


class TestIsBotMentioned:
    """机器人 @提及 精确判断测试"""

    def test_empty_mentions_returns_false(self, mock_bot):
        """空 mentions 列表返回 False"""
        assert mock_bot._is_bot_mentioned([]) is False

    def test_bot_mentioned_returns_true(self, mock_bot):
        """mentions 中包含机器人 open_id 时返回 True"""
        mention = _make_mention("@_user_1", open_id="ou_bot_open_id")
        assert mock_bot._is_bot_mentioned([mention]) is True

    def test_other_user_mentioned_returns_false(self, mock_bot):
        """mentions 中只有其他用户时返回 False"""
        mention = _make_mention("@_user_1", open_id="ou_other_user")
        assert mock_bot._is_bot_mentioned([mention]) is False

    def test_bot_among_multiple_mentions(self, mock_bot):
        """多个 mention 中包含机器人时返回 True"""
        mentions = [
            _make_mention("@_user_1", open_id="ou_other_user"),
            _make_mention("@_user_2", open_id="ou_bot_open_id"),
        ]
        assert mock_bot._is_bot_mentioned(mentions) is True

    def test_fallback_when_open_id_unknown(self, mock_bot):
        """无法获取机器人 open_id 时回退为宽松策略（有 mention 即 True）"""
        mock_bot._bot_open_id = ""  # 表示已尝试但获取失败
        mention = _make_mention("@_user_1", open_id="ou_anyone")
        assert mock_bot._is_bot_mentioned([mention]) is True

    def test_fallback_empty_mentions_still_false(self, mock_bot):
        """即使回退策略，空 mentions 仍返回 False"""
        mock_bot._bot_open_id = ""
        assert mock_bot._is_bot_mentioned([]) is False


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

    def test_group_at_other_user_ignored(self, bot_with_plugin):
        """群聊中 @其他用户（非机器人）不触发机器人响应"""
        bot, plugin = bot_with_plugin
        event = _make_mock_event(
            "user1", "oc_group1", "@_user_1 你好",
            message_id="grp_msg_009",
            chat_type="group",
            mentions=[_make_mention("@_user_1", open_id="ou_other_user")],
        )
        bot._on_raw_message(event)
        assert len(plugin.received_messages) == 0
        bot.reply_card.assert_not_called()

    def test_group_at_other_user_with_keyword_ignored(self, bot_with_plugin):
        """群聊中 @其他用户+插件关键词 不激活插件"""
        bot, plugin = bot_with_plugin
        event = _make_mock_event(
            "user1", "oc_group1", "@_user_1 test",
            message_id="grp_msg_010",
            chat_type="group",
            mentions=[_make_mention("@_user_1", open_id="ou_other_user")],
        )
        bot._on_raw_message(event)
        assert ("user1", "oc_group1") not in bot.active_plugin
        assert len(plugin.received_messages) == 0

    def test_group_at_bot_and_other_user(self, bot_with_plugin):
        """群聊中同时 @机器人和其他用户，机器人正常响应"""
        bot, plugin = bot_with_plugin
        event = _make_mock_event(
            "user1", "oc_group1", "@_user_1 @_user_2 test",
            message_id="grp_msg_011",
            chat_type="group",
            mentions=[
                _make_mention("@_user_1", open_id="ou_bot_open_id"),
                _make_mention("@_user_2", open_id="ou_other_user"),
            ],
        )
        bot._on_raw_message(event)
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


class TestWakeMode:
    """唤醒模式测试——群聊中通过卡片切换是否需要 @机器人"""

    def test_wake_keyword_sends_card_in_group(self, bot_with_plugin):
        """群聊中发送'唤醒模式'触发选项卡片"""
        bot, plugin = bot_with_plugin
        event = _make_mock_event(
            "user1", "oc_group1", "@_user_1 唤醒模式",
            message_id="wake_msg_001",
            chat_type="group",
            mentions=[_make_mention("@_user_1")],
        )
        bot._on_raw_message(event)
        bot.reply_card.assert_called_once()
        # 不应路由到插件
        assert plugin.received_messages == []

    def test_wake_keyword_ignored_in_p2p(self, bot_with_plugin):
        """私聊中'唤醒模式'不触发卡片，正常路由给插件/菜单"""
        bot, plugin = bot_with_plugin
        event = _make_mock_event(
            "user1", "ou_dm1", "唤醒模式",
            message_id="wake_msg_002",
            chat_type="p2p",
        )
        bot._on_raw_message(event)
        # 私聊中不触发唤醒模式卡片，应走正常路由（无匹配插件 → 菜单）
        bot.reply_card.assert_called_once()  # 菜单卡片
        assert "唤醒模式" not in str(bot.reply_card.call_args)

    def test_enable_wake_mode_allows_no_mention(self, bot_with_plugin):
        """开启唤醒模式后群聊非@消息能正常处理"""
        bot, plugin = bot_with_plugin
        bot._wake_mode_groups.add("oc_group1")
        event = _make_mock_event(
            "user1", "oc_group1", "test",
            message_id="wake_msg_003",
            chat_type="group",
            mentions=None,
        )
        bot._on_raw_message(event)
        assert plugin.received_messages == [("user1", "oc_group1", "test")]

    def test_disable_wake_mode_blocks_no_mention(self, bot_with_plugin):
        """关闭唤醒模式后群聊非@消息被忽略"""
        bot, plugin = bot_with_plugin
        # 先开启再关闭
        bot._wake_mode_groups.add("oc_group1")
        bot._wake_mode_groups.discard("oc_group1")
        event = _make_mock_event(
            "user1", "oc_group1", "test",
            message_id="wake_msg_004",
            chat_type="group",
            mentions=None,
        )
        bot._on_raw_message(event)
        assert plugin.received_messages == []

    def test_wake_mode_per_group_isolation(self, bot_with_plugin):
        """唤醒模式按群隔离，群A开启不影响群B"""
        bot, plugin = bot_with_plugin
        bot._wake_mode_groups.add("oc_group_a")
        # 群A: 非@消息通过
        event_a = _make_mock_event(
            "user1", "oc_group_a", "test",
            message_id="wake_msg_005",
            chat_type="group",
            mentions=None,
        )
        bot._on_raw_message(event_a)
        assert plugin.received_messages == [("user1", "oc_group_a", "test")]
        # 群B: 非@消息被忽略
        event_b = _make_mock_event(
            "user1", "oc_group_b", "test",
            message_id="wake_msg_006",
            chat_type="group",
            mentions=None,
        )
        bot._on_raw_message(event_b)
        assert len(plugin.received_messages) == 1  # 仍然只有群A的消息

    def test_card_action_set_wake_mode_all(self, mock_bot):
        """卡片点击'全部唤醒'将群加入唤醒列表"""
        action_value = {"action": "set_wake_mode", "mode": "all"}
        mock_bot._handle_set_wake_mode("oc_group1", action_value)
        assert "oc_group1" in mock_bot._wake_mode_groups

    def test_card_action_set_wake_mode_mention_only(self, mock_bot):
        """卡片点击'仅@唤醒'将群移出唤醒列表"""
        mock_bot._wake_mode_groups.add("oc_group1")
        action_value = {"action": "set_wake_mode", "mode": "mention_only"}
        mock_bot._handle_set_wake_mode("oc_group1", action_value)
        assert "oc_group1" not in mock_bot._wake_mode_groups

    def test_card_action_returns_updated_card(self, mock_bot):
        """卡片点击后返回刷新后的卡片"""
        action_value = {"action": "set_wake_mode", "mode": "all"}
        # _on_raw_card_action 会调用 _handle_set_wake_mode，需要直接测试
        result = mock_bot._handle_set_wake_mode("oc_group1", action_value)
        assert result is not None
        assert result.card is not None

    def test_wake_mode_with_mention_still_works(self, bot_with_plugin):
        """开启唤醒模式后带@的消息仍然正常处理"""
        bot, plugin = bot_with_plugin
        bot._wake_mode_groups.add("oc_group1")
        event = _make_mock_event(
            "user1", "oc_group1", "@_user_1 test",
            message_id="wake_msg_007",
            chat_type="group",
            mentions=[_make_mention("@_user_1")],
        )
        bot._on_raw_message(event)
        assert plugin.received_messages == [("user1", "oc_group1", "test")]

    def test_wake_mode_at_other_user_passes_through(self, bot_with_plugin):
        """唤醒模式下 @其他用户的消息正常通过，且不剥离 @占位符"""
        bot, plugin = bot_with_plugin
        bot._wake_mode_groups.add("oc_group1")
        # 先激活插件
        event1 = _make_mock_event(
            "user1", "oc_group1", "test",
            message_id="wake_msg_008a",
            chat_type="group",
            mentions=None,
        )
        bot._on_raw_message(event1)
        plugin.received_messages.clear()

        # @其他用户的消息：在唤醒模式下通过，且不剥离占位符
        event2 = _make_mock_event(
            "user1", "oc_group1", "@_user_1 你说的对",
            message_id="wake_msg_008b",
            chat_type="group",
            mentions=[_make_mention("@_user_1", open_id="ou_other_user")],
        )
        bot._on_raw_message(event2)
        assert plugin.received_messages == [
            ("user1", "oc_group1", "@_user_1 你说的对")
        ]


# ---- 合并转发消息测试辅助 ----

def _make_message_item(
    msg_type: str, content, message_id: str = "om_sub",
    sender_id: str = "ou_sender_1", create_time: str = "1700000000000",
    sender_type: str = "user",
    upper_message_id: str | None = None,
):
    """构造 mock 的 Message 对象（GetMessage 返回的条目）

    content: dict 会被 json.dumps，str 直接赋值（用于 merge_forward 的固定字符串）
    sender_type: "user" 或 "app"
    upper_message_id: 父消息 ID，用于构建消息树
    """
    item = MagicMock()
    item.msg_type = msg_type
    item.message_id = message_id
    item.create_time = create_time
    item.sender.id = sender_id
    item.sender.sender_type = sender_type
    item.upper_message_id = upper_message_id
    if isinstance(content, dict):
        item.body.content = json.dumps(content)
    else:
        item.body.content = content
    return item


def _make_get_response(items: list, *, success: bool = True):
    """构造 mock 的 GetMessage API 响应"""
    resp = MagicMock()
    resp.success.return_value = success
    if success:
        resp.data.items = items
    else:
        resp.code = 99999
        resp.msg = "mock error"
    return resp


def _setup_merge_bot(bot):
    """为合并转发测试设置 mock client，跳过用户名 API 调用"""
    bot.client = MagicMock()
    # 用户名解析直接返回 sender_id 本身，避免调用 contact API
    bot._batch_resolve_sender_names = lambda ids: {oid: oid for oid in ids}


# 固定时间戳及其格式化结果（UTC+8）
_TS_A = "1700000000000"   # -> 11-15 06:13:20
_TS_B = "1700000060000"   # -> 11-15 06:14:20
_TS_A_STR = "11-15 06:13:20"
_TS_B_STR = "11-15 06:14:20"


class TestMergeForwardRouting:
    """合并转发消息处理测试"""

    def test_merge_forward_extracts_text(self, bot_with_plugin):
        """正常合并转发：超时后独立处理，包裹标签、缩进格式、多条子消息"""
        bot, plugin = bot_with_plugin
        bot.on_message("user1", "chat1", "test")
        plugin.received_messages.clear()

        _setup_merge_bot(bot)
        bot.client.im.v1.message.get.return_value = _make_get_response([
            _make_message_item("merge_forward", "Merged and Forwarded Message",
                               message_id="merge_msg_001"),
            _make_message_item("text", {"text": "你好"}, message_id="om_1",
                               sender_id="Alice", create_time=_TS_A,
                               upper_message_id="merge_msg_001"),
            _make_message_item("text", {"text": "世界"}, message_id="om_2",
                               sender_id="Bob", create_time=_TS_B,
                               upper_message_id="merge_msg_001"),
        ])

        event = _make_mock_event(
            "user1", "chat1", "",
            message_id="merge_msg_001",
            msg_type="merge_forward",
        )
        bot._on_raw_message(event)
        # 转发消息暂存在缓冲区，超时后才处理
        assert len(plugin.received_messages) == 0
        bot._on_forward_timeout("user1", "chat1")
        assert len(plugin.received_messages) == 1
        text = plugin.received_messages[0][2]
        # 标签包裹
        assert text.startswith("<forwarded_messages>")
        assert text.endswith("</forwarded_messages>")
        # 对话内容：发送者头 + 缩进内容
        assert f"[{_TS_A_STR}] Alice:" in text
        assert "    你好" in text
        assert f"[{_TS_B_STR}] Bob:" in text
        assert "    世界" in text

    def test_merge_forward_api_failure(self, bot_with_plugin):
        """GetMessage API 失败时回复提示"""
        bot, plugin = bot_with_plugin

        _setup_merge_bot(bot)
        bot.client.im.v1.message.get.return_value = _make_get_response(
            [], success=False,
        )

        event = _make_mock_event(
            "user1", "chat1", "",
            message_id="merge_msg_002",
            msg_type="merge_forward",
        )
        bot._on_raw_message(event)
        bot.reply.assert_called_once()
        assert "未包含可识别的文本" in bot.reply.call_args[0][1]

    def test_merge_forward_only_self_item(self, bot_with_plugin):
        """仅返回自身条目、无子消息时回复提示"""
        bot, plugin = bot_with_plugin

        _setup_merge_bot(bot)
        bot.client.im.v1.message.get.return_value = _make_get_response([
            _make_message_item("merge_forward", "Merged and Forwarded Message",
                               message_id="merge_msg_003"),
        ])

        event = _make_mock_event(
            "user1", "chat1", "",
            message_id="merge_msg_003",
            msg_type="merge_forward",
        )
        bot._on_raw_message(event)
        bot.reply.assert_called_once()
        assert "未包含可识别的文本" in bot.reply.call_args[0][1]

    def test_merge_forward_image_placeholder(self, bot_with_plugin):
        """图片子消息显示占位提示而非静默丢弃"""
        bot, plugin = bot_with_plugin
        bot.on_message("user1", "chat1", "test")
        plugin.received_messages.clear()

        _setup_merge_bot(bot)
        bot.client.im.v1.message.get.return_value = _make_get_response([
            _make_message_item("merge_forward", "Merged and Forwarded Message",
                               message_id="merge_msg_004"),
            _make_message_item("text", {"text": "看这张图"}, message_id="om_1",
                               sender_id="Alice", create_time=_TS_A,
                               upper_message_id="merge_msg_004"),
            _make_message_item("image", {"image_key": "img_xxx"}, message_id="om_2",
                               sender_id="Alice", create_time=_TS_B,
                               upper_message_id="merge_msg_004"),
        ])

        event = _make_mock_event(
            "user1", "chat1", "",
            message_id="merge_msg_004",
            msg_type="merge_forward",
        )
        bot._on_raw_message(event)
        bot._on_forward_timeout("user1", "chat1")
        text = plugin.received_messages[0][2]
        assert "看这张图" in text
        assert "[图片]" in text

    def test_merge_forward_in_group_strips_mentions(self, bot_with_plugin):
        """群聊合并转发+留言合并时，正确剥离留言中的 @提及"""
        bot, plugin = bot_with_plugin
        # 先激活插件
        event_activate = _make_mock_event(
            "user1", "oc_group1", "@_user_1 test",
            message_id="grp_activate_005",
            chat_type="group",
            mentions=[_make_mention("@_user_1")],
        )
        bot._on_raw_message(event_activate)
        plugin.received_messages.clear()

        _setup_merge_bot(bot)
        bot.client.im.v1.message.get.return_value = _make_get_response([
            _make_message_item("merge_forward", "Merged and Forwarded Message",
                               message_id="merge_msg_005"),
            _make_message_item("text", {"text": "@_user_1 帮我看看"},
                               message_id="om_1", sender_id="Alice",
                               create_time=_TS_A,
                               upper_message_id="merge_msg_005"),
        ])

        # 第一条：转发（无 @mention）
        event = _make_mock_event(
            "user1", "oc_group1", "",
            message_id="merge_msg_005",
            msg_type="merge_forward",
            chat_type="group",
            mentions=[],
        )
        bot._on_raw_message(event)

        # 第二条：留言（@机器人）
        event_comment = _make_mock_event(
            "user1", "oc_group1", "@_user_1 帮我看看",
            message_id="comment_005",
            chat_type="group",
            mentions=[_make_mention("@_user_1")],
        )
        bot._on_raw_message(event_comment)
        assert len(plugin.received_messages) == 1
        text = plugin.received_messages[0][2]
        # 转发内容被包含
        assert "<forwarded_messages>" in text
        # 留言中的 @mention 被剥离
        assert "帮我看看" in text

    def test_merge_forward_nested_indentation(self, bot_with_plugin):
        """嵌套合并转发：API 返回扁平列表，通过 upper_message_id 构建树"""
        bot, plugin = bot_with_plugin
        bot.on_message("user1", "chat1", "test")
        plugin.received_messages.clear()

        _setup_merge_bot(bot)
        # 单次 API 返回所有消息（扁平列表），通过 upper_message_id 区分层级
        bot.client.im.v1.message.get.return_value = _make_get_response([
            _make_message_item("merge_forward", "Merged and Forwarded Message",
                               message_id="merge_msg_006"),
            _make_message_item("text", {"text": "外层消息"}, message_id="om_1",
                               sender_id="Alice", create_time=_TS_A,
                               upper_message_id="merge_msg_006"),
            _make_message_item("merge_forward", "Merged and Forwarded Message",
                               message_id="om_nested",
                               upper_message_id="merge_msg_006"),
            # 以下子消息的 upper_message_id 指向嵌套的 merge_forward
            _make_message_item("text", {"text": "内层消息"}, message_id="om_i1",
                               sender_id="Bob", create_time=_TS_B,
                               upper_message_id="om_nested"),
        ])

        event = _make_mock_event(
            "user1", "chat1", "",
            message_id="merge_msg_006",
            msg_type="merge_forward",
        )
        bot._on_raw_message(event)
        bot._on_forward_timeout("user1", "chat1")
        text = plugin.received_messages[0][2]
        # 外层消息无缩进前缀
        assert "[forwarded messages]" in text
        assert "Alice:" in text
        assert "    外层消息" in text
        # 内层消息有 4 空格缩进（时间戳前）
        assert f"    [{_TS_B_STR}] Bob:" in text
        assert "        内层消息" in text  # depth=1 content → 8 空格
        # 只调用一次 API（不再递归调用）
        assert bot.client.im.v1.message.get.call_count == 1

    def test_merge_forward_depth_limit(self, bot_with_plugin):
        """递归深度超限时输出截断提示"""
        bot, plugin = bot_with_plugin
        bot.on_message("user1", "chat1", "test")
        plugin.received_messages.clear()

        _setup_merge_bot(bot)
        # 构造 11 层嵌套的 merge_forward（超过 _MERGE_FORWARD_MAX_DEPTH=10）
        items = [
            _make_message_item("merge_forward", "M", message_id="root"),
        ]
        for i in range(11):
            parent = "root" if i == 0 else f"mf_{i - 1}"
            items.append(_make_message_item(
                "merge_forward", "M", message_id=f"mf_{i}",
                upper_message_id=parent,
            ))
        bot.client.im.v1.message.get.return_value = _make_get_response(items)

        event = _make_mock_event(
            "user1", "chat1", "",
            message_id="root",
            msg_type="merge_forward",
        )
        bot._on_raw_message(event)
        bot._on_forward_timeout("user1", "chat1")
        text = plugin.received_messages[0][2]
        assert "已截断" in text

    def test_merge_forward_with_post_submessage(self, bot_with_plugin):
        """子消息中包含富文本(post)类型，正确提取文本"""
        bot, plugin = bot_with_plugin
        bot.on_message("user1", "chat1", "test")
        plugin.received_messages.clear()

        _setup_merge_bot(bot)
        bot.client.im.v1.message.get.return_value = _make_get_response([
            _make_message_item("merge_forward", "Merged and Forwarded Message",
                               message_id="merge_msg_008"),
            _make_message_item("text", {"text": "普通消息"}, message_id="om_1",
                               sender_id="Alice", create_time=_TS_A,
                               upper_message_id="merge_msg_008"),
            _make_message_item("post", {
                "content": [[{"tag": "text", "text": "富文本内容"}]]
            }, message_id="om_2", sender_id="Bob", create_time=_TS_B,
                               upper_message_id="merge_msg_008"),
        ])

        event = _make_mock_event(
            "user1", "chat1", "",
            message_id="merge_msg_008",
            msg_type="merge_forward",
        )
        bot._on_raw_message(event)
        bot._on_forward_timeout("user1", "chat1")
        text = plugin.received_messages[0][2]
        assert "Alice:" in text
        assert "普通消息" in text
        assert "Bob:" in text
        assert "富文本内容" in text

    def test_merge_forward_unknown_type_placeholder(self, bot_with_plugin):
        """未知消息类型显示占位提示"""
        bot, plugin = bot_with_plugin
        bot.on_message("user1", "chat1", "test")
        plugin.received_messages.clear()

        _setup_merge_bot(bot)
        bot.client.im.v1.message.get.return_value = _make_get_response([
            _make_message_item("merge_forward", "Merged and Forwarded Message",
                               message_id="merge_msg_009"),
            _make_message_item("text", {"text": "正常消息"}, message_id="om_1",
                               sender_id="Alice", create_time=_TS_A,
                               upper_message_id="merge_msg_009"),
            _make_message_item("hongbao", "{}", message_id="om_2",
                               sender_id="Bob", create_time=_TS_B,
                               upper_message_id="merge_msg_009"),
        ])

        event = _make_mock_event(
            "user1", "chat1", "",
            message_id="merge_msg_009",
            msg_type="merge_forward",
        )
        bot._on_raw_message(event)
        bot._on_forward_timeout("user1", "chat1")
        text = plugin.received_messages[0][2]
        assert "正常消息" in text
        assert "[hongbao 消息]" in text


class TestForwardAggregation:
    """转发消息 + 留言消息聚合测试"""

    def test_p2p_forward_then_comment_merged(self, bot_with_plugin):
        """私聊：转发消息 + 后续留言合并为一条指令"""
        bot, plugin = bot_with_plugin
        bot.on_message("user1", "chat1", "test")
        plugin.received_messages.clear()

        _setup_merge_bot(bot)
        bot.client.im.v1.message.get.return_value = _make_get_response([
            _make_message_item("merge_forward", "Merged and Forwarded Message",
                               message_id="fwd_001"),
            _make_message_item("text", {"text": "对话内容"}, message_id="om_1",
                               sender_id="Alice", create_time=_TS_A,
                               upper_message_id="fwd_001"),
        ])

        # 第一条：合并转发
        event_fwd = _make_mock_event(
            "user1", "chat1", "",
            message_id="fwd_001",
            msg_type="merge_forward",
        )
        bot._on_raw_message(event_fwd)
        # 尚未处理，暂存在缓冲区
        assert len(plugin.received_messages) == 0

        # 第二条：留言
        event_comment = _make_mock_event(
            "user1", "chat1", "帮我总结一下",
            message_id="comment_001",
        )
        bot._on_raw_message(event_comment)
        # 合并后作为一条消息处理
        assert len(plugin.received_messages) == 1
        text = plugin.received_messages[0][2]
        assert "<forwarded_messages>" in text
        assert "对话内容" in text
        assert "帮我总结一下" in text
        # 留言在转发内容之后
        fwd_end = text.index("</forwarded_messages>")
        comment_pos = text.index("帮我总结一下")
        assert comment_pos > fwd_end

    def test_p2p_forward_timeout_processes_alone(self, bot_with_plugin):
        """私聊：转发消息超时后单独处理"""
        bot, plugin = bot_with_plugin
        bot.on_message("user1", "chat1", "test")
        plugin.received_messages.clear()

        _setup_merge_bot(bot)
        bot.client.im.v1.message.get.return_value = _make_get_response([
            _make_message_item("merge_forward", "Merged and Forwarded Message",
                               message_id="fwd_002"),
            _make_message_item("text", {"text": "超时内容"}, message_id="om_1",
                               sender_id="Alice", create_time=_TS_A,
                               upper_message_id="fwd_002"),
        ])

        event_fwd = _make_mock_event(
            "user1", "chat1", "",
            message_id="fwd_002",
            msg_type="merge_forward",
        )
        bot._on_raw_message(event_fwd)
        assert len(plugin.received_messages) == 0

        # 手动触发超时回调
        bot._on_forward_timeout("user1", "chat1")
        assert len(plugin.received_messages) == 1
        text = plugin.received_messages[0][2]
        assert "<forwarded_messages>" in text
        assert "超时内容" in text

    def test_group_forward_then_at_comment_merged(self, bot_with_plugin):
        """群聊(@唤醒)：转发消息(无@) + 留言(@机器人) 合并处理"""
        bot, plugin = bot_with_plugin
        # 先在群聊中激活插件
        event_activate = _make_mock_event(
            "user1", "oc_group1", "@_user_1 test",
            message_id="grp_activate",
            chat_type="group",
            mentions=[_make_mention("@_user_1")],
        )
        bot._on_raw_message(event_activate)
        plugin.received_messages.clear()

        _setup_merge_bot(bot)
        bot.client.im.v1.message.get.return_value = _make_get_response([
            _make_message_item("merge_forward", "Merged and Forwarded Message",
                               message_id="fwd_grp_001"),
            _make_message_item("text", {"text": "群聊对话"}, message_id="om_1",
                               sender_id="Alice", create_time=_TS_A,
                               upper_message_id="fwd_grp_001"),
        ])

        # 第一条：合并转发（无 @mention）
        event_fwd = _make_mock_event(
            "user1", "oc_group1", "",
            message_id="fwd_grp_001",
            msg_type="merge_forward",
            chat_type="group",
            mentions=[],
        )
        bot._on_raw_message(event_fwd)
        assert len(plugin.received_messages) == 0

        # 第二条：留言（@机器人）
        event_comment = _make_mock_event(
            "user1", "oc_group1", "@_user_1 帮我分析",
            message_id="comment_grp_001",
            chat_type="group",
            mentions=[_make_mention("@_user_1")],
        )
        bot._on_raw_message(event_comment)
        assert len(plugin.received_messages) == 1
        text = plugin.received_messages[0][2]
        assert "<forwarded_messages>" in text
        assert "群聊对话" in text
        assert "帮我分析" in text

    def test_group_forward_timeout_discarded(self, bot_with_plugin):
        """群聊(非唤醒模式)：转发消息超时后静默丢弃"""
        bot, plugin = bot_with_plugin

        _setup_merge_bot(bot)
        bot.client.im.v1.message.get.return_value = _make_get_response([
            _make_message_item("merge_forward", "Merged and Forwarded Message",
                               message_id="fwd_grp_002"),
            _make_message_item("text", {"text": "会被丢弃"}, message_id="om_1",
                               sender_id="Alice", create_time=_TS_A,
                               upper_message_id="fwd_grp_002"),
        ])

        event_fwd = _make_mock_event(
            "user1", "oc_group1", "",
            message_id="fwd_grp_002",
            msg_type="merge_forward",
            chat_type="group",
            mentions=[],
        )
        bot._on_raw_message(event_fwd)

        # 超时触发 — 群聊非唤醒模式，应丢弃
        bot._on_forward_timeout("user1", "oc_group1")
        assert len(plugin.received_messages) == 0

    def test_group_wake_mode_forward_timeout_processes(self, bot_with_plugin):
        """群聊(唤醒模式)：转发消息超时后单独处理"""
        bot, plugin = bot_with_plugin
        bot._wake_mode_groups.add("oc_group1")
        bot.on_message("user1", "oc_group1", "test")
        plugin.received_messages.clear()

        _setup_merge_bot(bot)
        bot.client.im.v1.message.get.return_value = _make_get_response([
            _make_message_item("merge_forward", "Merged and Forwarded Message",
                               message_id="fwd_grp_003"),
            _make_message_item("text", {"text": "唤醒模式内容"}, message_id="om_1",
                               sender_id="Alice", create_time=_TS_A,
                               upper_message_id="fwd_grp_003"),
        ])

        event_fwd = _make_mock_event(
            "user1", "oc_group1", "",
            message_id="fwd_grp_003",
            msg_type="merge_forward",
            chat_type="group",
            mentions=[],
        )
        bot._on_raw_message(event_fwd)
        assert len(plugin.received_messages) == 0

        bot._on_forward_timeout("user1", "oc_group1")
        assert len(plugin.received_messages) == 1
        text = plugin.received_messages[0][2]
        assert "唤醒模式内容" in text

    def test_different_user_no_merge(self, bot_with_plugin):
        """群聊：不同用户的消息不会误合并"""
        bot, plugin = bot_with_plugin
        bot._wake_mode_groups.add("oc_group1")
        bot.on_message("user1", "oc_group1", "test")
        bot.on_message("user2", "oc_group1", "test")
        plugin.received_messages.clear()

        _setup_merge_bot(bot)
        bot.client.im.v1.message.get.return_value = _make_get_response([
            _make_message_item("merge_forward", "Merged and Forwarded Message",
                               message_id="fwd_cross_001"),
            _make_message_item("text", {"text": "A的转发"}, message_id="om_1",
                               sender_id="Alice", create_time=_TS_A,
                               upper_message_id="fwd_cross_001"),
        ])

        # user1 转发
        event_fwd = _make_mock_event(
            "user1", "oc_group1", "",
            message_id="fwd_cross_001",
            msg_type="merge_forward",
            chat_type="group",
            mentions=[],
        )
        bot._on_raw_message(event_fwd)

        # user2 发消息 — 不应与 user1 的转发合并
        event_other = _make_mock_event(
            "user2", "oc_group1", "我是另一个人",
            message_id="other_msg_001",
            chat_type="group",
        )
        bot._on_raw_message(event_other)
        # user2 的消息不应包含转发内容
        user2_msgs = [m for m in plugin.received_messages if m[0] == "user2"]
        assert len(user2_msgs) == 1
        assert "<forwarded_messages>" not in user2_msgs[0][2]

        # user1 的转发仍在缓冲区，超时后才处理
        bot._on_forward_timeout("user1", "oc_group1")
        user1_msgs = [m for m in plugin.received_messages if m[0] == "user1"]
        assert len(user1_msgs) == 1
        assert "A的转发" in user1_msgs[0][2]

    def test_forward_api_failure_no_buffer(self, bot_with_plugin):
        """转发内容提取失败时不暂存"""
        bot, plugin = bot_with_plugin

        _setup_merge_bot(bot)
        bot.client.im.v1.message.get.return_value = _make_get_response(
            [], success=False,
        )

        event_fwd = _make_mock_event(
            "user1", "chat1", "",
            message_id="fwd_fail_001",
            msg_type="merge_forward",
        )
        bot._on_raw_message(event_fwd)
        # 提取失败，回复提示
        bot.reply.assert_called_once()
        assert "未包含可识别的文本" in bot.reply.call_args[0][1]
        # 缓冲区应为空
        assert ("user1", "chat1") not in bot._pending_forwards

    def test_new_forward_cancels_old_buffer(self, bot_with_plugin):
        """同一用户连续两次转发，新的覆盖旧的"""
        bot, plugin = bot_with_plugin
        bot.on_message("user1", "chat1", "test")
        plugin.received_messages.clear()

        _setup_merge_bot(bot)

        # 第一次转发
        bot.client.im.v1.message.get.return_value = _make_get_response([
            _make_message_item("merge_forward", "Merged and Forwarded Message",
                               message_id="fwd_old"),
            _make_message_item("text", {"text": "旧转发"}, message_id="om_1",
                               sender_id="Alice", create_time=_TS_A,
                               upper_message_id="fwd_old"),
        ])
        event1 = _make_mock_event(
            "user1", "chat1", "",
            message_id="fwd_old",
            msg_type="merge_forward",
        )
        bot._on_raw_message(event1)

        # 第二次转发（覆盖）
        bot.client.im.v1.message.get.return_value = _make_get_response([
            _make_message_item("merge_forward", "Merged and Forwarded Message",
                               message_id="fwd_new"),
            _make_message_item("text", {"text": "新转发"}, message_id="om_2",
                               sender_id="Bob", create_time=_TS_B,
                               upper_message_id="fwd_new"),
        ])
        event2 = _make_mock_event(
            "user1", "chat1", "",
            message_id="fwd_new",
            msg_type="merge_forward",
        )
        bot._on_raw_message(event2)

        # 发送留言
        event_comment = _make_mock_event(
            "user1", "chat1", "看最新的",
            message_id="comment_new",
        )
        bot._on_raw_message(event_comment)
        assert len(plugin.received_messages) == 1
        text = plugin.received_messages[0][2]
        # 应合并新转发的内容，不含旧转发
        assert "新转发" in text
        assert "旧转发" not in text
        assert "看最新的" in text

    def test_forward_timeout_real_timer(self, bot_with_plugin):
        """通过真实定时器验证超时后转发消息被处理（集成测试）"""
        import core.feishu_bot as bot_module
        original_timeout = bot_module._FORWARD_AGGREGATE_TIMEOUT
        # 缩短超时到 0.1 秒以加速测试
        bot_module._FORWARD_AGGREGATE_TIMEOUT = 0.1
        try:
            bot, plugin = bot_with_plugin
            bot.on_message("user1", "chat1", "test")
            plugin.received_messages.clear()

            _setup_merge_bot(bot)
            bot.client.im.v1.message.get.return_value = _make_get_response([
                _make_message_item("merge_forward", "Merged and Forwarded Message",
                                   message_id="fwd_timer"),
                _make_message_item("text", {"text": "定时器内容"}, message_id="om_1",
                                   sender_id="Alice", create_time=_TS_A,
                                   upper_message_id="fwd_timer"),
            ])

            event_fwd = _make_mock_event(
                "user1", "chat1", "",
                message_id="fwd_timer",
                msg_type="merge_forward",
            )
            bot._on_raw_message(event_fwd)
            assert len(plugin.received_messages) == 0

            # 等待定时器触发
            time.sleep(0.3)
            assert len(plugin.received_messages) == 1
            text = plugin.received_messages[0][2]
            assert "定时器内容" in text
        finally:
            bot_module._FORWARD_AGGREGATE_TIMEOUT = original_timeout
