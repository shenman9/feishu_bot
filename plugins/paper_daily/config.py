"""论文日报插件配置数据类。"""

from dataclasses import dataclass


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
