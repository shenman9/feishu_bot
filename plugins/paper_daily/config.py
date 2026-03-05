"""论文日报插件配置数据类。"""

from dataclasses import dataclass, field


@dataclass
class AppConfig:
    """插件运行配置，由 config/paper_daily.yaml 加载。"""

    research_interest: str = ""  # 用户自由描述的研究兴趣
    categories: list[str] = field(default_factory=lambda: ["cs.CL", "cs.AI", "cs.LG"])
    max_papers: int = 200
    llm_base_url: str = "https://api.lingyaai.cn"
    llm_api_key: str = ""
    llm_model: str = "gemini-3.1-pro-preview-thinking"
    schedule_time: str = "09:00"  # 北京时间 HH:MM
