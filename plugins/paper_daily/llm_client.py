"""封装 Lingya API (Claude 兼容) 的 LLM 客户端。"""

import logging
import time

import httpx

from .config import AppConfig

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """LLM API 调用失败。"""


class LLMClient:
    """通过 httpx 直接请求 /v1/messages 端点。"""

    RETRYABLE_STATUS = {429, 500, 502, 503, 529}

    def __init__(self, config: AppConfig):
        self.base_url = config.llm_base_url.rstrip("/")
        self.api_key = config.llm_api_key
        self.model = config.llm_model
        self.max_retries = 3
        self.retry_delay = 2.0
        self.client = httpx.Client(timeout=60.0)

    def chat(self, system_prompt: str, user_message: str, max_tokens: int = 2000) -> str:
        """发送消息并返回文本响应。失败时抛出 LLMError。

        将 system_prompt 拼接到 user_message 前，以兼容不支持
        顶层 system 字段的 API 代理。
        """
        combined_message = f"{system_prompt}\n\n---\n\n{user_message}"
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": combined_message}],
        }
        data = self._request(payload)
        if data.get("stop_reason") == "max_tokens":
            logger.warning(f"LLM 响应被截断 (max_tokens={max_tokens})")
        return data["content"][0]["text"]

    def _request(self, payload: dict) -> dict:
        """执行 HTTP POST，带指数退避重试。"""
        url = f"{self.base_url}/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.client.post(url, headers=headers, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    # API 代理可能返回 200 但包含错误
                    if "error" in data:
                        err_msg = data["error"].get("message", str(data["error"]))
                        raise LLMError(f"LLM API 返回错误: {err_msg}")
                    return data
                if resp.status_code in self.RETRYABLE_STATUS and attempt < self.max_retries:
                    delay = self.retry_delay * (2 ** attempt)
                    logger.warning(f"LLM API {resp.status_code}, 重试 {attempt+1}/{self.max_retries} (等待 {delay}s)")
                    time.sleep(delay)
                    continue
                raise LLMError(f"LLM API 返回 {resp.status_code}: {resp.text[:200]}")
            except httpx.HTTPError as e:
                last_err = e
                if attempt < self.max_retries:
                    delay = self.retry_delay * (2 ** attempt)
                    logger.warning(f"LLM API 网络错误, 重试 {attempt+1}/{self.max_retries}: {e}")
                    time.sleep(delay)
                    continue
                raise LLMError(f"LLM API 请求失败: {e}") from e

        raise LLMError(f"LLM API 重试耗尽: {last_err}")

    def close(self):
        """关闭 httpx 客户端。"""
        self.client.close()
