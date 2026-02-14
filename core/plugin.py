"""
插件抽象基类
每个功能插件需继承此类，实现 name / keyword / description 属性和 handle_message 方法。
"""

from abc import ABC, abstractmethod
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from core.hub_bot import HubBot
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTriggerResponse,
    )


class Plugin(ABC):

    def __init__(self):
        self.bot: Optional["HubBot"] = None

    # ---- 元信息（子类必须实现） ----

    @property
    @abstractmethod
    def name(self) -> str:
        """显示名称，如 '石头剪刀布'"""
        ...

    @property
    @abstractmethod
    def keyword(self) -> str:
        """触发关键词，如 '石头剪刀布'"""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """一句话描述，用于菜单展示"""
        ...

    # ---- 生命周期 ----

    def on_register(self, bot: "HubBot") -> None:
        """注册时由 HubBot 调用，注入 bot 引用"""
        self.bot = bot

    # ---- 消息处理（子类实现） ----

    @abstractmethod
    def handle_message(self, user_id: str, chat_id: str, text: str) -> None:
        """处理路由过来的文本消息"""
        ...

    def handle_card_action(
        self, user_id: str, chat_id: str, message_id: str, action_value: dict
    ) -> "P2CardActionTriggerResponse":
        """处理卡片按钮点击，默认无操作，有卡片交互的插件需覆写"""
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )
        return P2CardActionTriggerResponse()

    # ---- 会话状态 ----

    def is_user_active(self, user_id: str) -> bool:
        """用户是否在本插件的活跃会话中，默认 False（无状态插件）"""
        return False

    def deactivate_user(self, user_id: str) -> None:
        """清理用户会话状态，默认无操作"""
        pass
