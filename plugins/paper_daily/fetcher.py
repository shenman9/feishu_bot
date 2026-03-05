"""从 ArXiv 获取论文元数据。"""

import logging
from datetime import datetime, timedelta, timezone

import arxiv

from .config import AppConfig
from .models import Paper

logger = logging.getLogger(__name__)


def fetch_papers(config: AppConfig, target_date: datetime | None = None) -> list[Paper]:
    """获取指定日期的 ArXiv 论文（默认昨天），按分类过滤并去重。

    Args:
        config: 插件配置，使用 categories 字段。
        target_date: 目标日期，None 时取昨天（UTC）。

    Returns:
        去重后的 Paper 列表，按发布时间倒序。
    """
    if target_date is None:
        target_date = datetime.now(timezone.utc) - timedelta(days=1)

    date_str = target_date.strftime("%Y-%m-%d")
    query = " OR ".join(f"cat:{c}" for c in config.categories)
    logger.info(f"ArXiv 查询: query={query!r}, 目标日期={date_str}")

    date_min = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
    date_max = date_min + timedelta(days=1)

    search = arxiv.Search(
        query=query,
        max_results=500,
        sort_by=arxiv.SortCriterion.SubmittedDate,
        sort_order=arxiv.SortOrder.Descending,
    )

    seen: set[str] = set()
    papers: list[Paper] = []

    client = arxiv.Client()
    for result in client.results(search):
        if result.published < date_min:
            # 结果按时间倒序，遇到更早的日期即可停止
            break
        if result.published >= date_max:
            continue

        arxiv_id = result.entry_id.split("/abs/")[-1]
        if arxiv_id in seen:
            continue
        seen.add(arxiv_id)

        papers.append(Paper(
            arxiv_id=arxiv_id,
            title=result.title.replace("\n", " ").strip(),
            authors=[a.name for a in result.authors],
            abstract=result.summary.replace("\n", " ").strip(),
            categories=result.categories,
            primary_category=result.primary_category,
            published=result.published,
            pdf_url=result.pdf_url or "",
            entry_url=result.entry_id,
        ))

    logger.info(f"ArXiv 获取完成: 共 {len(papers)} 篇（{date_str}，去重后）")
    return papers
