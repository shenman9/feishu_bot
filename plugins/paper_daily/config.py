"""加载配置文件和环境变量。"""

import os
import logging
from dataclasses import dataclass

import yaml

logger = logging.getLogger(__name__)


@dataclass
class AppConfig:
    """应用配置。"""

    topics: list[str]
    categories: list[str]
    max_papers: int
    feishu_webhook_url: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str


def load_config(config_path: str = "config.yaml") -> AppConfig:
    """从 YAML 文件加载配置，LLM 参数从环境变量读取。"""
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    return AppConfig(
        topics=cfg["topics"],
        categories=cfg["categories"],
        max_papers=cfg.get("max_papers", 50),
        feishu_webhook_url=os.environ.get("FEISHU_WEBHOOK_URL", cfg.get("feishu_webhook_url", "")),
        llm_base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://api.lingyaai.cn"),
        llm_api_key=os.environ.get("ANTHROPIC_AUTH_TOKEN", ""),
        llm_model="claude-opus-4-6",
    )
