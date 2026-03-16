"""
FeishuBot API 方法异常降级测试

验证当飞书 SDK 内部抛出异常（如 JSONDecodeError）时，
各 API 方法安全降级返回，不向上层传播异常。
"""

import json

import pytest

from core.hub_bot import HubBot


@pytest.fixture
def bot():
    """返回一个不 mock API 方法的 HubBot 实例

    与 conftest.mock_bot 不同，这里保留 send_message / send_message_get_id
    等方法的真实实现，只让 SDK 最底层的 client 调用可被控制。
    """
    return HubBot("fake_id", "fake_secret")


# SDK 可能抛出的典型异常
SDK_EXCEPTIONS = [
    json.JSONDecodeError("Expecting value", "", 0),
    ConnectionError("连接被重置"),
    TimeoutError("请求超时"),
]


class TestSendMessageException:
    """send_message SDK 异常降级"""

    @pytest.mark.parametrize("exc", SDK_EXCEPTIONS)
    def test_sdk_exception_does_not_propagate(self, bot, exc):
        bot.client.im.v1.message.create.side_effect = exc
        # 不抛异常即为通过
        bot.send_message("chat_id", "text", '{"text":"hi"}')


class TestSendMessageGetIdException:
    """send_message_get_id SDK 异常降级"""

    @pytest.mark.parametrize("exc", SDK_EXCEPTIONS)
    def test_sdk_exception_returns_none(self, bot, exc):
        bot.client.im.v1.message.create.side_effect = exc
        result = bot.send_message_get_id("chat_id", "interactive", "{}")
        assert result is None


class TestPatchMessageException:
    """patch_message SDK 异常降级"""

    @pytest.mark.parametrize("exc", SDK_EXCEPTIONS)
    def test_sdk_exception_returns_false(self, bot, exc):
        bot.client.im.v1.message.patch.side_effect = exc
        result = bot.patch_message("msg_id", "{}")
        assert result is False


class TestUrgentMessageException:
    """urgent_message SDK 异常降级"""

    @pytest.mark.parametrize("exc", SDK_EXCEPTIONS)
    def test_sdk_exception_returns_false(self, bot, exc):
        bot.client.im.v1.message.urgent_app.side_effect = exc
        result = bot.urgent_message("msg_id", ["user_id"])
        assert result is False


class TestReplyToMessageException:
    """reply_to_message SDK 异常降级"""

    @pytest.mark.parametrize("exc", SDK_EXCEPTIONS)
    def test_sdk_exception_returns_none(self, bot, exc):
        bot.client.im.v1.message.reply.side_effect = exc
        result = bot.reply_to_message("parent_id", "interactive", "{}")
        assert result is None


class TestDeleteMessageException:
    """delete_message SDK 异常降级"""

    @pytest.mark.parametrize("exc", SDK_EXCEPTIONS)
    def test_sdk_exception_returns_false(self, bot, exc):
        bot.client.im.v1.message.delete.side_effect = exc
        result = bot.delete_message("msg_id")
        assert result is False


class TestDownloadFileException:
    """download_file SDK 异常降级 — 保持 RuntimeError 契约"""

    @pytest.mark.parametrize("exc", SDK_EXCEPTIONS)
    def test_sdk_exception_raises_runtime_error(self, bot, exc):
        bot.client.im.v1.message_resource.get.side_effect = exc
        with pytest.raises(RuntimeError, match="SDK异常"):
            bot.download_file("msg_id", "file_key")
