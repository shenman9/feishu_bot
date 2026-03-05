"""Gemini API 客户端。

使用 /v1beta/ 端点（Google Gemini 格式），该端点无 Claude Code 客户端检测。
"""

import json
import logging
import time

import httpx

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 529}


class GeminiError(Exception):
    """Gemini API 调用失败。"""


def call_gemini(
    api_key: str,
    model: str,
    prompt: str,
    base_url: str = "https://api.lingyaai.cn",
    max_retries: int = 3,
    retry_delay: float = 2.0,
    timeout: float = 120.0,
) -> str:
    """调用 Gemini generateContent API，返回模型文本响应。

    使用 /v1beta/ 端点（Google Gemini 格式），该端点无 Claude Code 客户端检测。
    响应中的 thought 部分（thinking 模型的思考过程）会被自动跳过。

    Args:
        api_key: Gemini API key（通过 query param 传递）。
        model: 模型名称，如 "gemini-3.1-pro-preview-thinking"。
        prompt: 用户 prompt。
        base_url: API 基础 URL。
        max_retries: 最大重试次数（仅 5xx/429 重试）。
        retry_delay: 初始重试间隔（秒），指数退避。
        timeout: 请求超时（秒）。

    Returns:
        模型输出文本（已去除首尾空白）。

    Raises:
        GeminiError: API 调用失败或无法提取文本时。
    """
    url = f"{base_url.rstrip('/')}/v1beta/models/{model}:generateContent"
    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    logger.debug(f"Gemini 请求 [{model}] prompt ({len(prompt)}字):\n{prompt[:300]}{'...' if len(prompt) > 300 else ''}")

    last_err: Exception | None = None
    with httpx.Client(timeout=timeout) as client:
        for attempt in range(max_retries + 1):
            try:
                resp = client.post(
                    url,
                    params={"key": api_key},
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )

                if resp.status_code in RETRYABLE_STATUS and attempt < max_retries:
                    delay = retry_delay * (2 ** attempt)
                    logger.warning(f"Gemini HTTP {resp.status_code}，重试 {attempt+1}/{max_retries}（等待 {delay:.0f}s）")
                    time.sleep(delay)
                    continue

                if resp.status_code != 200:
                    raise GeminiError(f"HTTP {resp.status_code}: {resp.text[:300]}")

                data = resp.json()

                # 检查 API 层面的错误（200 但含 error 字段）
                if "error" in data:
                    err = data["error"]
                    msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    raise GeminiError(f"API 错误: {msg[:200]}")

                # 提取文本，跳过 thought 部分
                parts = (
                    data.get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [])
                )
                for part in parts:
                    if not part.get("thought"):
                        text = part.get("text", "").strip()
                        logger.debug(f"Gemini 响应 ({len(text)}字): {text[:200]}{'...' if len(text) > 200 else ''}")
                        return text

                raise GeminiError(f"响应中无可用文本: {json.dumps(data, ensure_ascii=False)[:200]}")

            except httpx.HTTPError as e:
                last_err = e
                if attempt < max_retries:
                    delay = retry_delay * (2 ** attempt)
                    logger.warning(f"Gemini 网络错误，重试 {attempt+1}/{max_retries}: {e}")
                    time.sleep(delay)
                    continue
                raise GeminiError(f"网络请求失败: {e}") from e

    raise GeminiError(f"重试耗尽: {last_err}")
