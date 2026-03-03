"""
Claude Code 独立机器人适配层

将 ClaudeCodePlugin 直接包装为一个可独立运行的 FeishuBot 子类，
无需 HubBot 路由，所有消息直接转发给插件处理。

激活方式：首条消息自动触发激活（等同于 Hub 模式下发送关键词），
无需用户手动输入 "CC"。
"""

import logging
import os
from pathlib import Path

from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

from core.feishu_bot import FeishuBot
from plugins.claude_code.claude_code_plugin import ClaudeCodePlugin, PLUGIN_KEYWORD

logger = logging.getLogger(__name__)


class ClaudeCodeBot(FeishuBot):
    """Claude Code 独立机器人

    直接托管 ClaudeCodePlugin，跳过 HubBot 路由层。
    ClaudeCodePlugin 所需的 bot 引用（reply / reply_card 等）
    由 FeishuBot 基类本身提供，完全兼容。
    """

    def __init__(self, app_id: str, app_secret: str):
        super().__init__(app_id, app_secret)
        # CC 专属机器人通过环境变量注入独立的配置目录和数据目录，实现与 hub_agent 的隔离
        config_dir = Path(os.environ["CC_CONFIG_DIR"]) if "CC_CONFIG_DIR" in os.environ else None
        data_dir = Path(os.environ["CC_DATA_DIR"]) if "CC_DATA_DIR" in os.environ else None
        self._plugin = ClaudeCodePlugin(data_dir=data_dir, config_dir=config_dir)
        # 将自身注入为插件的 bot 引用（FeishuBot 提供了插件所需的全部方法）
        self._plugin.on_register(self)

    # ---- 消息路由 ----

    def on_message(self, user_id: str, chat_id: str, text: str) -> None:
        """将消息转发给 CC 插件；首条消息自动激活，无需发送关键词"""
        state = self._plugin._get_state(user_id)

        if not state["active"]:
            logger.info("[CC-Standalone] 自动激活用户: user=%s", user_id)
            # 注入关键词触发激活欢迎消息
            self._plugin.handle_message(user_id, chat_id, PLUGIN_KEYWORD)
            # 若用户第一条消息本身就是 prompt，激活后立即处理，不浪费一轮
            if text.strip().upper() != PLUGIN_KEYWORD:
                self._plugin.handle_message(user_id, chat_id, text)
        else:
            self._plugin.handle_message(user_id, chat_id, text)

    def on_card_action(
        self, user_id: str, chat_id: str, message_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        """将卡片点击事件（取消、权限确认）转发给插件"""
        return self._plugin.handle_card_action(user_id, chat_id, message_id, action_value)

    def on_file_message(
        self, user_id: str, chat_id: str, message_id: str,
        file_key: str, file_name: str
    ) -> None:
        """将文件消息转发给插件（预留扩展）"""
        self._plugin.handle_file_message(user_id, chat_id, message_id, file_key, file_name)
