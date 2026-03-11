"""
Claude Code 流式输出解析与工具调用日志渲染

解析 Claude Code CLI 的 stream-json 输出行，
将工具调用日志渲染为飞书卡片可用的 markdown 文本。
所有函数均为无状态纯函数。
"""

import json
from typing import Optional

from plugins.claude_code.constants import display_path

# 工具调用日志显示控制
_MAX_STREAK_DISPLAY = 15    # 连续工具调用段最多显示条数（超出则折叠）
_TOOL_PARAM_MAX = 60        # 工具参数摘要最大字符数

# 默认最大输出字符数（飞书卡片 markdown 上限）
DEFAULT_MAX_OUTPUT = 28000

# 工具名 → 展示图标
_TOOL_ICONS: dict[str, str] = {
    "Read":             "📖",
    "Write":            "✍️",
    "Edit":             "📝",
    "NotebookEdit":     "📓",
    "Bash":             "💻",
    "Glob":             "🔍",
    "Grep":             "🔍",
    "Task":             "🤖",
    "WebFetch":         "🌐",
    "AskUserQuestion":  "❓",
}


def format_tool_call(tool_name: str, tool_input: dict, working_dir: str = "") -> str:
    """将工具调用格式化为单行摘要，用于过程日志"""
    icon = _TOOL_ICONS.get(tool_name, "🔧")
    # AskUserQuestion 特殊处理：提取第一个问题文本作为摘要
    if tool_name == "AskUserQuestion":
        questions = tool_input.get("questions", [])
        param = questions[0].get("question", "") if questions else ""
        if len(param) > _TOOL_PARAM_MAX:
            param = param[:_TOOL_PARAM_MAX - 3] + "..."
        return f"{icon} {tool_name} `{param}`" if param else f"{icon} {tool_name}"
    # 按优先级提取最有意义的参数作为摘要
    param: str = (
        tool_input.get("file_path")
        or tool_input.get("notebook_path")
        or tool_input.get("command")
        or tool_input.get("pattern")
        or tool_input.get("path")
        or tool_input.get("description")
        or tool_input.get("url")
        or (str(next(iter(tool_input.values()))) if tool_input else "")
    )
    # 对路径类参数尝试缩短显示（非路径的 command/pattern 等调用后安全，因为非绝对路径时原样返回）
    param = display_path(param, working_dir)
    if len(param) > _TOOL_PARAM_MAX:
        param = param[:_TOOL_PARAM_MAX - 3] + "..."
    return f"{icon} {tool_name} `{param}`" if param else f"{icon} {tool_name}"


def parse_stream_line(
    line: str, has_previous_text: bool, working_dir: str = ""
) -> tuple[str, list[dict], str]:
    """解析 stream-json 单行

    Args:
        line: 一行 JSON 字符串
        has_previous_text: 之前是否已提取到 assistant 文本
        working_dir: 当前工作目录（用于路径缩短）

    Returns:
        (text_chunk, log_actions, meta_info) 三元组

        text_chunk: 本行提取到的 assistant 文字内容（空字符串表示无）

        log_actions: 工具调用日志动作列表，每项为：
            {"action": "add",    "line": str, "tool_use_id": str|None}  新增日志行
            {"action": "result", "tool_use_id": str, "is_error": bool, "summary": str}  更新结果

        meta_info: 统计信息字符串（来自 result 事件）
    """
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return ("", [], "")

    event_type = data.get("type", "")
    text_chunk = ""
    log_actions: list[dict] = []
    meta_info = ""

    if event_type == "assistant":
        # 处理 assistant 消息中的各类内容块
        message = data.get("message", {})
        content_blocks = message.get("content", [])
        text_parts = []
        for block in content_blocks:
            if isinstance(block, str):
                # /compact 等特殊指令的输出可能以纯字符串形式返回
                text_parts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "thinking":
                # 只显示图标，不展示原始思考内容；is_thinking 标记供主循环计算用时后更新
                log_actions.append({"action": "add", "line": "💭 思考...", "tool_use_id": None, "is_thinking": True})
            elif btype == "tool_use":
                tool_name = block.get("name", "Unknown")
                tool_input = block.get("input", {})
                tool_use_id = block.get("id", "")
                log_line = format_tool_call(tool_name, tool_input, working_dir)
                log_actions.append({"action": "add", "line": log_line, "tool_use_id": tool_use_id, "tool_name": tool_name})
        if text_parts:
            text_chunk = "\n\n".join(text_parts)

    elif event_type == "user":
        # 工具执行结果：追加 ✅/❌ 到对应日志行
        message = data.get("message", {})
        content_blocks = message.get("content", [])
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                tool_use_id = block.get("tool_use_id", "")
                is_error = bool(block.get("is_error", False))
                # content 可能是字符串或 content block 数组
                raw_content = block.get("content", "")
                if isinstance(raw_content, list):
                    raw_content = "\n".join(
                        b.get("text", "") for b in raw_content if b.get("type") == "text"
                    )
                summary = ""
                if is_error and raw_content:
                    # 错误时取首行，最多60字
                    first_line = str(raw_content).split("\n")[0].strip()
                    summary = first_line[:_TOOL_PARAM_MAX]
                log_actions.append({
                    "action": "result",
                    "tool_use_id": tool_use_id,
                    "is_error": is_error,
                    "summary": summary,
                })

    elif event_type == "system":
        # system 事件：处理 compact 等子类型
        subtype = data.get("subtype", "")
        if subtype == "compact_boundary":
            meta = data.get("compact_metadata", {})
            pre = meta.get("pre_tokens", 0)
            text_chunk = f"上下文已压缩（压缩前 {pre:,} tokens）" if pre else "上下文已压缩"

    elif event_type == "result":
        # result 事件：提取统计信息
        cost = data.get("cost_usd", 0)
        duration = data.get("duration_ms", 0)
        turns = data.get("num_turns", 0)
        session_id = data.get("session_id", "")
        meta_parts = []
        if cost:
            meta_parts.append(f"费用: ${cost:.4f}")
        if duration:
            meta_parts.append(f"耗时: {duration / 1000:.1f}s")
        if turns:
            meta_parts.append(f"轮次: {turns}")
        if session_id:
            meta_parts.append(f"会话: {session_id[:8]}...")
        if meta_parts:
            meta_info = " | ".join(meta_parts)

        # 无 assistant 文本时用 result 的兜底文本
        if not has_previous_text:
            result_text = data.get("result", "")
            if result_text:
                text_chunk = result_text

    return (text_chunk, log_actions, meta_info)


def _fold_entries(entries: list[str]) -> list[str]:
    """对超长条目列表进行折叠，保留最后 _MAX_STREAK_DISPLAY 条"""
    n = len(entries)
    if n > _MAX_STREAK_DISPLAY:
        return [f"*... 已省略 {n - _MAX_STREAK_DISPLAY} 次工具调用*"] + entries[-_MAX_STREAK_DISPLAY:]
    return entries


def _running_indicator(elapsed: int, thinking: bool) -> str:
    """生成执行中的进度提示行"""
    elapsed_text = f" (已等待 {elapsed}s)" if elapsed > 0 else ""
    if thinking:
        return f"💭 思考中...{elapsed_text}"
    return f"⏳ 正在处理...{elapsed_text}"


def render_log_parts(
    log_segments: list[dict],
    current_streak: list[dict],
    running: bool,
    elapsed: int = 0,
    thinking: bool = False,
) -> tuple[str, str]:
    """将内容段列表分离为工具日志文本和最终回复文本

    将执行过程（工具调用 + 中间文字）和最终回复分开渲染，用于 v2 卡片的
    折叠面板布局。

    分离规则：只有最后一段文字被视为最终回复，前面的文字属于执行过程的
    中间推理，与工具调用一起归入日志。

    Returns:
        (tool_log_text, reply_text) 二元组：
        - tool_log_text: 工具调用段 + 中间文字段 + 进度提示的 markdown
        - reply_text: 最后一段文字（最终回复），无文字段时为空字符串
    """
    log_lines: list[str] = []

    # 找到最后一个文字段的索引
    last_text_idx = -1
    for i, segment in enumerate(log_segments):
        if segment["type"] == "text":
            last_text_idx = i

    reply_text = ""
    for i, segment in enumerate(log_segments):
        if segment["type"] == "tools":
            log_lines.extend(_fold_entries(segment["entries"]))
            log_lines.append("")  # 段间空行
        elif segment["type"] == "text":
            if i == last_text_idx:
                reply_text = segment["content"]
            else:
                # 中间文字归入日志
                log_lines.append(segment["content"])
                log_lines.append("")  # 段间空行

    if current_streak:
        log_lines.extend(_fold_entries([e["line"] for e in current_streak]))

    if running:
        log_lines.append(_running_indicator(elapsed, thinking))

    return "\n".join(log_lines), reply_text
