"""
飞书机器人基类
封装了连接、消息收发等通用逻辑，子类只需实现 on_message / on_card_action 处理业务。
"""

import json
from abc import ABC, abstractmethod
from typing import Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import *
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
    CallBackCard,
    CallBackToast,
)


class FeishuBot(ABC):
    """飞书机器人基类

    关键部分：
    1. 连接层: __init__ 中创建 lark.Client 和事件回调，start() 启动 WebSocket
    2. 消息收发层: send_message 泛化发送，reply / reply_card 为便捷方法
    3. 业务逻辑层: 子类实现 on_message 和 on_card_action
    """

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret

        self.client = lark.Client.builder() \
            .app_id(app_id) \
            .app_secret(app_secret) \
            .log_level(lark.LogLevel.INFO) \
            .build()

        self._event_handler = lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(self._on_raw_message) \
            .register_p2_card_action_trigger(self._on_raw_card_action) \
            .build()

    # ---- 消息收发层 ----

    def _on_raw_message(self, data: P2ImMessageReceiveV1) -> None:
        """解析原始消息，提取关键字段后交给子类处理"""
        message = data.event.message
        try:
            content_dict = json.loads(message.content)
            text = content_dict.get("text", "").strip()
        except (json.JSONDecodeError, AttributeError):
            return

        sender_id = data.event.sender.sender_id.user_id
        chat_id = message.chat_id
        print(f"[INFO] 收到消息: user={sender_id}, text={text}")
        self.on_message(sender_id, chat_id, text)

    def _on_raw_card_action(self, data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        """解析卡片按钮点击事件，交给子类处理"""
        user_id = data.event.operator.user_id
        chat_id = data.event.context.open_chat_id
        message_id = data.event.context.open_message_id
        action_value = data.event.action.value or {}
        print(f"[INFO] 卡片点击: user={user_id}, action={action_value}")
        return self.on_card_action(user_id, chat_id, message_id, action_value)

    def send_message(self, chat_id: str, msg_type: str, content: str) -> None:
        """发送任意类型消息"""
        request = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type(msg_type)
                .content(content)
                .build()) \
            .build()
        response = self.client.im.v1.message.create(request)
        if not response.success():
            print(f"[ERROR] 发送失败: code={response.code}, msg={response.msg}")

    def reply(self, chat_id: str, text: str) -> None:
        """发送文本消息"""
        self.send_message(chat_id, "text", json.dumps({"text": text}))

    def reply_card(self, chat_id: str, card: dict) -> None:
        """发送交互卡片消息"""
        self.send_message(chat_id, "interactive", json.dumps(card))

    @staticmethod
    def make_card_response(
        card: Optional[dict] = None,
        toast: Optional[str] = None,
        toast_type: str = "info",
    ) -> P2CardActionTriggerResponse:
        """构造卡片动作的响应（可更新卡片 / 弹 toast）"""
        resp = P2CardActionTriggerResponse()
        if toast:
            resp.toast = CallBackToast()
            resp.toast.type = toast_type
            resp.toast.content = toast
        if card:
            resp.card = CallBackCard()
            resp.card.type = "raw"
            resp.card.data = card
        return resp

    # ---- 业务逻辑层 (子类实现) ----

    @abstractmethod
    def on_message(self, sender_id: str, chat_id: str, text: str) -> None:
        """处理收到的文本消息"""
        ...

    def on_card_action(
        self, user_id: str, chat_id: str, message_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        """处理卡片按钮点击，子类可覆写"""
        return P2CardActionTriggerResponse()

    # ---- 启动 ----

    def start(self) -> None:
        """启动 WebSocket 长连接，开始监听消息"""
        ws_client = lark.ws.Client(
            self.app_id, self.app_secret,
            event_handler=self._event_handler,
            log_level=lark.LogLevel.INFO,
        )
        print("机器人启动中，正在连接飞书...")
        ws_client.start()
