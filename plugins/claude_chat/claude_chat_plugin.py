"""
Claude 对话插件

通过 Anthropic Messages API 兼容的中转服务与 Claude 进行多轮对话。
支持流式响应，实时更新飞书消息内容。
"""

import json
import logging
import time
from typing import Optional

import requests

from config import load_plugin_config
from core.feishu_bot import limit_card_tables
from core.plugin import Plugin

logger = logging.getLogger(__name__)

# 默认配置
DEFAULT_MAX_HISTORY = 20
DEFAULT_MAX_TOKENS = 4096
DEFAULT_MODEL = "claude-opus-4-6"

# 流式更新控制
_PATCH_INTERVAL = 0.5   # 最小更新间隔（秒）
_PATCH_MIN_CHARS = 50   # 最小新增字符数触发更新


class ClaudeChatPlugin(Plugin):
    """Claude 对话插件

    支持多轮对话，维护每个用户独立的消息历史。
    通过 Anthropic Messages API 兼容接口调用 Claude 模型，
    使用流式响应实时更新飞书消息。
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
        """懒加载 Claude 配置，首次调用时从 config/claude_chat.yaml 读取"""
        if self._config is None:
            cc = load_plugin_config("claude_chat")
            self._config = {
                "api_url": cc.get("api_url", ""),
                "api_key": cc.get("api_key", ""),
                "model": cc.get("model", DEFAULT_MODEL),
                "max_history": cc.get("max_history", DEFAULT_MAX_HISTORY),
                "max_tokens": cc.get("max_tokens", DEFAULT_MAX_TOKENS),
                "system_prompt": cc.get("system_prompt", ""),
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
# PLACEHOLDER_STREAM

    def _call_claude_api_stream(self, history: list[dict], message_id: Optional[str],
                                chat_id: str) -> str:
        """流式调用 Claude API，实时更新飞书消息

        Args:
            history: 对话历史消息列表
            message_id: 占位消息的 ID，为 None 时降级为非流式
            chat_id: 聊天 ID（降级时用于发送消息）

        Returns:
            Claude 的完整回复文本

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
            "stream": True,
        }

        if cfg["system_prompt"]:
            payload["system"] = cfg["system_prompt"]

        try:
            resp = requests.post(
                cfg["api_url"],
                headers=headers,
                json=payload,
                timeout=120,
                stream=True,
            )
        except requests.RequestException as e:
            raise RuntimeError(f"网络请求失败: {e}") from e

        if resp.status_code != 200:
            raise RuntimeError(
                f"API 返回错误: status={resp.status_code}, body={resp.text[:500]}"
            )

        # 解析 SSE 流，累积文本并定期更新消息
        full_text = ""
        last_patch_len = 0
        last_patch_time = time.time()

        try:
            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[6:]  # 去掉 "data: " 前缀
                if data_str.strip() == "[DONE]":
                    break
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    logger.debug("SSE JSON 解析失败, 原始数据: %s", data_str[:200])
                    continue

                # 提取增量文本
                delta_text = self._extract_delta_text(data)
                if not delta_text:
                    continue

                full_text += delta_text

                # 按频率控制更新消息
                if message_id:
                    now = time.time()
                    chars_since = len(full_text) - last_patch_len
                    time_since = now - last_patch_time
                    if chars_since >= _PATCH_MIN_CHARS or time_since >= _PATCH_INTERVAL:
                        self._patch_text(message_id, full_text)
                        last_patch_len = len(full_text)
                        last_patch_time = now
        finally:
            resp.close()

        if not full_text:
            raise RuntimeError("API 流式响应中无文本内容")

        # 最终更新一次完整内容
        if message_id and len(full_text) > last_patch_len:
            self._patch_text(message_id, full_text, chat_id=chat_id)

        return full_text
# PLACEHOLDER_HANDLE

    @staticmethod
    def _extract_delta_text(data: dict) -> str:
        """从 SSE 事件数据中提取增量文本"""
        # content_block_delta 事件
        if data.get("type") == "content_block_delta":
            delta = data.get("delta", {})
            if delta.get("type") == "text_delta":
                return delta.get("text", "")
        return ""

    @staticmethod
    def _build_card(text: str) -> str:
        """构造包含文本的飞书卡片 JSON"""
        text = limit_card_tables(text)
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "Claude"},
                "template": "blue",
            },
            "elements": [
                {"tag": "markdown", "content": text},
            ],
        }
        return json.dumps(card)

    def _patch_text(self, message_id: str, text: str, chat_id: str = "") -> None:
        """更新飞书卡片消息内容，表格超限时自动降级重试

        Args:
            chat_id: 可选，仅最终更新时传入。卡片彻底更新失败时
                     以纯文本发送回复内容，确保用户至少能收到结果。
        """
        content = self._build_card(text)
        try:
            ok = self.bot.patch_message(message_id, content)
            if not ok:
                # 降级：将所有表格转为代码块后重试
                safe_text = limit_card_tables(text, 0)
                ok = self.bot.patch_message(message_id, self._build_card(safe_text))
            if not ok and chat_id and text:
                # 终极兜底：卡片彻底失败，以纯文本发送回复内容
                logger.warning("卡片更新彻底失败，降级为纯文本发送")
                self.bot.reply(chat_id, text)
        except Exception as e:
            logger.warning("消息更新失败: %s", e)
            if chat_id and text:
                try:
                    self.bot.reply(chat_id, text)
                except Exception:
                    pass

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

        # 3. 发送卡片占位消息，获取 message_id 用于后续更新
        #    飞书 PatchMessage API 仅支持更新卡片消息，不支持纯文本
        placeholder = self._build_card("正在思考...")
        message_id = self.bot.send_message_get_id(chat_id, "interactive", placeholder)

        # 4. 流式调用 API 并实时更新消息
        try:
            reply_text = self._call_claude_api_stream(
                state["history"], message_id, chat_id
            )
        except RuntimeError as e:
            logger.error("Claude API 调用失败: %s", e)
            # 移除刚加入的用户消息，避免污染历史
            if state["history"] and state["history"][-1]["role"] == "user":
                state["history"].pop()
            # 更新占位消息为错误提示
            error_msg = f"Claude 暂时无法回复，请稍后再试。\n错误: {e}"
            if message_id:
                self._patch_text(message_id, error_msg, chat_id=chat_id)
            else:
                self.bot.reply(chat_id, error_msg)
            return

        # 5. 将 Claude 回复加入历史
        state["history"].append({"role": "assistant", "content": reply_text})

        # 6. 如果没有 message_id（降级模式），直接发送完整回复
        if not message_id:
            self.bot.reply(chat_id, reply_text)

    def is_user_active(self, user_id: str, chat_id: str = "") -> bool:
        """用户是否在活跃会话中"""
        return self._get_state(user_id)["active"]

    def deactivate_user(self, user_id: str, chat_id: str = "") -> None:
        """清理用户全部会话状态"""
        self.user_states.pop(user_id, None)
