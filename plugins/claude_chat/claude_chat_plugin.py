"""
Claude 对话插件

通过 Anthropic Messages API 兼容的中转服务与 Claude 进行多轮对话。
"""

import json
import logging
from typing import Optional

import requests

from config import load_config
from core.plugin import Plugin

logger = logging.getLogger(__name__)

# 默认配置
DEFAULT_MAX_HISTORY = 20
DEFAULT_MAX_TOKENS = 4096
DEFAULT_MODEL = "claude-opus-4-6"


class ClaudeChatPlugin(Plugin):
    """Claude 对话插件

    支持多轮对话，维护每个用户独立的消息历史。
    通过 Anthropic Messages API 兼容接口调用 Claude 模型。
    """

    def __init__(self):
        super().__init__()
        # user_id -> {"active": bool, "history": [{"role": str, "content": str}]}
        self.user_states: dict[str, dict] = {}
        self._config: Optional[dict] = None

    # ---- 元信息 ----

    @property
    def name(self) -> str:
        return "Claude 对话"

    @property
    def keyword(self) -> str:
        return "Claude"

    @property
    def description(self) -> str:
        return "与 Claude 进行多轮智能对话"

    # ---- 内部方法 ----

    def _load_claude_config(self) -> dict:
        """懒加载 Claude 配置，首次调用时从 config.yaml 读取"""
        if self._config is None:
            cfg = load_config()
            claude_cfg = cfg.get("claude", {})
            self._config = {
                "api_url": claude_cfg.get("api_url", ""),
                "api_key": claude_cfg.get("api_key", ""),
                "model": claude_cfg.get("model", DEFAULT_MODEL),
                "max_history": claude_cfg.get("max_history", DEFAULT_MAX_HISTORY),
                "max_tokens": claude_cfg.get("max_tokens", DEFAULT_MAX_TOKENS),
                "system_prompt": claude_cfg.get("system_prompt", ""),
            }
        return self._config

    def _get_state(self, user_id: str) -> dict:
        """获取用户会话状态，不存在则初始化"""
        if user_id not in self.user_states:
            self.user_states[user_id] = {"active": False, "history": []}
        return self.user_states[user_id]

    def _trim_history(self, history: list[dict]) -> list[dict]:
        """裁剪对话历史，保留最近 max_history 条消息

        裁剪后确保第一条是 user 角色（Anthropic API 要求）。
        """
        cfg = self._load_claude_config()
        max_history = cfg["max_history"]
        if len(history) <= max_history:
            return history
        trimmed = history[-max_history:]
        # 确保第一条是 user 消息
        if trimmed and trimmed[0]["role"] != "user":
            trimmed = trimmed[1:]
        return trimmed

    def _call_claude_api(self, history: list[dict]) -> str:
        """调用 Claude API 获取回复

        Args:
            history: 对话历史消息列表

        Returns:
            Claude 的回复文本

        Raises:
            RuntimeError: API 调用失败时抛出
        """
        cfg = self._load_claude_config()

        headers = {
            "Content-Type": "application/json",
            "x-api-key": cfg["api_key"],
            "anthropic-version": "2023-06-01",
        }

        payload: dict = {
            "model": cfg["model"],
            "max_tokens": cfg["max_tokens"],
            "messages": history,
        }

        # 如果配置了系统提示词，加入 system 字段
        if cfg["system_prompt"]:
            payload["system"] = cfg["system_prompt"]

        try:
            resp = requests.post(
                cfg["api_url"],
                headers=headers,
                json=payload,
                timeout=120,
            )
        except requests.RequestException as e:
            raise RuntimeError(f"网络请求失败: {e}") from e

        if resp.status_code != 200:
            raise RuntimeError(
                f"API 返回错误: status={resp.status_code}, body={resp.text[:500]}"
            )

        try:
            data = resp.json()
        except ValueError as e:
            raise RuntimeError(f"API 响应解析失败: {e}") from e

        # Anthropic Messages API 响应格式:
        # {"content": [{"type": "text", "text": "..."}], ...}
        content_blocks = data.get("content", [])
        texts = [
            block["text"]
            for block in content_blocks
            if block.get("type") == "text"
        ]
        if not texts:
            raise RuntimeError(f"API 响应中无文本内容: {data}")

        return "\n".join(texts)

    # ---- Plugin 接口实现 ----

    def handle_message(self, user_id: str, chat_id: str, text: str) -> None:
        """处理用户消息"""
        state = self._get_state(user_id)

        # 关键词激活
        if text == self.keyword:
            state["active"] = True
            history_len = len(state["history"])
            if history_len > 0:
                self.bot.reply(
                    chat_id,
                    f"Claude 对话已激活，当前有 {history_len // 2} 轮历史对话。\n"
                    "直接发消息即可继续对话，发送「清空对话」可重新开始。",
                )
            else:
                self.bot.reply(
                    chat_id,
                    "Claude 对话已激活，请直接发送消息开始对话。\n"
                    "发送「清空对话」可清空历史记录。",
                )
            return

        # 清空对话历史指令
        if text == "清空对话":
            state["history"] = []
            self.bot.reply(chat_id, "对话历史已清空，可以开始新的对话。")
            return

        # 正常对话流程
        # 1. 将用户消息加入历史
        state["history"].append({"role": "user", "content": text})

        # 2. 裁剪历史
        state["history"] = self._trim_history(state["history"])

        # 3. 调用 API
        try:
            reply_text = self._call_claude_api(state["history"])
        except RuntimeError as e:
            logger.error("Claude API 调用失败: %s", e)
            # 移除刚加入的用户消息，避免污染历史
            if state["history"] and state["history"][-1]["role"] == "user":
                state["history"].pop()
            self.bot.reply(chat_id, f"Claude 暂时无法回复，请稍后再试。\n错误: {e}")
            return

        # 4. 将 Claude 回复加入历史
        state["history"].append({"role": "assistant", "content": reply_text})

        # 5. 发送回复
        self.bot.reply(chat_id, reply_text)

    def is_user_active(self, user_id: str) -> bool:
        """用户是否在活跃会话中"""
        return self._get_state(user_id)["active"]

    def deactivate_user(self, user_id: str) -> None:
        """清理用户全部会话状态"""
        self.user_states.pop(user_id, None)
