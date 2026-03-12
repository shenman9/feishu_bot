"""论文筛选、摘要与标签处理模块。

包含：
- 批量论文筛选（每次 10 篇，减少 API 调用次数约 10 倍）
- 相关论文摘要生成（逐篇）
- 标签归类（摘要生成后，一次 LLM 调用统一打标签）
- 标签词表管理（跨天复用已有标签）
- 按日期缓存（只在成功时写缓存，避免失败结果污染缓存）
- 自动清理过期缓存（保留最近 7 天）
"""

import json
import logging
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Optional

from .config import AppConfig
from .gemini_client import call_gemini, GeminiError
from .models import Paper

logger = logging.getLogger(__name__)

_PLUGIN_DIR = Path(__file__).parent


# ---------- Prompt 模板 ----------

BATCH_SIZE = 10  # 每批筛选的论文数

_BATCH_FILTER_PROMPT = """\
你是论文推荐助手。根据研究者的兴趣描述，判断以下 {count} 篇论文是否值得推荐给该研究者阅读。

研究者的兴趣：
{research_interest}

待筛选论文：
{papers_block}

请输出一个 JSON 数组（不要 markdown 代码块），每个元素对应一篇论文，按编号顺序排列。
推荐的论文（score >= 3）需要给出 reason，不推荐的只需 id 和 score：
[
  {{"id": 1, "recommend": true, "score": 4, "reason": "推荐理由"}},
  {{"id": 2, "recommend": false, "score": 1}},
  ...
]

字段说明：
- score：1=无关, 2=略沾边, 3=值得一读, 4=推荐阅读, 5=强烈推荐
- reason：一句话推荐理由
- score >= 3 时 recommend 应为 true
- 数组长度必须等于 {count}\
"""

_SUMMARY_PROMPT = """\
请为以下论文生成 150 字以内的中文摘要，涵盖研究动机、方法和主要发现。
直接输出摘要纯文本，不要 JSON、markdown 或任何格式包裹。

标题：{title}
作者：{authors}
分类：{categories}
原文摘要：{abstract}\
"""

_TAG_PROMPT = """\
你是论文标签助手。请为以下推荐论文分配研究方向标签。

## 标签定义规范

1. **简洁命名**：每个标签 2-8 个中文字符（如"KV Cache 优化"、"Agent 记忆"、"推理加速"）。
2. **代表性**：标签应概括一类研究的核心方向，而非描述某一篇具体论文的内容。
3. **区分度**：标签之间语义不得重叠。若两个标签含义相近，必须合并为一个。
4. **粒度适中**：
   - 太粗 ✗：「深度学习」「人工智能」→ 无区分价值
   - 太细 ✗：「基于注意力分数的 KV Cache 动态淘汰策略」→ 只能匹配单篇论文
   - 合适 ✓：「KV Cache 优化」「稀疏注意力」「Agent 架构」
5. **参考已有标签**：下方提供了常用标签词表供参考。若已有标签能准确描述论文方向，直接使用；若已有标签不够准确或粒度不合适，应创建更合适的新标签，不必迁就已有标签。
6. **按需分配**：每篇论文可分配多个标签，涵盖其涉及的所有相关方向。仅分配确实相关的标签，不必凑数，也不要遗漏。

## 已有标签词表
{existing_tags}

## 研究者关注方向（供参考）
{research_interest}

## 待分类论文
{papers}

## 输出要求
请严格输出以下 JSON 格式，不要包含其他内容：
{{
  "mapping": {{
    "论文arxiv_id": ["标签1", "标签2"],
    "论文arxiv_id": ["标签1"],
    ...
  }}
}}\
"""


# ---------- 缓存工具 ----------

CACHE_RETENTION_DAYS = 180  # 缓存保留天数


def _cache_path(date_str: str | None = None) -> Path:
    """返回当天缓存文件路径（插件目录下 cache/YYYY-MM-DD/papers.json）。"""
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cache_dir = _PLUGIN_DIR / "cache" / date_str
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / "papers.json"


def _load_cache(date_str: str | None = None) -> dict:
    path = _cache_path(date_str)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"缓存读取失败，忽略: {e}")
        return {}


def _save_cache(cache: dict, date_str: str | None = None) -> None:
    try:
        _cache_path(date_str).write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as e:
        logger.warning(f"缓存写入失败: {e}")


def _cleanup_old_cache() -> None:
    """清理过期缓存（保留最近 CACHE_RETENTION_DAYS 天）。"""
    cache_root = _PLUGIN_DIR / "cache"
    if not cache_root.exists():
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=CACHE_RETENTION_DAYS)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    deleted_count = 0
    for date_dir in cache_root.iterdir():
        if not date_dir.is_dir():
            continue
        # 目录名格式：YYYY-MM-DD
        if date_dir.name < cutoff_str:
            try:
                for file in date_dir.iterdir():
                    file.unlink()
                date_dir.rmdir()
                deleted_count += 1
                logger.debug(f"清理过期缓存: {date_dir.name}")
            except OSError as e:
                logger.warning(f"清理缓存失败 [{date_dir.name}]: {e}")

    if deleted_count > 0:
        logger.info(f"缓存清理完成: 删除 {deleted_count} 个过期目录（保留最近 {CACHE_RETENTION_DAYS} 天）")


# ---------- 标签词表管理 ----------

MAX_TAGS_IN_PROMPT = 30  # 传给 LLM 的最大标签数（按使用频率取 top N）


def _tags_path() -> Path:
    """返回标签词表文件路径（插件目录下 tags.json）。"""
    return _PLUGIN_DIR / "tags.json"


def _load_tags() -> dict[str, dict]:
    """加载标签词表，返回 {标签名: {"count": N, "last_used": "YYYY-MM-DD"}}。"""
    path = _tags_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("tags", {})
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"标签词表读取失败，忽略: {e}")
        return {}


def _save_tags(tags: dict[str, dict]) -> None:
    """保存标签词表（按 count 降序）。"""
    sorted_tags = dict(sorted(tags.items(), key=lambda x: x[1].get("count", 0), reverse=True))
    try:
        _tags_path().write_text(
            json.dumps({"tags": sorted_tags}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        logger.warning(f"标签词表写入失败: {e}")


def _top_tags_for_prompt(tags: dict[str, dict]) -> str:
    """取使用频率最高的 top N 个标签，格式化为 prompt 文本。"""
    if not tags:
        return "（暂无，请自由创建）"
    # 按 count 降序排列，取前 MAX_TAGS_IN_PROMPT 个
    sorted_items = sorted(tags.items(), key=lambda x: x[1].get("count", 0), reverse=True)
    top = [name for name, _ in sorted_items[:MAX_TAGS_IN_PROMPT]]
    return "、".join(top)


# ---------- JSON 解析工具 ----------

def _parse_json(text: str) -> dict | list:
    """从 LLM 响应中提取 JSON（对象或数组），容错处理 markdown 包裹和换行符。"""
    # 直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 从 markdown code block 提取
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 提取第一个 [...] 块（数组）
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    # 提取第一个 {...} 块（对象）
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    # 清理换行后再试
    cleaned = re.sub(r"\n\s*", " ", text)
    m = re.search(r"\[.*\]", cleaned)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    raise ValueError(f"无法解析 JSON: {text[:150]}")


# ---------- 标签归类 ----------

def _assign_tags(
    papers: list[Paper],
    config: AppConfig,
    cache: dict,
    progress_callback: Optional[Callable[[str], None]] = None,
    date_str: str | None = None,
) -> None:
    """对推荐论文统一打标签（一次 LLM 调用），并更新标签词表和缓存。

    基于 title + reason + summary_zh 进行归类，放在摘要生成之后执行。
    """
    # 筛选需要打标签的论文（推荐且缓存中无 tags）
    need_tags = [p for p in papers if p.is_recommended
                 and "tags" not in cache.get(p.arxiv_id, {})]

    if not need_tags:
        logger.info("标签归类: 所有推荐论文已有缓存标签，跳过")
        return

    if progress_callback:
        progress_callback(
            f"正在为 {len(need_tags)} 篇推荐论文打标签..."
        )

    # 构造论文列表文本
    papers_text = ""
    for paper in need_tags:
        papers_text += (
            f"\n- arxiv_id: {paper.arxiv_id}\n"
            f"  标题: {paper.title}\n"
            f"  推荐理由: {paper.relevance_reason}\n"
            f"  摘要: {paper.summary_zh or '（无摘要）'}\n"
        )

    existing_tags = _load_tags()
    existing_tags_str = _top_tags_for_prompt(existing_tags)

    prompt = _TAG_PROMPT.format(
        existing_tags=existing_tags_str,
        research_interest=config.research_interest,
        papers=papers_text,
    )

    try:
        resp = call_gemini(
            config.llm_api_key, config.llm_model, prompt,
            base_url=config.llm_base_url,
        )
        result = _parse_json(resp)
        if not isinstance(result, dict):
            raise ValueError(f"期望 JSON 对象，实际: {type(result).__name__}")

        mapping = result.get("mapping", {})
        tag_date_str = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # 收集本次所有标签
        all_tags_this_run: set[str] = set()

        for paper in need_tags:
            paper_tags = mapping.get(paper.arxiv_id, [])
            if not isinstance(paper_tags, list):
                paper_tags = []
            paper.tags = paper_tags
            all_tags_this_run.update(paper_tags)

            cache.setdefault(paper.arxiv_id, {})["tags"] = paper_tags
            logger.info(f"  标签: {paper.title[:40]} → {paper_tags}")

        _save_cache(cache, date_str)
        new_count = 0
        for tag_name in all_tags_this_run:
            if tag_name in existing_tags:
                existing_tags[tag_name]["count"] = existing_tags[tag_name].get("count", 0) + 1
                existing_tags[tag_name]["last_used"] = tag_date_str
            else:
                existing_tags[tag_name] = {"count": 1, "last_used": tag_date_str}
                new_count += 1

        _save_tags(existing_tags)
        if new_count:
            logger.info(f"标签词表更新: 新增 {new_count} 个标签，总计 {len(existing_tags)} 个")

        logger.info(f"标签归类完成: {len(need_tags)} 篇论文")

    except (GeminiError, ValueError) as e:
        logger.warning(f"标签归类失败: {e}")
        # 标签归类失败不影响日报推送，论文仍可无标签展示


# ---------- 主处理流程 ----------

def process_papers(
    papers: list[Paper],
    config: AppConfig,
    progress_callback: Optional[Callable[[str], None]] = None,
    date_str: str | None = None,
) -> list[Paper]:
    """批量筛选论文 → 生成摘要 → 标签归类。支持断点续跑。

    Args:
        papers: 待处理的论文列表。
        config: 插件配置（含 LLM 设置和关注主题）。
        progress_callback: 进度汇报回调，每批完成后触发。
        date_str: 缓存日期（YYYY-MM-DD），None 时取 UTC 当天。

    Returns:
        筛选出的相关论文列表，按相关性分数倒序排列。
    """
    # 清理过期缓存
    _cleanup_old_cache()

    cache = _load_cache(date_str)
    total = len(papers)

    # ---- 阶段 1：分离已缓存和未缓存的论文 ----
    uncached: list[Paper] = []
    cached_relevant_count = 0
    for paper in papers:
        c = cache.get(paper.arxiv_id, {})
        if "filter" in c:
            _restore_from_cache(paper, c)
            if paper.is_recommended:
                cached_relevant_count += 1
        else:
            uncached.append(paper)

    if cached_relevant_count:
        logger.info(f"从缓存恢复 {cached_relevant_count} 篇推荐论文，"
                    f"还有 {len(uncached)} 篇待筛选")

    # ---- 阶段 2：批量筛选未缓存的论文 ----
    total_batches = (len(uncached) + BATCH_SIZE - 1) // BATCH_SIZE if uncached else 0

    for batch_idx in range(total_batches):
        start = batch_idx * BATCH_SIZE
        batch = uncached[start:start + BATCH_SIZE]

        logger.info(f"批量筛选 [{batch_idx+1}/{total_batches}]: "
                    f"第 {start+1}-{start+len(batch)} 篇（共 {len(uncached)} 篇待筛选）")

        # 构造论文列表文本
        papers_block = ""
        for j, paper in enumerate(batch):
            papers_block += (
                f"\n[{j+1}] 标题：{paper.title}\n"
                f"    分类：{', '.join(paper.categories)}\n"
                f"    摘要：{paper.abstract}\n"
            )

        prompt = _BATCH_FILTER_PROMPT.format(
            count=len(batch),
            research_interest=config.research_interest,
            papers_block=papers_block,
        )

        try:
            resp = call_gemini(
                config.llm_api_key, config.llm_model, prompt,
                base_url=config.llm_base_url,
            )
            results = _parse_json(resp)
            if not isinstance(results, list):
                raise ValueError(f"期望 JSON 数组，实际: {type(results).__name__}")

            # 按 id 字段建索引（兼容 id 从 1 开始或从 0 开始）
            results_by_id = {}
            for r in results:
                rid = r.get("id", 0)
                results_by_id[rid] = r

            for j, paper in enumerate(batch):
                # 尝试匹配 id（1-based 优先，0-based 兜底，顺序兜底）
                result = results_by_id.get(j + 1) or results_by_id.get(j)
                if result is None and j < len(results):
                    result = results[j]
                if result is None:
                    logger.warning(f"批量筛选结果缺少第 {j+1} 篇: {paper.arxiv_id}")
                    continue

                paper.is_recommended = bool(result.get("recommend", False))
                paper.relevance_score = int(result.get("score", 0))
                paper.relevance_reason = result.get("reason", "")

                cache.setdefault(paper.arxiv_id, {})["filter"] = {
                    "is_recommended": paper.is_recommended,
                    "relevance_score": paper.relevance_score,
                    "relevance_reason": paper.relevance_reason,
                }

                if paper.is_recommended:
                    logger.info(f"  [{j+1}/{len(batch)}] ★推荐 score={paper.relevance_score}"
                                f" {paper.title[:45]} | {paper.relevance_reason[:30]}")
                else:
                    logger.info(f"  [{j+1}/{len(batch)}] 跳过 score={paper.relevance_score}"
                                f" {paper.title[:50]}")

            _save_cache(cache, date_str)

        except (GeminiError, ValueError) as e:
            logger.warning(f"批量筛选失败（第 {batch_idx+1} 批），跳过: {e}")

        # 汇报进度
        if progress_callback:
            done = start + len(batch)
            relevant_so_far = sum(1 for p in papers if p.is_recommended)
            progress_callback(
                f"筛选进度: {total - len(uncached) + done}/{total}\n"
                f"已发现 {relevant_so_far} 篇推荐论文"
            )

    # ---- 阶段 3：对推荐论文逐篇生成摘要 ----
    need_summary = [p for p in papers if p.is_recommended
                    and not cache.get(p.arxiv_id, {}).get("summary")]

    if need_summary:
        logger.info(f"开始生成摘要: {len(need_summary)} 篇")

    for i, paper in enumerate(need_summary):
        logger.info(f"生成摘要 [{i+1}/{len(need_summary)}]: {paper.title[:55]}")
        prompt = _SUMMARY_PROMPT.format(
            title=paper.title,
            authors=", ".join(paper.authors[:5]),
            categories=", ".join(paper.categories),
            abstract=paper.abstract,
        )
        try:
            paper.summary_zh = call_gemini(
                config.llm_api_key, config.llm_model, prompt,
                base_url=config.llm_base_url,
            )
            cache.setdefault(paper.arxiv_id, {})["summary"] = paper.summary_zh
            _save_cache(cache, date_str)
        except GeminiError as e:
            logger.warning(f"摘要生成失败 [{paper.arxiv_id}]: {e}")
            paper.summary_zh = "（摘要生成失败）"

        if progress_callback:
            progress_callback(
                f"摘要生成: {i+1}/{len(need_summary)}\n"
                f"共 {sum(1 for p in papers if p.is_recommended)} 篇推荐论文"
            )

    # 恢复有缓存摘要的论文
    for paper in papers:
        if paper.is_recommended and not paper.summary_zh:
            c = cache.get(paper.arxiv_id, {})
            if "summary" in c:
                paper.summary_zh = c["summary"]

    # ---- 阶段 4：标签归类 ----
    _assign_tags(
        papers, config, cache,
        progress_callback=progress_callback,
        date_str=date_str,
    )

    # 恢复有缓存标签的论文
    for paper in papers:
        if paper.is_recommended and not paper.tags:
            c = cache.get(paper.arxiv_id, {})
            if "tags" in c:
                paper.tags = c["tags"]

    # 收集并排序
    relevant = [p for p in papers if p.is_recommended]
    relevant.sort(key=lambda p: p.relevance_score, reverse=True)
    logger.info(f"处理完成: {total} 篇中推荐 {len(relevant)} 篇")
    return relevant


def _restore_from_cache(paper: Paper, c: dict) -> None:
    """从缓存字典中恢复 Paper 的筛选、摘要和标签字段。"""
    if "filter" in c:
        f = c["filter"]
        paper.is_recommended = f.get("is_recommended", False)
        paper.relevance_score = f.get("relevance_score", 0)
        paper.relevance_reason = f.get("relevance_reason", "")
    if "summary" in c:
        paper.summary_zh = c["summary"]
    if "tags" in c:
        paper.tags = c["tags"]
