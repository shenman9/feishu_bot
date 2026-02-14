"""
HubBot - 统一入口机器人
管理插件注册、功能菜单展示、消息/卡片事件路由。
"""

from typing import Dict, List

from core.feishu_bot import FeishuBot, _log
from core.plugin import Plugin
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)

MENU_KEYWORDS = {"菜单", "menu", "帮助", "help"}
EXIT_KEYWORDS = {"退出", "exit", "返回"}


class HubBot(FeishuBot):

    def __init__(self, app_id: str, app_secret: str):
        super().__init__(app_id, app_secret)
        self.plugins: Dict[str, Plugin] = {}       # keyword -> Plugin
        self.active_plugin: Dict[str, str] = {}    # user_id -> keyword

    # ---- 插件管理 ----

    def register(self, plugin: Plugin) -> None:
        plugin.on_register(self)
        self.plugins[plugin.keyword] = plugin
        _log("INFO", f"已注册插件: {plugin.name} (关键词='{plugin.keyword}')")

    def register_all(self, plugins: List[Plugin]) -> None:
        for p in plugins:
            self.register(p)

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

    def on_message(self, sender_id: str, chat_id: str, text: str) -> None:
        # 1. 菜单请求
        if text.lower() in MENU_KEYWORDS:
            self._send_menu(chat_id)
            return

        # 2. 退出当前插件
        if text.lower() in EXIT_KEYWORDS:
            prev_kw = self.active_plugin.pop(sender_id, None)
            if prev_kw and prev_kw in self.plugins:
                self.plugins[prev_kw].deactivate_user(sender_id)
            self.reply(chat_id, "已退出当前功能。发送「菜单」查看可用功能。")
            return

        # 3. 关键词匹配插件 → 激活
        if text in self.plugins:
            prev_kw = self.active_plugin.get(sender_id)
            if prev_kw and prev_kw in self.plugins:
                self.plugins[prev_kw].deactivate_user(sender_id)
            self.active_plugin[sender_id] = text
            self.plugins[text].handle_message(sender_id, chat_id, text)
            return

        # 4. 用户有活跃插件 → 转发
        active_kw = self.active_plugin.get(sender_id)
        if active_kw and active_kw in self.plugins:
            plugin = self.plugins[active_kw]
            plugin.handle_message(sender_id, chat_id, text)
            if not plugin.is_user_active(sender_id):
                self.active_plugin.pop(sender_id, None)
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
            plugin_kw = self.active_plugin.get(user_id)

        if plugin_kw and plugin_kw in self.plugins:
            return self.plugins[plugin_kw].handle_card_action(
                user_id, chat_id, message_id, action_value
            )

        return self.make_card_response(toast="请先选择一个功能（发送「菜单」查看）")

    # ---- 文件消息路由 ----

    def on_file_message(
        self, sender_id: str, chat_id: str, message_id: str,
        file_key: str, file_name: str
    ) -> None:
        """文件消息路由：转发给活跃插件，无活跃插件则提示"""
        active_kw = self.active_plugin.get(sender_id)
        if active_kw and active_kw in self.plugins:
            plugin = self.plugins[active_kw]
            plugin.handle_file_message(
                sender_id, chat_id, message_id, file_key, file_name
            )
            if not plugin.is_user_active(sender_id):
                self.active_plugin.pop(sender_id, None)
            return

        self.reply(chat_id, "请先发送「文件阅读」激活文件阅读功能，再上传文件。")
