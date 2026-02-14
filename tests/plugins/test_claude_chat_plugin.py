"""
Claude 对话插件单元测试
"""

import json
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


def _mock_stream_response(text="ok"):
    """构造一个流式 SSE 响应 mock"""
    resp = Mock()
    resp.status_code = 200
    # 模拟 SSE 流：content_block_delta 事件
    lines = [
        f'data: {json.dumps({"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}})}',
        "data: [DONE]",
    ]
    resp.iter_lines = Mock(return_value=iter(lines))
    resp.close = Mock()
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
        """正常流式对话：发送消息并通过 patch 更新回复"""
        mock_post.return_value = _mock_stream_response("你好！有什么可以帮你的？")

        plugin.handle_message("u1", "c1", "Claude")
        plugin.handle_message("u1", "c1", "你好")

        # 验证发送了占位消息
        plugin.bot.send_message_get_id.assert_called()
        # 验证历史记录
        state = plugin._get_state("u1")
        assert len(state["history"]) == 2
        assert state["history"][0] == {"role": "user", "content": "你好"}
        assert state["history"][1]["role"] == "assistant"
        assert state["history"][1]["content"] == "你好！有什么可以帮你的？"

    @patch("plugins.claude_chat.claude_chat_plugin.requests.post")
    def test_multi_turn_conversation(self, mock_post, plugin):
        """多轮对话历史正确累积"""
        mock_post.side_effect = lambda *a, **kw: _mock_stream_response("回复")

        plugin.handle_message("u1", "c1", "Claude")
        plugin.handle_message("u1", "c1", "问题1")
        plugin.handle_message("u1", "c1", "问题2")

        state = plugin._get_state("u1")
        assert len(state["history"]) == 4  # 2 轮 x 2 条

    @patch("plugins.claude_chat.claude_chat_plugin.requests.post")
    def test_api_payload_has_stream_flag(self, mock_post, plugin):
        """验证请求中包含 stream=true"""
        captured = {}

        def capture_call(*args, **kwargs):
            import copy
            captured["json"] = copy.deepcopy(kwargs.get("json", {}))
            return _mock_stream_response()

        mock_post.side_effect = capture_call

        plugin.handle_message("u1", "c1", "Claude")
        plugin.handle_message("u1", "c1", "测试消息")

        payload = captured["json"]
        assert payload["model"] == "claude-opus-4-6"
        assert payload["stream"] is True
        assert payload["messages"] == [{"role": "user", "content": "测试消息"}]
        assert "system" not in payload

    @patch("plugins.claude_chat.claude_chat_plugin.requests.post")
    def test_system_prompt_included(self, mock_post, plugin):
        """配置了 system_prompt 时请求中包含 system 字段"""
        plugin._config["system_prompt"] = "你是一个有帮助的助手"
        mock_post.return_value = _mock_stream_response()

        plugin.handle_message("u1", "c1", "Claude")
        plugin.handle_message("u1", "c1", "你好")

        payload = mock_post.call_args.kwargs["json"]
        assert payload["system"] == "你是一个有帮助的助手"

    @patch("plugins.claude_chat.claude_chat_plugin.requests.post")
    def test_patch_message_called(self, mock_post, plugin):
        """流式响应结束后调用 patch_message 更新最终内容"""
        mock_post.return_value = _mock_stream_response("最终回复")

        plugin.handle_message("u1", "c1", "Claude")
        plugin.handle_message("u1", "c1", "你好")

        # patch_message 至少被调用一次（最终更新）
        plugin.bot.patch_message.assert_called()

    @patch("plugins.claude_chat.claude_chat_plugin.requests.post")
    def test_placeholder_message_sent(self, mock_post, plugin):
        """对话时先发送占位消息"""
        mock_post.return_value = _mock_stream_response()

        plugin.handle_message("u1", "c1", "Claude")
        plugin.handle_message("u1", "c1", "你好")

        # send_message_get_id 被调用，内容是卡片格式（interactive）
        call_args = plugin.bot.send_message_get_id.call_args
        assert call_args[0][1] == "interactive"
        card = json.loads(call_args[0][2])
        # 卡片 markdown 元素中包含"思考"
        assert "思考" in card["elements"][0]["content"]


# ---- 清空对话测试 ----


class TestClearHistory:
    """清空对话测试"""

    @patch("plugins.claude_chat.claude_chat_plugin.requests.post")
    def test_clear_history(self, mock_post, plugin):
        """清空对话指令清除历史"""
        mock_post.return_value = _mock_stream_response()

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
        """网络错误时通过 patch 更新错误提示且不污染历史"""
        import requests as req
        mock_post.side_effect = req.ConnectionError("连接失败")

        plugin.handle_message("u1", "c1", "Claude")
        plugin.handle_message("u1", "c1", "你好")

        # 错误通过 patch_message 更新到占位消息（卡片格式）
        plugin.bot.patch_message.assert_called()
        last_card = json.loads(plugin.bot.patch_message.call_args[0][1])
        assert "无法回复" in last_card["elements"][0]["content"]
        # 失败的用户消息应被回滚
        assert len(plugin._get_state("u1")["history"]) == 0

    @patch("plugins.claude_chat.claude_chat_plugin.requests.post")
    def test_non_200_status(self, mock_post, plugin):
        """API 返回非 200 时通过 patch 更新错误提示"""
        resp = Mock()
        resp.status_code = 429
        resp.text = "rate limited"
        mock_post.return_value = resp

        plugin.handle_message("u1", "c1", "Claude")
        plugin.handle_message("u1", "c1", "你好")

        plugin.bot.patch_message.assert_called()
        last_card = json.loads(plugin.bot.patch_message.call_args[0][1])
        assert "无法回复" in last_card["elements"][0]["content"]

    @patch("plugins.claude_chat.claude_chat_plugin.requests.post")
    def test_empty_stream(self, mock_post, plugin):
        """流式响应无文本内容时友好提示"""
        resp = Mock()
        resp.status_code = 200
        resp.iter_lines = Mock(return_value=iter(["data: [DONE]"]))
        resp.close = Mock()
        mock_post.return_value = resp

        plugin.handle_message("u1", "c1", "Claude")
        plugin.handle_message("u1", "c1", "你好")

        plugin.bot.patch_message.assert_called()
        last_card = json.loads(plugin.bot.patch_message.call_args[0][1])
        assert "无法回复" in last_card["elements"][0]["content"]

    @patch("plugins.claude_chat.claude_chat_plugin.requests.post")
    def test_fallback_when_no_message_id(self, mock_post, plugin):
        """send_message_get_id 返回 None 时降级为直接发送"""
        plugin.bot.send_message_get_id.return_value = None
        mock_post.return_value = _mock_stream_response("降级回复")

        plugin.handle_message("u1", "c1", "Claude")
        plugin.bot.reply.reset_mock()
        plugin.handle_message("u1", "c1", "你好")

        # 降级模式下通过 reply 发送
        plugin.bot.reply.assert_called_once_with("c1", "降级回复")


# ---- 多用户隔离测试 ----


class TestUserIsolation:
    """多用户隔离测试"""

    @patch("plugins.claude_chat.claude_chat_plugin.requests.post")
    def test_users_isolated(self, mock_post, plugin):
        """不同用户的对话历史互相隔离"""
        # 每次调用返回新的 mock（迭代器不可复用）
        mock_post.side_effect = lambda *a, **kw: _mock_stream_response()

        plugin.handle_message("u1", "c1", "Claude")
        plugin.handle_message("u2", "c2", "Claude")

        plugin.handle_message("u1", "c1", "用户1的消息")
        plugin.handle_message("u2", "c2", "用户2的消息")

        state1 = plugin._get_state("u1")
        state2 = plugin._get_state("u2")
        assert state1["history"][0]["content"] == "用户1的消息"
        assert state2["history"][0]["content"] == "用户2的消息"


# ---- SSE 解析测试 ----


class TestExtractDeltaText:
    """SSE 事件解析测试"""

    def test_text_delta(self, plugin):
        """正确提取 text_delta 事件中的文本"""
        data = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hello"}}
        assert plugin._extract_delta_text(data) == "hello"

    def test_non_delta_event(self, plugin):
        """非 content_block_delta 事件返回空"""
        data = {"type": "message_start"}
        assert plugin._extract_delta_text(data) == ""

    def test_non_text_delta(self, plugin):
        """非 text_delta 类型返回空"""
        data = {"type": "content_block_delta", "delta": {"type": "input_json_delta"}}
        assert plugin._extract_delta_text(data) == ""
