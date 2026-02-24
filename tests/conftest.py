"""
测试框架共享夹具
提供 mock 化的 bot 和 plugin 实例，隔离 lark_oapi 外部依赖。
"""

from unittest.mock import MagicMock, patch

import pytest


# ---- Mock lark_oapi ----
# 在导入任何项目模块之前，先 mock 掉 lark_oapi，
# 这样测试无需安装飞书 SDK 也能运行。

_lark_mock = MagicMock()

# P2CardActionTriggerResponse 需要可实例化
_lark_mock.event.callback.model.p2_card_action_trigger.P2CardActionTriggerResponse = (
    type("P2CardActionTriggerResponse", (), {})
)

# lark_oapi.api.im.v1 使用了 wildcard import，
# 需要显式声明 __all__ 让 MagicMock 正确导出这些名称。
_im_v1_mock = MagicMock()
_im_v1_mock.__all__ = [
    "P2ImMessageReceiveV1",
    "CreateMessageRequest",
    "CreateMessageRequestBody",
    "GetMessageResourceRequest",
    "PatchMessageRequest",
    "PatchMessageRequestBody",
]

_modules = {
    "lark_oapi": _lark_mock,
    "lark_oapi.api.im.v1": _im_v1_mock,
    "lark_oapi.api.application.v6.model.p2_application_bot_menu_v6": (
        _lark_mock.api.application.v6.model.p2_application_bot_menu_v6
    ),
    "lark_oapi.event.callback.model.p2_card_action_trigger": (
        _lark_mock.event.callback.model.p2_card_action_trigger
    ),
}

_patcher = patch.dict("sys.modules", _modules)
_patcher.start()

# 现在可以安全导入项目模块了
from core.hub_bot import HubBot  # noqa: E402
from core.plugin import Plugin  # noqa: E402


# ---- Fixtures ----


class StubPlugin(Plugin):
    """用于测试的最小插件实现"""

    def __init__(self, name="测试插件", keyword="test", description="测试用"):
        super().__init__()
        self._name = name
        self._keyword = keyword
        self._description = description
        self.received_messages: list[tuple] = []
        self.received_file_messages: list[tuple] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def keyword(self) -> str:
        return self._keyword

    @property
    def description(self) -> str:
        return self._description

    def handle_message(self, user_id: str, chat_id: str, text: str) -> None:
        self.received_messages.append((user_id, chat_id, text))

    def handle_file_message(
        self, user_id: str, chat_id: str, message_id: str,
        file_key: str, file_name: str
    ) -> None:
        self.received_file_messages.append(
            (user_id, chat_id, message_id, file_key, file_name)
        )


@pytest.fixture
def mock_bot():
    """返回一个 reply/reply_card 被 mock 的 HubBot 实例"""
    bot = HubBot("fake_id", "fake_secret")
    bot.reply = MagicMock()
    bot.reply_card = MagicMock()
    bot.send_message = MagicMock()
    bot.send_message_get_id = MagicMock(return_value="mock_msg_id")
    bot.patch_message = MagicMock()
    return bot


@pytest.fixture
def stub_plugin():
    """返回一个干净的 StubPlugin 实例"""
    return StubPlugin()


@pytest.fixture
def bot_with_plugin(mock_bot, stub_plugin):
    """返回已注册一个 StubPlugin 的 HubBot"""
    mock_bot.register(stub_plugin)
    return mock_bot, stub_plugin
