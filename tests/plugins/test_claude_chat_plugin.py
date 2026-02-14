"""
Claude 对话插件单元测试
"""

from unittest.mock import MagicMock, Mock, patch

import pytest

from plugins.claude_chat.claude_chat_plugin import (
    ClaudeChatPlugin,
    DEFAULT_MAX_HISTORY,
)


# ---- Fixtures ----


@pytest.fixture
def plugin(mock_bot):
    """返回已注册 mock bot 的 ClaudeChatPlugin，并注入测试配置"""
    p = ClaudeChatPlugin()
    p.on_register(mock_bot)
    # 直接注入配置，跳过 config.yaml 读取
    p._config = {
        "api_url": "https://api.test.com/v1/messages",
        "api_key": "sk-test-key",
        "model": "claude-opus-4-6",
        "max_history": DEFAULT_MAX_HISTORY,
        "max_tokens": 4096,
        "system_prompt": "",
    }
    return p


def _mock_api_response(text="ok"):
    """构造一个成功的 API 响应 mock"""
    resp = Mock()
    resp.status_code = 200
    resp.json.return_value = {
        "content": [{"type": "text", "text": text}],
    }
    return resp


# ---- 元信息测试 ----


class TestPluginProperties:
    """插件元信息测试"""

    def test_name(self, plugin):
        """插件名称正确"""
        assert plugin.name == "Claude 对话"

    def test_keyword(self, plugin):
        """插件关键词正确"""
        assert plugin.keyword == "Claude"

    def test_description(self, plugin):
        """插件描述非空"""
        assert plugin.description


# ---- 激活与退出测试 ----


class TestActivation:
    """插件激活与退出测试"""

    def test_keyword_activates(self, plugin):
        """发送关键词激活插件"""
        plugin.handle_message("u1", "c1", "Claude")
        assert plugin.is_user_active("u1") is True
        msg = plugin.bot.reply.call_args[0][1]
        assert "已激活" in msg

    def test_keyword_shows_history_count(self, plugin):
        """有历史对话时激活显示轮数"""
        state = plugin._get_state("u1")
        state["history"] = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！"},
        ]
        plugin.handle_message("u1", "c1", "Claude")
        msg = plugin.bot.reply.call_args[0][1]
        assert "1 轮" in msg

    def test_deactivate_clears_state(self, plugin):
        """deactivate 清理用户全部状态"""
        plugin.handle_message("u1", "c1", "Claude")
        plugin.deactivate_user("u1")
        assert "u1" not in plugin.user_states

    def test_inactive_user_returns_false(self, plugin):
        """未激活用户 is_user_active 返回 False"""
        assert plugin.is_user_active("unknown") is False


# ---- 对话流程测试 ----


class TestConversation:
    """对话流程测试"""

    @patch("plugins.claude_chat.claude_chat_plugin.requests.post")
    def test_normal_conversation(self, mock_post, plugin):
        """正常对话流程：发送消息并收到回复"""
        mock_post.return_value = _mock_api_response("你好！有什么可以帮你的？")

        plugin.handle_message("u1", "c1", "Claude")
        plugin.bot.reply.reset_mock()

        plugin.handle_message("u1", "c1", "你好")

        plugin.bot.reply.assert_called_once_with("c1", "你好！有什么可以帮你的？")
        state = plugin._get_state("u1")
        assert len(state["history"]) == 2
        assert state["history"][0] == {"role": "user", "content": "你好"}
        assert state["history"][1]["role"] == "assistant"

    @patch("plugins.claude_chat.claude_chat_plugin.requests.post")
    def test_multi_turn_conversation(self, mock_post, plugin):
        """多轮对话历史正确累积"""
        mock_post.return_value = _mock_api_response("回复1")

        plugin.handle_message("u1", "c1", "Claude")
        plugin.handle_message("u1", "c1", "问题1")

        mock_post.return_value = _mock_api_response("回复2")
        plugin.handle_message("u1", "c1", "问题2")

        state = plugin._get_state("u1")
        assert len(state["history"]) == 4  # 2 轮 x 2 条

    @patch("plugins.claude_chat.claude_chat_plugin.requests.post")
    def test_api_payload_format(self, mock_post, plugin):
        """验证发送给 API 的请求格式正确"""
        # 用 side_effect 在调用时捕获 payload 快照（避免列表引用被后续修改）
        captured = {}

        def capture_call(*args, **kwargs):
            import copy
            captured["json"] = copy.deepcopy(kwargs.get("json", {}))
            return _mock_api_response()

        mock_post.side_effect = capture_call

        plugin.handle_message("u1", "c1", "Claude")
        plugin.handle_message("u1", "c1", "测试消息")

        payload = captured["json"]
        assert payload["model"] == "claude-opus-4-6"
        assert payload["max_tokens"] == 4096
        assert payload["messages"] == [{"role": "user", "content": "测试消息"}]
        assert "system" not in payload

    @patch("plugins.claude_chat.claude_chat_plugin.requests.post")
    def test_system_prompt_included(self, mock_post, plugin):
        """配置了 system_prompt 时请求中包含 system 字段"""
        plugin._config["system_prompt"] = "你是一个有帮助的助手"
        mock_post.return_value = _mock_api_response()

        plugin.handle_message("u1", "c1", "Claude")
        plugin.handle_message("u1", "c1", "你好")

        payload = mock_post.call_args.kwargs["json"]
        assert payload["system"] == "你是一个有帮助的助手"


# ---- 清空对话测试 ----


class TestClearHistory:
    """清空对话测试"""

    @patch("plugins.claude_chat.claude_chat_plugin.requests.post")
    def test_clear_history(self, mock_post, plugin):
        """清空对话指令清除历史"""
        mock_post.return_value = _mock_api_response()

        plugin.handle_message("u1", "c1", "Claude")
        plugin.handle_message("u1", "c1", "你好")

        plugin.bot.reply.reset_mock()
        plugin.handle_message("u1", "c1", "清空对话")

        msg = plugin.bot.reply.call_args[0][1]
        assert "已清空" in msg
        assert len(plugin._get_state("u1")["history"]) == 0


# ---- 历史裁剪测试 ----


class TestTrimHistory:
    """对话历史裁剪测试"""

    def test_within_limit_no_trim(self, plugin):
        """历史未超限时不裁剪"""
        history = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
        ]
        result = plugin._trim_history(history)
        assert len(result) == 2

    def test_exceeds_limit_trimmed(self, plugin):
        """历史超限时裁剪到 max_history"""
        plugin._config["max_history"] = 4
        history = []
        for i in range(10):
            role = "user" if i % 2 == 0 else "assistant"
            history.append({"role": role, "content": f"msg{i}"})
        result = plugin._trim_history(history)
        assert len(result) <= 4

    def test_trimmed_starts_with_user(self, plugin):
        """裁剪后第一条消息必须是 user 角色"""
        plugin._config["max_history"] = 3
        history = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2"},
            {"role": "user", "content": "q3"},
        ]
        result = plugin._trim_history(history)
        assert result[0]["role"] == "user"


# ---- 错误处理测试 ----


class TestErrorHandling:
    """API 错误处理测试"""

    @patch("plugins.claude_chat.claude_chat_plugin.requests.post")
    def test_network_error(self, mock_post, plugin):
        """网络错误时友好提示且不污染历史"""
        import requests as req
        mock_post.side_effect = req.ConnectionError("连接失败")

        plugin.handle_message("u1", "c1", "Claude")
        plugin.bot.reply.reset_mock()
        plugin.handle_message("u1", "c1", "你好")

        msg = plugin.bot.reply.call_args[0][1]
        assert "无法回复" in msg
        # 失败的用户消息应被回滚
        assert len(plugin._get_state("u1")["history"]) == 0

    @patch("plugins.claude_chat.claude_chat_plugin.requests.post")
    def test_non_200_status(self, mock_post, plugin):
        """API 返回非 200 时友好提示"""
        resp = Mock()
        resp.status_code = 429
        resp.text = "rate limited"
        mock_post.return_value = resp

        plugin.handle_message("u1", "c1", "Claude")
        plugin.bot.reply.reset_mock()
        plugin.handle_message("u1", "c1", "你好")

        msg = plugin.bot.reply.call_args[0][1]
        assert "无法回复" in msg

    @patch("plugins.claude_chat.claude_chat_plugin.requests.post")
    def test_empty_content(self, mock_post, plugin):
        """API 返回空内容时友好提示"""
        resp = Mock()
        resp.status_code = 200
        resp.json.return_value = {"content": []}
        mock_post.return_value = resp

        plugin.handle_message("u1", "c1", "Claude")
        plugin.bot.reply.reset_mock()
        plugin.handle_message("u1", "c1", "你好")

        msg = plugin.bot.reply.call_args[0][1]
        assert "无法回复" in msg


# ---- 多用户隔离测试 ----


class TestUserIsolation:
    """多用户隔离测试"""

    @patch("plugins.claude_chat.claude_chat_plugin.requests.post")
    def test_users_isolated(self, mock_post, plugin):
        """不同用户的对话历史互相隔离"""
        mock_post.return_value = _mock_api_response()

        plugin.handle_message("u1", "c1", "Claude")
        plugin.handle_message("u2", "c2", "Claude")

        plugin.handle_message("u1", "c1", "用户1的消息")
        plugin.handle_message("u2", "c2", "用户2的消息")

        state1 = plugin._get_state("u1")
        state2 = plugin._get_state("u2")
        assert state1["history"][0]["content"] == "用户1的消息"
        assert state2["history"][0]["content"] == "用户2的消息"
