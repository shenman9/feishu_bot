"""论文数据模型。"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Paper:
    """一篇 ArXiv 论文。基础字段由 fetcher 填充，筛选/摘要/标签字段由 processor 填充。"""

    # 基础字段
    arxiv_id: str
    title: str
    authors: list[str]
    abstract: str
    categories: list[str]
    primary_category: str
    published: datetime
    pdf_url: str
    entry_url: str

    # 筛选后填充
    is_recommended: bool = False
    relevance_score: int = 0          # 1-5，0 表示未筛选
    relevance_reason: str = ""

    # 摘要后填充
    summary_zh: str = ""

    # 标签归类后填充
    tags: list[str] = field(default_factory=list)
