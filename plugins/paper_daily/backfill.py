"""论文日报历史回溯脚本。

独立于飞书 Bot 运行，根据指定日期获取 ArXiv 论文并执行筛选、摘要、标签全流程。
结果缓存在 cache/{YYYY-MM-DD}/papers.json。

用法:
    python -m plugins.paper_daily.backfill 20260302
"""

import logging
import sys
from datetime import datetime, timezone

from config import load_plugin_config
from .config import AppConfig
from .fetcher import fetch_papers
from .processor import process_papers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _build_config() -> AppConfig:
    """从 config/paper_daily.yaml 加载配置并构造 AppConfig。"""
    raw = load_plugin_config("paper_daily")
    return AppConfig(
        research_interest=raw.get("research_interest", ""),
        categories=raw.get("categories", ["cs.CL", "cs.AI", "cs.LG"]),
        max_papers=raw.get("max_papers", 200),
        llm_base_url=raw.get("llm_base_url", "https://api.lingyaai.cn"),
        llm_api_key=raw.get("llm_api_key", ""),
        llm_model=raw.get("llm_model", "gemini-3.1-pro-preview-thinking"),
    )


def main() -> None:
    if len(sys.argv) != 2:
        print(f"用法: python -m plugins.paper_daily.backfill YYYYMMDD")
        sys.exit(1)

    date_arg = sys.argv[1]
    try:
        target_date = datetime.strptime(date_arg, "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        print(f"日期格式错误: {date_arg!r}，请使用 YYYYMMDD 格式（如 20260302）")
        sys.exit(1)

    date_str = target_date.strftime("%Y-%m-%d")
    logger.info(f"===== 论文日报回溯: {date_str} =====")

    cfg = _build_config()
    if not cfg.llm_api_key:
        logger.error("缺少 llm_api_key，请检查 config/paper_daily.yaml")
        sys.exit(1)

    # 获取论文
    papers = fetch_papers(cfg, target_date=target_date)
    if not papers:
        logger.info(f"{date_str}: 未获取到论文（该日期可能无 ArXiv 更新）")
        return

    papers = papers[:cfg.max_papers]
    logger.info(f"{date_str}: 获取到 {len(papers)} 篇论文，开始处理...")

    # 筛选 + 摘要 + 标签（缓存写入对应日期目录）
    relevant = process_papers(papers, cfg, date_str=date_str)

    logger.info(
        f"===== {date_str} 完成: "
        f"总计 {len(papers)} 篇, 推荐 {len(relevant)} 篇 ====="
    )


if __name__ == "__main__":
    main()
