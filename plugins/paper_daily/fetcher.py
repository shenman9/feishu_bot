"""从 ArXiv 获取论文元数据。"""

import logging
from datetime import datetime, timedelta, timezone

import arxiv

from .config import AppConfig
from .models import Paper

logger = logging.getLogger(__name__)


def fetch_papers(config: AppConfig, target_date: datetime | None = None) -> list[Paper]:
    """获取指定日期、指定分类的 ArXiv 论文，按 arxiv_id 去重。

    当 target_date 为 None 时默认取昨天。
    """
    if target_date is None:
        target_date = datetime.now(timezone.utc) - timedelta(days=1)

    query = _build_query(config.categories)
    return _fetch_for_date(query, target_date)


def _fetch_for_date(query: str, target_date: datetime) -> list[Paper]:
    """获取指定日期的论文。"""
    logger.info(f"查询 ArXiv: {query}, 筛选日期: {target_date.strftime('%Y-%m-%d')}")

    search = arxiv.Search(
        query=query,
        max_results=500,
        sort_by=arxiv.SortCriterion.SubmittedDate,
        sort_order=arxiv.SortOrder.Descending,
    )

    seen: set[str] = set()
    papers: list[Paper] = []
    date_min = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
    date_max = date_min + timedelta(days=1)

    client = arxiv.Client()
    for result in client.results(search):
        if result.published < date_min:
            break
        if result.published >= date_max:
            continue
        paper = _to_paper(result)
        if paper.arxiv_id in seen:
            continue
        seen.add(paper.arxiv_id)
        papers.append(paper)

    logger.info(f"获取到 {len(papers)} 篇论文（{date_min.strftime('%Y-%m-%d')} published，去重后）")
    return papers


def _build_query(categories: list[str]) -> str:
    """构建 ArXiv 查询字符串，如 'cat:cs.CL OR cat:cs.AI'。"""
    return " OR ".join(f"cat:{cat}" for cat in categories)


def _to_paper(result: arxiv.Result) -> Paper:
    """将 arxiv.Result 转换为 Paper 数据类。"""
    return Paper(
        arxiv_id=result.entry_id.split("/abs/")[-1],
        title=result.title.replace("\n", " ").strip(),
        authors=[a.name for a in result.authors],
        abstract=result.summary.replace("\n", " ").strip(),
        categories=result.categories,
        primary_category=result.primary_category,
        published=result.published,
        pdf_url=result.pdf_url,
        entry_url=result.entry_id,
    )
