"""论文数据模型。"""

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Paper:
    """一篇 ArXiv 论文。基础字段由 fetcher 填充，筛选/摘要字段由 processor 填充。"""

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
    aspect: str = ""                  # LLM 归纳的关联方向（如"KV Cache压缩"）
    relevance_reason: str = ""

    # 摘要后填充
    summary_zh: str = ""
