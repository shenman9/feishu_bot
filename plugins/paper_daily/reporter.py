"""生成 Markdown 和 HTML 格式日报。"""

import os
import logging
from datetime import datetime

from jinja2 import Environment, FileSystemLoader

from .models import Paper

logger = logging.getLogger(__name__)


def generate_reports(
    papers: list[Paper],
    report_date: datetime,
    template_dir: str = "templates",
    output_base: str = "reports",
    topics: list[str] | None = None,
) -> tuple[str, str]:
    """生成 Markdown 和 HTML 报告，返回 (md_path, html_path)。"""
    date_str = report_date.strftime("%Y-%m-%d")
    out_dir = os.path.join(output_base, date_str)
    os.makedirs(out_dir, exist_ok=True)

    md_content = _build_markdown(papers, date_str, topics)
    md_path = os.path.join(out_dir, "paper_daily.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    html_content = _build_html(papers, date_str, template_dir)
    html_path = os.path.join(out_dir, "paper_daily.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    logger.info(f"报告已生成: {out_dir}")
    return md_path, html_path


def _build_markdown(papers: list[Paper], date_str: str, topics: list[str] | None = None) -> str:
    """构建 Markdown 日报内容。"""
    lines = [f"# ArXiv 日报 - {date_str}\n"]
    lines.append(f"共筛选出 **{len(papers)}** 篇相关论文。\n")
    if topics:
        for topic in topics:
            count = sum(1 for p in papers if topic in p.matched_topics or any(topic in mt or mt in topic for mt in p.matched_topics))
            lines.append(f"- {topic}: {count} 篇")
        lines.append("")

    for i, p in enumerate(papers, 1):
        stars = "★" * p.relevance_score + "☆" * (5 - p.relevance_score)
        lines.append("---\n")
        lines.append(f"## {i}. [{p.title}]({p.entry_url})\n")
        lines.append(f"- **相关度**: {stars} ({p.relevance_score}/5)")
        if p.matched_topics:
            lines.append(f"- **关联主题**: {' / '.join(p.matched_topics)}")
        lines.append(f"- **筛选理由**: {p.relevance_reason}\n")
        lines.append(f"### 中文摘要\n{p.summary_zh}\n")

    return "\n".join(lines)


def _build_html(papers: list[Paper], date_str: str, template_dir: str) -> str:
    """用 Jinja2 模板渲染 HTML 日报。"""
    env = Environment(loader=FileSystemLoader(template_dir), autoescape=True)
    template = env.get_template("report.html.j2")
    return template.render(papers=papers, report_date=date_str)
