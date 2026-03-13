"""
Claude Code 飞书卡片构建

所有飞书卡片 JSON 的构建逻辑集中于此。
函数均为无状态纯函数，接收数据参数返回卡片 JSON 字符串或 dict。
"""

import datetime
import json

from core.feishu_bot import limit_card_tables, count_card_tables, _MAX_CARD_TABLES
from plugins.claude_code.constants import (
    PLUGIN_KEYWORD,
    BEIJING_TZ,
    display_path,
)

# 会话列表分页控制
_SESSION_INITIAL_COUNT = 5   # /session 初始显示会话数
_SESSION_PAGE_SIZE = 10      # "显示更多" 每次增加的会话数

# 历史预览单条消息最大展示字符数
_HISTORY_TEXT_MAX = 300

# 飞书卡片 markdown 中计划内容的最大字符数
_PLAN_CONTENT_MAX = 4000


def build_execution_card(
    log_text: str,
    reply_text: str = "",
    running: bool = False,
    elapsed: int = 0,
    cancelled: bool = False,
) -> str:
    """构造执行中/完成/取消的主卡片 JSON 字符串（v2 格式）

    Args:
        log_text: 工具调用过程日志（放入折叠面板）
        reply_text: 文字回复内容（面板外直接展示）
        running: 是否执行中
        elapsed: 已用时秒数
        cancelled: 是否已取消
    """
    if running:
        template = "turquoise"
        if elapsed > 0:
            header_content = f"Claude Code (执行中...已用时 {elapsed}s)"
        else:
            header_content = "Claude Code (执行中...)"
    elif cancelled:
        template = "grey"
        header_content = "Claude Code（已停止）"
    else:
        template = "blue"
        header_content = "Claude Code"

    # 折叠面板箭头图标：默认向右，展开时顺时针旋转 90° 变为向下
    _PANEL_ICON = {
        "tag": "standard_icon",
        "token": "right-small-ccm_outlined",
        "size": "16px 16px",
    }

    def _panel(title: str, content: str, expanded: bool) -> dict:
        return {
            "tag": "collapsible_panel",
            "expanded": expanded,
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "icon": _PANEL_ICON,
                "icon_position": "left",
                "icon_expanded_angle": 90,
            },
            "elements": [
                {"tag": "markdown", "content": content},
            ],
        }

    elements: list[dict] = []

    # 飞书对整张卡片的 markdown 表格总数有上限，需跨面板统筹分配配额
    if log_text and reply_text:
        log_tc = count_card_tables(log_text)
        reply_tc = count_card_tables(reply_text)
        if log_tc + reply_tc > _MAX_CARD_TABLES:
            # 优先保留回复中的表格，剩余配额给日志
            reply_budget = min(reply_tc, _MAX_CARD_TABLES)
            log_budget = _MAX_CARD_TABLES - reply_budget
            log_text = limit_card_tables(log_text, log_budget)
            reply_text = limit_card_tables(reply_text, reply_budget)
        log_title = "📋 执行过程（进行中）" if running else "📋 执行过程"
        elements.append(_panel(log_title, log_text, expanded=running))
        elements.append(_panel("💬 回复", reply_text, expanded=True))
    elif log_text:
        log_title = "📋 执行过程（进行中）" if running else "📋 执行过程"
        elements.append(_panel(log_title, limit_card_tables(log_text), expanded=running))
    elif reply_text:
        elements.append(_panel("💬 回复", limit_card_tables(reply_text), expanded=True))
    else:
        elements.append({"tag": "markdown", "content": ""})

    # 运行中时添加取消按钮（V2 不支持 action 容器，按钮直接放入 elements）
    if running:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "取消执行"},
            "type": "danger",
            "value": {"action": "cancel", "plugin": PLUGIN_KEYWORD},
        })

    card: dict = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": header_content},
            "template": template,
        },
        "body": {
            "elements": elements,
        },
    }

    return json.dumps(card)


def build_permission_card(
    request_id: str, tool_name: str, tool_input: dict,
    show_accept_edits_option: bool = False, working_dir: str = ""
) -> str:
    """构造权限确认飞书卡片

    快捷升级按钮根据当前请求类型动态显示：
    - show_accept_edits_option=True（工作目录内的文件修改）:
      同时显示「允许本次会话所有修改」和「允许本次会话所有请求」
    - show_accept_edits_option=False（Bash 等其他操作，或工作目录外的文件修改）:
      仅显示「允许本次会话所有请求」，因为 accept_edits 模式对当前请求无效
    """
    # 格式化工具输入的展示内容
    if tool_name == "Bash" and "command" in tool_input:
        input_display = f"```\n{tool_input['command']}\n```"
    elif tool_name == "Edit" or tool_name == "Write":
        file_path = tool_input.get("file_path", tool_input.get("path", ""))
        input_display = f"文件: `{display_path(file_path, working_dir)}`"
    else:
        # 通用展示：JSON 格式
        input_display = f"```json\n{json.dumps(tool_input, indent=2, ensure_ascii=False)[:1000]}\n```"

    actions = [
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "允许"},
            "type": "primary",
            "value": {"action": "perm_allow", "plugin": PLUGIN_KEYWORD, "request_id": request_id},
        },
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "拒绝"},
            "type": "danger",
            "value": {"action": "perm_deny", "plugin": PLUGIN_KEYWORD, "request_id": request_id},
        },
    ]
    # 仅当请求属于 accept_edits 范围（工作目录内的文件修改）时显示该按钮
    if show_accept_edits_option:
        actions.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "允许本次会话所有修改"},
            "type": "default",
            "value": {"action": "perm_accept_edits", "plugin": PLUGIN_KEYWORD, "request_id": request_id},
        })
    # interactive 和 accept_edits 模式均显示「允许本次会话所有请求」（升级到 bypass）
    actions.append({
        "tag": "button",
        "text": {"tag": "plain_text", "content": "允许本次会话所有请求"},
        "type": "default",
        "value": {"action": "perm_bypass", "plugin": PLUGIN_KEYWORD, "request_id": request_id},
    })

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Claude Code 权限确认"},
            "template": "orange",
        },
        "elements": [
            {
                "tag": "markdown",
                "content": f"**工具**: {tool_name}\n**操作**:\n{input_display}",
            },
            {"tag": "hr"},
            {"tag": "action", "actions": actions},
        ],
    }
    return json.dumps(card)


def build_permission_handled_card(decision: str) -> str:
    """构造权限确认已处理的卡片（灰色，无按钮）"""
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"Claude Code 权限确认 - 已{decision}"},
            "template": "grey",
        },
        "elements": [
            {"tag": "markdown", "content": f"已{decision}此操作。"},
        ],
    }
    return json.dumps(card)


def build_plan_approval_card(
    request_id: str,
    plan_content: str,
    allowed_prompts: list[dict] | None = None,
) -> str:
    """构造 ExitPlanMode 计划审批飞书卡片

    展示计划内容（markdown 格式），附带「批准」「拒绝」按钮，
    以及「拒绝并反馈」表单输入框。

    Args:
        request_id: 权限请求 ID（用于回调匹配）
        plan_content: .claude/plans/ 下的计划文件内容
        allowed_prompts: ExitPlanMode 的 allowedPrompts 列表（暂保留）
    """
    if not plan_content:
        display_content = "*（未找到计划文件内容）*"
    elif len(plan_content) > _PLAN_CONTENT_MAX:
        display_content = plan_content[:_PLAN_CONTENT_MAX] + "\n\n**[计划内容已截断，超出卡片显示限制]**"
    else:
        display_content = plan_content
    display_content = limit_card_tables(display_content)

    elements: list[dict] = [
        {"tag": "markdown", "content": display_content},
        {"tag": "hr"},
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "批准计划"},
                    "type": "primary",
                    "value": {
                        "action": "plan_approve",
                        "plugin": PLUGIN_KEYWORD,
                        "request_id": request_id,
                    },
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "拒绝计划"},
                    "type": "danger",
                    "value": {
                        "action": "plan_reject",
                        "plugin": PLUGIN_KEYWORD,
                        "request_id": request_id,
                    },
                },
            ],
        },
        {
            "tag": "form",
            "name": "plan_reject_form",
            "elements": [
                {
                    "tag": "input",
                    "name": "plan_reject_reason",
                    "placeholder": {
                        "tag": "plain_text",
                        "content": "输入修改意见后点击「拒绝并反馈」…",
                    },
                },
                {
                    "tag": "button",
                    "name": "submit_plan_reject",
                    "text": {"tag": "plain_text", "content": "拒绝并反馈"},
                    "type": "default",
                    "form_action_type": "submit",
                    "value": {
                        "action": "plan_reject_with_reason",
                        "plugin": PLUGIN_KEYWORD,
                        "request_id": request_id,
                    },
                },
            ],
        },
    ]

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Claude Code 执行计划审批"},
            "template": "orange",
        },
        "elements": elements,
    }
    return json.dumps(card)


def build_plan_handled_card(decision: str, plan_summary: str = "") -> str:
    """构造计划审批已处理的卡片（灰色，无按钮）

    Args:
        decision: "批准" 或 "拒绝" 等决策描述
        plan_summary: 计划内容摘要（可选）
    """
    content = f"已{decision}此执行计划。"
    if plan_summary:
        content += f"\n\n{plan_summary}"

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"Claude Code 执行计划 - 已{decision}"},
            "template": "grey",
        },
        "elements": [
            {"tag": "markdown", "content": content},
        ],
    }
    return json.dumps(card)


def build_permission_mode_card(current_mode: str) -> str:
    """构造权限模式选择卡片，高亮当前模式"""
    mode_labels = {
        "interactive": "交互确认（Interactive）",
        "accept_edits": "自动接受编辑（Accept Edits）",
        "bypass": "全部放行（Bypass）",
    }
    mode_descs = {
        "interactive": "所有操作均通过飞书卡片确认，安全性最高",
        "accept_edits": "工作目录内的文件修改自动放行，Bash 等操作仍需确认",
        "bypass": "所有操作自动放行，无需任何确认（危险，慎用）",
    }
    buttons = []
    for mode, label in mode_labels.items():
        is_current = mode == current_mode
        buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": f"{'✓ ' if is_current else ''}{label}"},
            "type": "primary" if is_current else "default",
            "value": {"action": "set_perm_mode", "plugin": PLUGIN_KEYWORD, "mode": mode},
        })
    elements = [
        {"tag": "markdown", "content": f"当前模式：**{mode_labels[current_mode]}**\n{mode_descs[current_mode]}"},
        {"tag": "hr"},
    ]
    for mode, label in mode_labels.items():
        elements.append({"tag": "markdown", "content": f"**{label}**\n{mode_descs[mode]}"})
    elements.append({"tag": "action", "actions": buttons})
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Claude Code 权限模式"},
            "template": "blue",
        },
        "elements": elements,
    }
    return json.dumps(card)


def build_model_select_card(current_model: str, models_cfg: list[dict]) -> str:
    """构造模型选择卡片，高亮当前模型

    Args:
        current_model: 当前会话模型的 alias，空字符串表示 CLI 默认
        models_cfg: 可选模型配置列表
    """
    # "CLI 默认" 始终作为第一个选项
    options = [{"alias": "", "label": "CLI 默认", "desc": "不指定模型，由 Claude Code CLI 自行决定"}]
    for m in models_cfg:
        options.append({
            "alias": m.get("alias", ""),
            "label": m.get("label", m.get("alias", "未知")),
            "desc": m.get("desc", ""),
        })
    # 查找当前模型的展示信息
    current_label = "CLI 默认"
    current_desc = options[0]["desc"]
    for opt in options:
        if opt["alias"] == current_model:
            current_label = opt["label"]
            current_desc = opt["desc"]
            break
    buttons = []
    for opt in options:
        is_current = opt["alias"] == current_model
        buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": f"{'✓ ' if is_current else ''}{opt['label']}"},
            "type": "primary" if is_current else "default",
            "value": {"action": "set_model", "plugin": PLUGIN_KEYWORD, "model": opt["alias"]},
        })
    elements = [
        {"tag": "markdown", "content": f"当前模型：**{current_label}**\n{current_desc}"},
        {"tag": "hr"},
    ]
    for opt in options:
        elements.append({"tag": "markdown", "content": f"**{opt['label']}**\n{opt['desc']}"})
    elements.append({"tag": "action", "actions": buttons})
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Claude Code 模型选择"},
            "template": "blue",
        },
        "elements": elements,
    }
    return json.dumps(card)


def build_ask_user_card(
    request_id: str,
    questions: list[dict],
    answers: dict[int, str] | None = None,
) -> str:
    """构造 AskUserQuestion 飞书问题卡片（逐题展示）

    多问题场景下按顺序逐题展示：已回答的灰色展示，当前题目交互式展示，
    后续题目仅显示标题作为预览，用户答完当前题后刷新卡片展示下一题。

    Args:
        request_id: 权限请求 ID（用于回调匹配）
        questions: 问题列表，每项含 question, header, options 字段
        answers: 已回答的问题索引 → 答案，用于部分回答后刷新卡片
    """
    if answers is None:
        answers = {}

    elements: list[dict] = []
    total = len(questions)
    # 找到当前待回答的题目索引（第一个未回答的）
    current_qi = next((i for i in range(total) if i not in answers), total)

    for qi, question in enumerate(questions):
        q_text = question.get("question", "")
        header_text = question.get("header", "")
        options = question.get("options", [])

        # 多问题时添加序号前缀
        prefix = f"**问题 {qi + 1}/{total}**  " if total > 1 else ""

        if qi in answers:
            # 已回答的问题：灰色文本展示
            q_label = f"**{header_text}**" if header_text else q_text[:60]
            elements.append({
                "tag": "markdown",
                "content": f"{prefix}{q_label}\n~~已选择: {answers[qi]}~~",
            })
            if qi < total - 1:
                elements.append({"tag": "hr"})
            continue

        if qi > current_qi:
            # 后续未到达的问题：仅显示标题，不渲染交互元素
            q_label = f"**{header_text}**" if header_text else q_text[:60]
            elements.append({
                "tag": "markdown",
                "content": f"{prefix}{q_label}\n*待回答*",
            })
            if qi < total - 1:
                elements.append({"tag": "hr"})
            continue

        # 当前题目：交互式区块
        content_md = f"{prefix}**{header_text}**\n\n{q_text}" if header_text else f"{prefix}{q_text}"
        elements.append({"tag": "markdown", "content": content_md})

        # 各选项的描述说明
        if options:
            option_lines = []
            for i, opt in enumerate(options, 1):
                label = opt.get("label", f"选项{i}")
                desc = opt.get("description", "")
                option_lines.append(f"**{label}**: {desc}" if desc else f"**{label}**")
            elements.append({"tag": "markdown", "content": "\n".join(option_lines)})

        elements.append({"tag": "hr"})

        # 每个选项一个按钮
        actions = []
        for i, opt in enumerate(options):
            label = opt.get("label", f"选项{i+1}")
            desc = opt.get("description", "")
            answer_display = f"{label} - {desc}" if desc else label
            actions.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": label},
                "type": "primary" if i == 0 else "default",
                "value": {
                    "action": "ask_user_answer",
                    "plugin": PLUGIN_KEYWORD,
                    "request_id": request_id,
                    "question_index": qi,
                    "answer_label": answer_display,
                    "question_text": q_text[:100],
                },
            })
        elements.append({"tag": "action", "actions": actions})

        # "其他" 自定义输入：输入框 + 提交按钮放在 form 容器内
        elements.append({
            "tag": "form",
            "name": f"custom_answer_form_{qi}",
            "elements": [
                {
                    "tag": "input",
                    "name": f"custom_answer_{qi}",
                    "placeholder": {
                        "tag": "plain_text",
                        "content": "输入自定义回答…",
                    },
                },
                {
                    "tag": "button",
                    "name": f"submit_custom_{qi}",
                    "text": {"tag": "plain_text", "content": "其他"},
                    "type": "default",
                    "form_action_type": "submit",
                    "value": {
                        "action": "ask_user_custom",
                        "plugin": PLUGIN_KEYWORD,
                        "request_id": request_id,
                        "question_index": qi,
                        "question_text": q_text[:100],
                    },
                },
            ],
        })

        if qi < total - 1:
            elements.append({"tag": "hr"})

    # 卡片标题：多问题时显示进度
    answered_count = len(answers)
    if total > 1:
        title = f"Claude Code 需要你的输入（{answered_count + 1}/{total}）"
    else:
        title = "Claude Code 需要你的输入"

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "blue",
        },
        "elements": elements,
    }
    return json.dumps(card)


def build_ask_user_answered_card(
    questions: list[dict], answers: dict[int, str],
) -> str:
    """构造 AskUserQuestion 已回答的卡片（灰色，无按钮）

    Args:
        questions: 完整问题列表
        answers: 问题索引 → 用户答案
    """
    total = len(questions)
    lines = []
    for qi, question in enumerate(questions):
        q_text = question.get("question", "")[:100]
        answer = answers.get(qi, "（未回答）")
        if total > 1:
            lines.append(f"**问题 {qi + 1}**: {q_text}\n**选择**: {answer}")
        else:
            lines.append(f"**问题**: {q_text}\n**你的选择**: {answer}")

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Claude Code 用户输入 - 已回答"},
            "template": "grey",
        },
        "elements": [
            {"tag": "markdown", "content": "\n\n".join(lines)},
        ],
    }
    return json.dumps(card)


def build_session_row(session: dict, current_session_id: str) -> list[dict]:
    """构造单个会话行的卡片元素（信息 + 按钮行 + 分割线）"""
    sid = session["session_id"]
    short_id = sid[:8]
    working_dir = display_path(session.get("working_dir") or "") or "默认目录"
    title = session.get("title", "（无标题）")
    starred = session.get("starred", False)
    last_activity = session.get("last_activity", "")
    # 格式化时间（UTC → 北京时间 UTC+8，去掉秒）
    try:
        dt = datetime.datetime.fromisoformat(last_activity)
        dt_beijing = dt + BEIJING_TZ.utcoffset(None)
        time_str = dt_beijing.strftime("%m-%d %H:%M")
    except Exception:
        time_str = last_activity[:16]

    is_current = sid == current_session_id
    resume_label = f"{'✓ 当前  ' if is_current else ''}{short_id}…"
    star_label = "取消收藏" if starred else "收藏"

    return [
        {
            "tag": "markdown",
            "content": f"**{short_id}…** | `{working_dir}` | {time_str}\n{title}",
        },
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": resume_label},
                    "type": "primary" if is_current else "default",
                    "value": {
                        "action": "resume_session",
                        "plugin": PLUGIN_KEYWORD,
                        "session_id": sid,
                        "working_dir": session.get("working_dir", ""),
                    },
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": star_label},
                    "type": "default",
                    "value": {
                        "action": "toggle_star_session",
                        "plugin": PLUGIN_KEYWORD,
                        "session_id": sid,
                    },
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "重命名"},
                    "type": "default",
                    "value": {
                        "action": "show_rename_form",
                        "plugin": PLUGIN_KEYWORD,
                        "session_id": sid,
                    },
                },
            ],
        },
        {"tag": "hr"},
    ]


def build_sessions_card(
    sessions: list[dict],
    current_session_id: str,
    show_count: int = _SESSION_INITIAL_COUNT,
) -> dict:
    """构造历史会话选择卡片

    Args:
        sessions: 全部会话列表（已按 last_activity 倒序）
        current_session_id: 当前会话 ID，用于高亮标记
        show_count: 本次显示的未收藏会话数量（收藏会话始终全部显示）
    """
    starred = [s for s in sessions if s.get("starred")]
    unstarred = [s for s in sessions if not s.get("starred")]

    elements: list[dict] = [
        {
            "tag": "markdown",
            "content": "点击选择要恢复的会话（将替换当前会话上下文）",
        },
        {"tag": "hr"},
    ]

    # 收藏会话分区（全部显示，不分页）
    if starred:
        elements.append({
            "tag": "markdown",
            "content": "**⭐ 收藏会话**",
        })
        for session in starred:
            elements.extend(build_session_row(session, current_session_id))
        # 有未收藏会话时，显示分区标题
        if unstarred:
            elements.append({
                "tag": "markdown",
                "content": "**📋 全部会话**",
            })

    # 未收藏会话分区（分页显示）
    visible_unstarred = unstarred[:show_count]
    for session in visible_unstarred:
        elements.extend(build_session_row(session, current_session_id))

    # 还有更多未收藏会话时，添加"显示更多"按钮
    remaining = len(unstarred) - show_count
    if remaining > 0:
        next_count = show_count + _SESSION_PAGE_SIZE
        elements.append({
            "tag": "action",
            "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": f"显示更多（还有 {remaining} 个）"},
                "type": "default",
                "value": {
                    "action": "show_more_sessions",
                    "plugin": PLUGIN_KEYWORD,
                    "show_count": next_count,
                },
            }],
        })

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Claude Code 历史会话"},
            "template": "blue",
        },
        "elements": elements,
    }


def build_rename_card(session: dict) -> dict:
    """构造会话重命名卡片（输入框 + 提交/取消按钮）"""
    sid = session["session_id"]
    short_id = sid[:8]
    working_dir = display_path(session.get("working_dir") or "") or "默认目录"
    current_title = session.get("title", "")

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "重命名会话"},
            "template": "blue",
        },
        "elements": [
            {
                "tag": "markdown",
                "content": f"**{short_id}…** | `{working_dir}`\n当前标题：{current_title}",
            },
            {"tag": "hr"},
            {
                "tag": "form",
                "name": "rename_form",
                "elements": [
                    {
                        "tag": "input",
                        "name": "rename_title",
                        "placeholder": {
                            "tag": "plain_text",
                            "content": "输入新标题…",
                        },
                        "default_value": current_title,
                    },
                    {
                        "tag": "button",
                        "name": "submit_rename",
                        "text": {"tag": "plain_text", "content": "确认"},
                        "type": "primary",
                        "form_action_type": "submit",
                        "value": {
                            "action": "rename_session",
                            "plugin": PLUGIN_KEYWORD,
                            "session_id": sid,
                        },
                    },
                ],
            },
            {
                "tag": "action",
                "actions": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "取消"},
                    "type": "default",
                    "value": {
                        "action": "cancel_rename",
                        "plugin": PLUGIN_KEYWORD,
                    },
                }],
            },
        ],
    }


def build_history_preview_card(
    session_id: str, rounds: list[tuple[str, str]]
) -> dict:
    """构造会话历史预览卡片，展示最近几轮用户/CC 对话"""
    elements: list[dict] = []
    for user_text, assistant_text in rounds:
        if len(assistant_text) > _HISTORY_TEXT_MAX:
            assistant_text = assistant_text[:_HISTORY_TEXT_MAX] + "…（省略）"
        elements.append({
            "tag": "markdown",
            "content": f"👤 **你**\n{user_text}",
        })
        elements.append({
            "tag": "markdown",
            "content": f"🤖 **CC**\n{assistant_text}",
        })
        elements.append({"tag": "hr"})

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"会话 {session_id[:8]}… 最近对话",
            },
            "template": "green",
        },
        "elements": elements,
    }
