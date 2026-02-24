"""飞书推送模块：卡片构建与通知发送。"""

import logging
from datetime import datetime

from .models import Paper

logger = logging.getLogger(__name__)


def _topic_matches(topic: str, matched_topics: list[str]) -> bool:
    """模糊匹配：topic 或 matched_topic 互相包含即算匹配。"""
    for mt in matched_topics:
        if topic in mt or mt in topic:
            return True
    return False


def count_per_topic(papers: list[Paper], topics: list[str]) -> dict[str, int]:
    """统计每个 topic 下的相关论文数量（一篇论文可计入多个 topic）。"""
    counts: dict[str, int] = {}
    for topic in topics:
        counts[topic] = sum(1 for p in papers if _topic_matches(topic, p.matched_topics))
    return counts


def select_per_topic(papers: list[Paper], topics: list[str]) -> list[tuple[str, Paper]]:
    """每个 topic 选一篇最相关的论文，不重复。返回 (topic, paper) 列表。"""
    used_ids: set[str] = set()
    result: list[tuple[str, Paper]] = []
    for topic in topics:
        best: Paper | None = None
        for paper in papers:
            if paper.arxiv_id in used_ids:
                continue
            if not _topic_matches(topic, paper.matched_topics):
                continue
            if best is None or paper.relevance_score > best.relevance_score:
                best = paper
        if best:
            used_ids.add(best.arxiv_id)
            result.append((topic, best))
    return result


def build_feishu_card_content(
    selected: list[tuple[str, Paper]],
    report_date: datetime,
    total: int = 0,
    remaining: int = 0,
    topic_counts: dict[str, int] | None = None,
) -> dict:
    """构建飞书 Interactive Card dict，每个 topic 展示一篇。

    返回裸卡片 dict（含 config/header/elements），可直接传给 bot.reply_card()。
    """
    date_str = report_date.strftime("%Y-%m-%d")
    elements: list[dict] = []

    summary = f"共筛选出 **{total}** 篇相关论文。\n"
    if topic_counts:
        for topic, count in topic_counts.items():
            summary += f"- {topic}: {count} 篇\n"

    elements.append({
        "tag": "markdown",
        "content": summary,
    })
    elements.append({"tag": "hr"})

    for topic, paper in selected:
        topics_str = ", ".join(paper.matched_topics) if paper.matched_topics else topic
        content = (
            f"📌 **{topic}**\n"
            f"**{paper.title}**\n"
            f"**筛选理由:** {paper.relevance_reason}\n"
            f"**关联主题:** {topics_str}\n"
            f"**摘要:** {paper.summary_zh}\n"
            f"[查看原文]({paper.entry_url})"
        )
        elements.append({"tag": "markdown", "content": content})
        elements.append({"tag": "hr"})

    if remaining > 0:
        elements.append({
            "tag": "markdown",
            "content": f"还有 {remaining} 篇相关论文，请查看完整日报文件。",
        })

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"ArXiv 日报 - {date_str}"},
            "template": "blue",
        },
        "elements": elements,
    }
