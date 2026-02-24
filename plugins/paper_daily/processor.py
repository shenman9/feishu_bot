"""业务逻辑层：论文筛选与摘要生成，支持增量缓存。"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .config import AppConfig
from .llm_client import LLMClient, LLMError
from .models import Paper

logger = logging.getLogger(__name__)

_PLUGIN_DIR = Path(__file__).parent

FILTER_SYSTEM = """你是一个学术论文筛选助手。判断论文是否与用户关注的研究主题相关。

用户关注的主题：
{topics}

回复要求：
- 仅输出一行合法 JSON，不要用 markdown code block 包裹，不要输出任何其他文字。
- JSON 中的字符串值不得包含换行符。
- 格式：{{"relevant": true/false, "score": 1-5, "matched_topics": ["匹配的主题1"], "reason": "一句话中文理由"}}

字段说明：
- score：1=几乎无关, 2=略有关联, 3=一般相关, 4=比较相关, 5=高度相关。仅当 score >= 3 时 relevant 应为 true。
- matched_topics：从上述主题列表中选取与论文相关的主题，无关则为空列表。"""

FILTER_USER = """论文标题：{title}
分类：{categories}
摘要：{abstract}"""

SUMMARY_SYSTEM = """你是一个学术论文摘要助手。请用中文为论文生成简明摘要。

回复要求：
- 直接输出中文摘要纯文本，不要输出 JSON、markdown 或任何格式包裹。
- 摘要控制在150字以内，涵盖研究动机、方法和主要发现。"""

SUMMARY_USER = """论文标题：{title}
作者：{authors}
分类：{categories}
原文摘要：{abstract}"""


def process_papers(
    papers: list[Paper],
    config: AppConfig,
    llm: LLMClient,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> list[Paper]:
    """主流程：逐篇筛选，通过后立即生成摘要并更新报告。支持断点续跑。"""
    cache = _load_cache()
    relevant: list[Paper] = []

    # 先恢复缓存中已完成的论文（筛选+摘要都有的）
    for paper in papers:
        cached = cache.get(paper.arxiv_id, {})
        if "filter" in cached and "summary" in cached:
            _restore_filter(paper, cached["filter"])
            _restore_summary(paper, cached["summary"])
            if paper.is_relevant:
                relevant.append(paper)

    if relevant:
        logger.info(f"从缓存恢复 {len(relevant)} 篇已完成的相关论文")

    for i, paper in enumerate(papers):
        cached = cache.get(paper.arxiv_id, {})

        # 筛选阶段
        if "filter" in cached:
            _restore_filter(paper, cached["filter"])
            logger.info(f"筛选 [{i+1}/{len(papers)}]: {paper.title[:60]} (缓存)")
        else:
            logger.info(f"筛选 [{i+1}/{len(papers)}]: {paper.title[:60]}")
            _filter_paper(paper, config, llm)
            _save_to_cache(cache, paper, "filter")

        if not paper.is_relevant:
            # 每 10 篇汇报一次进度
            if progress_callback and (i + 1) % 10 == 0:
                progress_callback(
                    f"筛选进度: {i+1}/{len(papers)}\n"
                    f"已发现 {len(relevant)} 篇相关论文"
                )
            continue

        # 摘要阶段：筛选通过后立即生成
        if "summary" in cached:
            _restore_summary(paper, cached["summary"])
            logger.info(f"摘要: {paper.title[:60]} (缓存)")
        else:
            logger.info(f"摘要: {paper.title[:60]}")
            _summarize_paper(paper, config, llm)
            _save_to_cache(cache, paper, "summary")
            if paper.arxiv_id not in {p.arxiv_id for p in relevant}:
                relevant.append(paper)

            # 每完成一篇就更新报告
            _update_reports(relevant, config.topics)

        # 发现新相关论文时汇报进度
        if progress_callback:
            progress_callback(
                f"筛选进度: {i+1}/{len(papers)}\n"
                f"已发现 {len(relevant)} 篇相关论文"
            )

    relevant.sort(key=lambda p: p.relevance_score, reverse=True)
    return relevant


def _update_reports(relevant: list[Paper], topics: list[str] | None = None) -> None:
    """增量更新报告文件。"""
    try:
        from .reporter import generate_reports
        sorted_papers = sorted(relevant, key=lambda p: p.relevance_score, reverse=True)
        today = datetime.now(timezone.utc)
        generate_reports(
            sorted_papers, today, topics=topics,
            template_dir=str(_PLUGIN_DIR / "templates"),
            output_base=str(_PLUGIN_DIR / "reports"),
        )
    except Exception as e:
        logger.warning(f"增量更新报告失败: {e}")


def _cache_path() -> str:
    """当天缓存文件路径（使用插件目录下的 cache 子目录）。"""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cache_dir = _PLUGIN_DIR / "cache" / date_str
    cache_dir.mkdir(parents=True, exist_ok=True)
    return str(cache_dir / "papers.json")


def _load_cache() -> dict:
    """加载缓存，返回 {arxiv_id: {...}} 字典。"""
    path = _cache_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_to_cache(cache: dict, paper: Paper, stage: str) -> None:
    """将单篇论文的处理结果写入缓存。"""
    if paper.arxiv_id not in cache:
        cache[paper.arxiv_id] = {}
    if stage == "filter":
        cache[paper.arxiv_id]["filter"] = {
            "is_relevant": paper.is_relevant,
            "relevance_score": paper.relevance_score,
            "matched_topics": paper.matched_topics,
            "relevance_reason": paper.relevance_reason,
        }
    elif stage == "summary":
        cache[paper.arxiv_id]["summary"] = {
            "summary_zh": paper.summary_zh,
        }
    try:
        with open(_cache_path(), "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning(f"缓存写入失败: {e}")


def _restore_filter(paper: Paper, data: dict) -> None:
    """从缓存恢复筛选结果。"""
    paper.is_relevant = data.get("is_relevant", False)
    paper.relevance_score = data.get("relevance_score", 0)
    paper.matched_topics = data.get("matched_topics", [])
    paper.relevance_reason = data.get("relevance_reason", "")


def _restore_summary(paper: Paper, data: dict) -> None:
    """从缓存恢复摘要结果。"""
    paper.summary_zh = data.get("summary_zh", "")


def _filter_paper(paper: Paper, config: AppConfig, llm: LLMClient) -> None:
    """调用 LLM 判断单篇论文相关性。"""
    system = FILTER_SYSTEM.format(
        topics="\n".join(f"- {t}" for t in config.topics),
    )
    user_msg = FILTER_USER.format(
        title=paper.title,
        categories=", ".join(paper.categories),
        abstract=paper.abstract,
    )
    resp = ""
    try:
        resp = llm.chat(system, user_msg, max_tokens=200)
        data = _parse_json(resp)
        paper.is_relevant = data.get("relevant", False)
        paper.relevance_score = int(data.get("score", 0))
        paper.matched_topics = data.get("matched_topics", [])
        paper.relevance_reason = data.get("reason", "")
    except (LLMError, ValueError) as e:
        logger.warning(f"筛选失败，跳过 [{paper.arxiv_id}]: {e}")
        logger.debug(f"筛选 LLM 原始返回 [{paper.arxiv_id}]:\n{resp}")


def _summarize_paper(paper: Paper, config: AppConfig, llm: LLMClient) -> None:
    """调用 LLM 生成中文摘要。"""
    user_msg = SUMMARY_USER.format(
        title=paper.title,
        authors=", ".join(paper.authors[:5]),
        categories=", ".join(paper.categories),
        abstract=paper.abstract,
    )
    try:
        resp = llm.chat(SUMMARY_SYSTEM, user_msg, max_tokens=1000)
        paper.summary_zh = resp.strip()
    except LLMError as e:
        logger.warning(f"摘要失败 [{paper.arxiv_id}]: {e}")


def _parse_json(text: str) -> dict:
    """解析 JSON，支持从 markdown code block 中提取，并容错处理。"""
    # 尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 尝试从 code block 中提取
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    # 尝试提取第一个 {...} 块
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    # 清理换行符后再试
    cleaned = re.sub(r"\n\s*", " ", text)
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    # 最后尝试：修复值内未转义的双引号
    try:
        return json.loads(_fix_unescaped_quotes(text))
    except json.JSONDecodeError:
        pass
    raise ValueError(f"无法解析 JSON: {text[:100]}")


def _fix_unescaped_quotes(text: str) -> str:
    """修复 JSON 字符串值内部未转义的双引号。

    逐字符扫描，区分 JSON 结构性引号和值内的裸引号，
    将后者替换为转义形式。
    """
    result = []
    i = 0
    in_string = False
    while i < len(text):
        ch = text[i]
        if ch == '\\' and in_string:
            # 转义序列，原样保留
            result.append(text[i:i+2])
            i += 2
            continue
        if ch == '"':
            if not in_string:
                in_string = True
                result.append(ch)
            else:
                # 判断这个引号是结构性的（关闭字符串）还是值内的裸引号
                # 结构性引号后面应该是 : , } ] 或空白
                rest = text[i+1:].lstrip()
                if not rest or rest[0] in ':,}]':
                    in_string = False
                    result.append(ch)
                else:
                    result.append('\\"')
        else:
            result.append(ch)
        i += 1
    return ''.join(result)
