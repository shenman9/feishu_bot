"""
飞书机器人基类
封装了连接、消息收发等通用逻辑，子类只需实现 on_message / on_card_action 处理业务。
"""

import json
import logging
import threading
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import *
from lark_oapi.api.application.v6.model.p2_application_bot_menu_v6 import (
    P2ApplicationBotMenuV6,
)
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
    CallBackCard,
    CallBackToast,
)

logger = logging.getLogger(__name__)

# 消息去重缓存最大容量和过期时间
_DEDUP_MAX_SIZE = 500
_DEDUP_TTL = 300  # 5 分钟


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
        self._seen_messages: OrderedDict[str, float] = OrderedDict()
        self._dedup_lock = threading.Lock()

        self.client = lark.Client.builder() \
            .app_id(app_id) \
            .app_secret(app_secret) \
            .log_level(lark.LogLevel.INFO) \
            .build()

        self._event_handler = lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(self._on_raw_message) \
            .register_p2_card_action_trigger(self._on_raw_card_action) \
            .register_p2_application_bot_menu_v6(self._on_raw_bot_menu) \
            .build()

    # ---- 消息收发层 ----

    def _is_duplicate(self, message_id: str) -> bool:
        """检查消息是否重复，同时清理过期条目"""
        with self._dedup_lock:
            now = time.time()
            if message_id in self._seen_messages:
                return True
            # 清理过期条目
            while self._seen_messages:
                oldest_id, ts = next(iter(self._seen_messages.items()))
                if now - ts > _DEDUP_TTL:
                    self._seen_messages.pop(oldest_id)
                else:
                    break
            # 容量上限兜底
            if len(self._seen_messages) >= _DEDUP_MAX_SIZE:
                self._seen_messages.popitem(last=False)
            self._seen_messages[message_id] = now
            return False

    @staticmethod
    def _strip_mentions(text: str, mentions: list) -> str:
        """从消息文本中剥离所有 @提及 占位符（如 @_user_1）"""
        for mention in mentions:
            key = getattr(mention, "key", "")
            if key:
                text = text.replace(key, "")
        return text.strip()

    def _on_raw_message(self, data: P2ImMessageReceiveV1) -> None:
        """解析原始消息，根据消息类型分发到对应处理方法

        群聊消息仅处理 @机器人 的消息，非 @消息直接忽略。
        """
        message = data.event.message
        sender_id = data.event.sender.sender_id.user_id
        chat_id = message.chat_id
        message_id = message.message_id
        msg_type = message.message_type
        chat_type = getattr(message, "chat_type", None) or "p2p"
        mentions = getattr(message, "mentions", None) or []

        # 消息去重，防止飞书重试导致重复处理
        if self._is_duplicate(message_id):
            logger.info("跳过重复消息: message_id=%s", message_id)
            return

        # 群聊非 @消息直接忽略（后续可扩展为被动监控）
        if chat_type == "group" and not mentions:
            logger.debug("忽略群聊非@消息: chat=%s, user=%s", chat_id, sender_id)
            return

        try:
            content_dict = json.loads(message.content)
        except (json.JSONDecodeError, AttributeError) as e:
            logger.warning("消息内容解析失败: message_id=%s, error=%s", message_id, type(e).__name__)
            return

        if msg_type == "file":
            file_key = content_dict.get("file_key", "")
            file_name = content_dict.get("file_name", "")
            logger.info("收到文件: user=%s, chat_type=%s, message_id=%s, file=%s",
                       sender_id, chat_type, message_id, file_name)
            self.on_file_message(sender_id, chat_id, message_id, file_key, file_name)
        else:
            text = content_dict.get("text", "").strip()
            # 群聊消息剥离 @提及 占位符
            if chat_type == "group" and mentions:
                text = self._strip_mentions(text, mentions)
            logger.info("收到消息: user=%s, chat_type=%s, message_id=%s, text=%s",
                       sender_id, chat_type, message_id, text)
            self.on_message(sender_id, chat_id, text)

    def _on_raw_card_action(self, data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        """解析卡片按钮点击事件，交给子类处理"""
        user_id = data.event.operator.user_id
        chat_id = data.event.context.open_chat_id
        message_id = data.event.context.open_message_id
        action_value = data.event.action.value or {}
        # 表单提交时携带输入框的值，注入 action_value 供插件读取
        if data.event.action.form_value:
            action_value["_form_value"] = data.event.action.form_value
        logger.info("卡片点击: user=%s, action=%s", user_id, action_value)
        return self.on_card_action(user_id, chat_id, message_id, action_value)

    def _on_raw_bot_menu(self, data: P2ApplicationBotMenuV6) -> None:
        """解析机器人菜单点击事件，交给子类处理"""
        operator = data.event.operator
        user_id = operator.operator_id.user_id
        open_id = operator.operator_id.open_id
        event_key = data.event.event_key
        logger.info("菜单点击: user=%s, event_key=%s", user_id, event_key)
        self.on_bot_menu(user_id, open_id, event_key)

    @staticmethod
    def _detect_id_type(receive_id: str) -> str:
        """根据 ID 前缀自动判断 receive_id_type（ou_ → open_id，默认 chat_id）"""
        if receive_id.startswith("ou_"):
            return "open_id"
        return "chat_id"

    def send_message(self, chat_id: str, msg_type: str, content: str) -> None:
        """发送任意类型消息"""
        id_type = self._detect_id_type(chat_id)
        request = CreateMessageRequest.builder() \
            .receive_id_type(id_type) \
            .request_body(CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type(msg_type)
                .content(content)
                .build()) \
            .build()
        response = self.client.im.v1.message.create(request)
        if not response.success():
            logger.error("发送失败: code=%s, msg=%s", response.code, response.msg)

    def send_message_get_id(self, chat_id: str, msg_type: str, content: str) -> Optional[str]:
        """发送消息并返回 message_id，失败时返回 None"""
        id_type = self._detect_id_type(chat_id)
        request = CreateMessageRequest.builder() \
            .receive_id_type(id_type) \
            .request_body(CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type(msg_type)
                .content(content)
                .build()) \
            .build()
        response = self.client.im.v1.message.create(request)
        if not response.success():
            logger.error("发送失败: code=%s, msg=%s", response.code, response.msg)
            return None
        try:
            return response.data.message_id
        except AttributeError:
            return None

    def patch_message(self, message_id: str, content: str) -> None:
        """更新已发送消息的文本内容"""
        request = PatchMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(PatchMessageRequestBody.builder()
                .content(content)
                .build()) \
            .build()
        response = self.client.im.v1.message.patch(request)
        if not response.success():
            logger.error("消息更新失败: code=%s, msg=%s", response.code, response.msg)

    def urgent_message(self, message_id: str, user_ids: list[str]) -> bool:
        """对已有消息发送应用内加急通知

        Args:
            message_id: 要加急的消息 ID
            user_ids: 接收加急通知的用户 user_id 列表

        Returns:
            是否成功
        """
        request = UrgentAppMessageRequest.builder() \
            .message_id(message_id) \
            .user_id_type("user_id") \
            .request_body(UrgentReceivers.builder()
                .user_id_list(user_ids)
                .build()) \
            .build()
        response = self.client.im.v1.message.urgent_app(request)
        if not response.success():
            logger.error("加急通知失败: code=%s, msg=%s", response.code, response.msg)
            return False
        return True

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

    def download_file(self, message_id: str, file_key: str) -> bytes:
        """下载飞书消息中的文件，返回文件二进制内容

        Args:
            message_id: 消息 ID
            file_key: 文件的 file_key

        Returns:
            文件的二进制内容

        Raises:
            RuntimeError: 下载失败时抛出
        """
        request = GetMessageResourceRequest.builder() \
            .message_id(message_id) \
            .file_key(file_key) \
            .type("file") \
            .build()
        response = self.client.im.v1.message_resource.get(request)
        if not response.success():
            raise RuntimeError(f"文件下载失败: code={response.code}, msg={response.msg}")
        return response.file.read()

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

    def on_file_message(
        self, sender_id: str, chat_id: str, message_id: str,
        file_key: str, file_name: str
    ) -> None:
        """处理收到的文件消息，子类可覆写"""
        pass

    def on_bot_menu(self, user_id: str, open_id: str, event_key: str) -> None:
        """处理机器人菜单点击事件，子类可覆写"""
        pass

    # ---- 启动 ----

    def start(self) -> None:
        """启动 WebSocket 长连接，开始监听消息"""
        ws_client = lark.ws.Client(
            self.app_id, self.app_secret,
            event_handler=self._event_handler,
            log_level=lark.LogLevel.INFO,
        )
        logger.info("机器人启动中，正在连接飞书...")
        ws_client.start()
