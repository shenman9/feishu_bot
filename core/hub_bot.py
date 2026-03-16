"""
HubBot - 统一入口机器人
管理插件注册、功能菜单展示、消息/卡片事件路由。
"""

import logging

from core.feishu_bot import FeishuBot
from core.plugin import Plugin
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)

logger = logging.getLogger(__name__)

MENU_KEYWORDS = {"菜单", "menu", "帮助", "help"}
EXIT_KEYWORDS = {"退出", "exit", "返回"}


class HubBot(FeishuBot):

    def __init__(self, app_id: str, app_secret: str):
        super().__init__(app_id, app_secret)
        self.plugins: dict[str, Plugin] = {}       # keyword -> Plugin
        # (user_id, chat_id) -> keyword — 按群聊隔离插件激活状态
        self.active_plugin: dict[tuple[str, str], str] = {}

    # ---- 插件管理 ----

    def register(self, plugin: Plugin) -> None:
        if plugin.keyword in self.plugins:
            existing = self.plugins[plugin.keyword]
            raise ValueError(
                f"插件关键词冲突: '{plugin.keyword}' 已被插件 '{existing.name}' 注册，"
                f"无法再注册插件 '{plugin.name}'"
            )
        plugin.on_register(self)
        self.plugins[plugin.keyword] = plugin
        logger.info("已注册插件: %s (关键词='%s')", plugin.name, plugin.keyword)

    def register_all(self, plugins: list[Plugin]) -> None:
        for p in plugins:
            try:
                self.register(p)
            except Exception as e:
                logger.error(
                    "插件 '%s' 注册失败，已跳过: %s",
                    type(p).__name__, e, exc_info=True,
                )

    # ---- 功能菜单 ----

    def _send_menu(self, chat_id: str) -> None:
        rows = []
        for plugin in self.plugins.values():
            rows.append(
                f"**{plugin.name}** - {plugin.description}\n"
                f"> 发送「{plugin.keyword}」启动"
            )
        body_md = "\n\n".join(rows) if rows else "暂无可用功能"

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "🤖 功能菜单"},
                "template": "blue",
            },
            "elements": [
                {"tag": "markdown", "content": body_md},
            ],
        }
        self.reply_card(chat_id, card)

    # ---- 消息路由 ----

    def on_message(self, sender_id: str, chat_id: str, text: str,
                   message_id: str = "") -> None:
        # 1. 菜单请求
        if text.lower() in MENU_KEYWORDS:
            self._send_menu(chat_id)
            return

        # 2. 退出当前插件
        if text.lower() in EXIT_KEYWORDS:
            prev_kw = self.active_plugin.pop((sender_id, chat_id), None)
            if prev_kw and prev_kw in self.plugins:
                try:
                    self.plugins[prev_kw].deactivate_user(sender_id, chat_id)
                except Exception as e:
                    logger.error("插件 '%s' deactivate_user 异常: %s", prev_kw, e, exc_info=True)
            self.reply(chat_id, "已退出当前功能。发送「菜单」查看可用功能。")
            return

        # 3. 关键词匹配插件 → 激活
        if text in self.plugins:
            prev_kw = self.active_plugin.get((sender_id, chat_id))
            if prev_kw and prev_kw in self.plugins:
                try:
                    self.plugins[prev_kw].deactivate_user(sender_id, chat_id)
                except Exception as e:
                    logger.error("插件 '%s' deactivate_user 异常: %s", prev_kw, e, exc_info=True)
            self.active_plugin[(sender_id, chat_id)] = text
            try:
                self.plugins[text].handle_message(sender_id, chat_id, text, message_id=message_id)
            except Exception as e:
                logger.error("插件 '%s' 处理消息异常: %s", text, e, exc_info=True)
                self.reply(chat_id, "该功能暂时遇到问题，请稍后再试。")
            return

        # 4. 用户有活跃插件 → 转发
        active_kw = self.active_plugin.get((sender_id, chat_id))
        if active_kw and active_kw in self.plugins:
            plugin = self.plugins[active_kw]
            try:
                plugin.handle_message(sender_id, chat_id, text, message_id=message_id)
            except Exception as e:
                logger.error("插件 '%s' 处理消息异常: %s", active_kw, e, exc_info=True)
                self.reply(chat_id, "该功能暂时遇到问题，请稍后再试。")
            try:
                if not plugin.is_user_active(sender_id, chat_id):
                    self.active_plugin.pop((sender_id, chat_id), None)
            except Exception as e:
                logger.error("插件 '%s' is_user_active 异常: %s", active_kw, e, exc_info=True)
            return

        # 5. 无上下文 → 展示菜单
        self._send_menu(chat_id)

    # ---- 卡片事件路由 ----

    def on_card_action(
        self, user_id: str, chat_id: str, message_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        # 优先从 action_value 中读 plugin 字段，回退到活跃插件
        plugin_kw = action_value.get("plugin")
        if not plugin_kw:
            plugin_kw = self.active_plugin.get((user_id, chat_id))

        if plugin_kw and plugin_kw in self.plugins:
            try:
                return self.plugins[plugin_kw].handle_card_action(
                    user_id, chat_id, message_id, action_value
                )
            except Exception as e:
                logger.error("插件 '%s' 处理卡片事件异常: %s", plugin_kw, e, exc_info=True)
                return self.make_card_response(toast="该功能暂时遇到问题")

        return self.make_card_response(toast="请先选择一个功能（发送「菜单」查看）")

    # ---- 文件消息路由 ----

    def on_file_message(
        self, sender_id: str, chat_id: str, message_id: str,
        file_key: str, file_name: str
    ) -> None:
        """文件消息路由：转发给活跃插件，无活跃插件则提示"""
        active_kw = self.active_plugin.get((sender_id, chat_id))
        if active_kw and active_kw in self.plugins:
            plugin = self.plugins[active_kw]
            try:
                plugin.handle_file_message(
                    sender_id, chat_id, message_id, file_key, file_name
                )
            except Exception as e:
                logger.error("插件 '%s' 处理文件消息异常: %s", active_kw, e, exc_info=True)
                self.reply(chat_id, "该功能暂时遇到问题，请稍后再试。")
            try:
                if not plugin.is_user_active(sender_id, chat_id):
                    self.active_plugin.pop((sender_id, chat_id), None)
            except Exception as e:
                logger.error("插件 '%s' is_user_active 异常: %s", active_kw, e, exc_info=True)
            return

        self.reply(chat_id, "请先发送「文件阅读」激活文件阅读功能，再上传文件。")

    # ---- 机器人菜单事件路由 ----

    def on_bot_menu(self, user_id: str, open_id: str, event_key: str) -> None:
        """处理底部菜单栏点击：event_key 匹配插件 keyword 则激活，否则展示菜单"""
        # 菜单关键词 → 展示功能菜单
        if event_key in MENU_KEYWORDS:
            self._send_menu(open_id)
            return

        if event_key in self.plugins:
            # 退出当前会话中的活跃插件
            prev_kw = self.active_plugin.get((user_id, open_id))
            if prev_kw and prev_kw in self.plugins:
                try:
                    self.plugins[prev_kw].deactivate_user(user_id, open_id)
                except Exception as e:
                    logger.error("插件 '%s' deactivate_user 异常: %s", prev_kw, e, exc_info=True)
            # 激活目标插件，open_id 作为消息目标（send_message 自动检测 ID 类型）
            self.active_plugin[(user_id, open_id)] = event_key
            try:
                self.plugins[event_key].handle_message(user_id, open_id, event_key)
            except Exception as e:
                logger.error("插件 '%s' 处理消息异常: %s", event_key, e, exc_info=True)
                self.reply(open_id, "该功能暂时遇到问题，请稍后再试。")
        else:
            self._send_menu(open_id)
