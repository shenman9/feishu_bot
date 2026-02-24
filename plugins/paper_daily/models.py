"""论文数据模型。"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Paper:
    """贯穿整个流水线的论文数据结构。"""

    arxiv_id: str
    title: str
    authors: list[str]
    abstract: str
    categories: list[str]
    primary_category: str
    published: datetime
    pdf_url: str
    entry_url: str
    # LLM 筛选后填充
    is_relevant: bool = False
    relevance_reason: str = ""
    relevance_score: int = 0
    matched_topics: list[str] = field(default_factory=list)
    # LLM 摘要后填充
    summary_zh: str = ""
