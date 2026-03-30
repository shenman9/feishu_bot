"""
番茄钟插件 — 飞书卡片构建函数

所有函数为纯函数，返回 dict，不依赖任何外部状态。
按钮 value 均包含 "plugin" 字段用于 HubBot 显式路由。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .models import Phase, fmt_duration

if TYPE_CHECKING:
    from .models import DailyStats, LastSettings, PomodoroState, ReminderState

PLUGIN_KEYWORD = "番茄钟"


# ────────────────────────── 工具函数 ──────────────────────────

def _fmt_time(seconds: float) -> str:
    """将秒数格式化为 HH:MM:SS 或 MM:SS"""
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _btn(text: str, action: str, btn_type: str = "default", **extra) -> dict:
    """快捷创建按钮元素"""
    value = {"action": action, "plugin": PLUGIN_KEYWORD, **extra}
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": btn_type,
        "value": value,
    }



# ────────────────────────── 番茄钟设置 ──────────────────────────

def _time_input_fields(prefix: str, label: str,
                       default_h: str = "0", default_m: str = "25",
                       default_s: str = "0") -> list[dict]:
    """生成一组时/分/秒输入字段，三列并排，label 在输入框上方"""
    return [{
        "tag": "column_set",
        "flex_mode": "none",
        "background_style": "default",
        "columns": [
            {
                "tag": "column", "width": "weighted", "weight": 1,
                "elements": [{
                    "tag": "input", "name": f"{prefix}_h",
                    "input_type": "text", "width": "fill",
                    "label": {"tag": "plain_text", "content": f"{label}（时）"},
                    "placeholder": {"tag": "plain_text", "content": "0"},
                    "default_value": default_h,
                }],
            },
            {
                "tag": "column", "width": "weighted", "weight": 1,
                "elements": [{
                    "tag": "input", "name": f"{prefix}_m",
                    "input_type": "text", "width": "fill",
                    "label": {"tag": "plain_text", "content": f"{label}（分）"},
                    "placeholder": {"tag": "plain_text", "content": "0"},
                    "default_value": default_m,
                }],
            },
            {
                "tag": "column", "width": "weighted", "weight": 1,
                "elements": [{
                    "tag": "input", "name": f"{prefix}_s",
                    "input_type": "text", "width": "fill",
                    "label": {"tag": "plain_text", "content": f"{label}（秒）"},
                    "placeholder": {"tag": "plain_text", "content": "0"},
                    "default_value": default_s,
                }],
            },
        ],
    }]


def _seconds_to_hms(total: int) -> tuple[str, str, str]:
    """将秒数拆分为 (时, 分, 秒) 字符串"""
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return str(h), str(m), str(s)


def build_pomodoro_setup_card(settings: "LastSettings | None" = None) -> dict:
    """番茄钟参数设置表单，使用上次的设置参数作为默认值"""
    from .models import LastSettings
    s = settings or LastSettings()
    work_h, work_m, work_s = _seconds_to_hms(s.work_seconds)
    rest_h, rest_m, rest_s = _seconds_to_hms(s.rest_seconds)

    form_elements: list[dict] = [
        {
            "tag": "input",
            "name": "pomo_title",
            "input_type": "text",
            "width": "default",
            "label": {"tag": "plain_text", "content": "标题"},
            "placeholder": {"tag": "plain_text", "content": "工作"},
            "default_value": s.title,
        },
    ]
    form_elements.extend(_time_input_fields("work", "工作时长", work_h, work_m, work_s))
    form_elements.extend(_time_input_fields("rest", "休息时长", rest_h, rest_m, rest_s))
    form_elements.append({
        "tag": "input",
        "name": "cycles",
        "input_type": "text",
        "width": "default",
        "label": {"tag": "plain_text", "content": "周期数"},
        "placeholder": {"tag": "plain_text", "content": "1"},
        "default_value": str(s.cycles),
    })

    form_elements.append({
        "tag": "button",
        "name": "submit_setup",
        "text": {"tag": "plain_text", "content": "开始"},
        "type": "primary",
        "form_action_type": "submit",
        "value": {"action": "pomo_start", "plugin": PLUGIN_KEYWORD},
    })

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "番茄钟设置"},
            "template": "red",
        },
        "elements": [
            {"tag": "form", "name": "pomo_setup_form", "elements": form_elements},
            {"tag": "action", "actions": [_btn("我的统计", "show_stats")]},
        ],
    }


# ────────────────────────── 番茄钟运行中 ──────────────────────────

def build_pomodoro_running_card(state: PomodoroState) -> dict:
    """番茄钟运行中状态卡片"""
    left = _fmt_time(state.time_left_seconds)
    end_time = state.end_time_str
    label = state.title if state.phase == "work" else "休息"

    body_parts = [
        f"**{label}**（{state.work_duration_str if state.phase == 'work' else state.rest_duration_str}）",
        f"**周期**: 第 {state.current_cycle} / {state.total_cycles} 轮",
        f"**结束时间**: {end_time}",
    ]

    template = "turquoise" if state.phase == "work" else "green"

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"番茄钟 · {label} · {left}"},
            "template": template,
        },
        "elements": [
            {"tag": "markdown", "content": "\n".join(body_parts)},
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    _btn("暂停", "pomo_pause"),
                    _btn("跳过", "pomo_skip"),
                    _btn("停止", "pomo_stop", "danger"),
                ],
            },
        ],
    }


# ────────────────────────── 番茄钟暂停 ──────────────────────────

def build_pomodoro_paused_card(state: PomodoroState) -> dict:
    """番茄钟暂停状态卡片"""
    label = state.title if state.phase == "work" else "休息"
    left = _fmt_time(state.remaining_seconds)

    body = (
        f"**状态**: 已暂停（{label}）\n"
        f"**周期**: 第 {state.current_cycle} / {state.total_cycles} 轮\n"
        f"**剩余时间**: {left}"
    )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"番茄钟 · {label} · 已暂停"},
            "template": "yellow",
        },
        "elements": [
            {"tag": "markdown", "content": body},
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    _btn("恢复", "pomo_resume", "primary"),
                    _btn("跳过此阶段", "pomo_skip"),
                    _btn("停止", "pomo_stop", "danger"),
                ],
            },
        ],
    }


# ────────────────────────── 阶段等待确认 ──────────────────────────

def build_phase_wait_card(state: PomodoroState, next_phase: str) -> dict:
    """阶段结束后等待用户确认开始下一阶段的卡片

    Args:
        next_phase: 下一个阶段 "rest" 或 "work"
    """
    focus = fmt_duration(state.total_focus_seconds)

    if next_phase == "rest":
        # 工作刚结束，等待开始休息
        title = f"{state.title}时间结束！"
        body = (
            f"第 {state.current_cycle} 轮{state.title}已完成。\n"
            f"已累计专注 {focus}。\n"
            f"准备好了就开始休息 {state.rest_duration_str} 吧。"
        )
        template = "green"
        actions = [
            _btn("开始休息", "pomo_start_next", "primary"),
            _btn("跳过休息", "pomo_skip_rest"),
            _btn("停止", "pomo_stop", "danger"),
        ]
    else:
        # 休息刚结束（或无休息），等待开始下一轮工作
        title = "休息结束！" if state.phase == Phase.REST else f"{state.title}时间结束！"
        body = (
            f"第 {state.current_cycle} / {state.total_cycles} 轮即将开始。\n"
            f"已累计专注 {focus}。\n"
            f"准备好了就开始{state.title} {state.work_duration_str} 吧。"
        )
        template = "turquoise"
        actions = [
            _btn(f"开始{state.title}", "pomo_start_next", "primary"),
            _btn("停止", "pomo_stop", "danger"),
        ]

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        },
        "elements": [
            {"tag": "markdown", "content": body},
            {"tag": "hr"},
            {"tag": "action", "actions": actions},
        ],
    }


def build_phase_finished_card(state: PomodoroState, finished_label: str = "",
                              suffix: str = "已结束",
                              cycle: int = 0) -> dict:
    """旧卡片的精简灰色样式，无按钮

    Args:
        finished_label: 阶段标签（如 "工作" 或 "休息"）
        suffix: 标题后缀（"已结束"、"已跳过"、"已停止"）
        cycle: 显示的轮次（0 则取 state.current_cycle）
    """
    label = finished_label or (state.title if state.phase == "work" else "休息")
    display_cycle = cycle or state.current_cycle
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"番茄钟 · {label}{suffix}"},
            "template": "grey",
        },
        "elements": [
            {"tag": "markdown",
             "content": f"第 {display_cycle} / {state.total_cycles} 轮　　"
                        f"已专注 {fmt_duration(state.total_focus_seconds)}"},
        ],
    }


# ────────────────────────── 番茄钟完成 ──────────────────────────

def build_pomodoro_completed_card(state: PomodoroState) -> dict:
    """番茄钟完成统计卡片"""
    focus = fmt_duration(state.total_focus_seconds)

    body = (
        f"**完成周期**: {state.completed_work_phases} 轮\n"
        f"**总专注时间**: {focus}"
    )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "番茄钟 · 完成！"},
            "template": "green",
        },
        "elements": [
            {"tag": "markdown", "content": body},
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    _btn("再来一轮", "pomo_restart", "primary"),
                    _btn("返回", "show_setup"),
                ],
            },
        ],
    }


# ────────────────────────── 替换确认 ──────────────────────────

def build_confirm_replace_card(old_state: PomodoroState) -> dict:
    """确认替换进行中番茄钟的卡片"""
    label = old_state.title if old_state.phase == "work" else "休息"
    left = _fmt_time(old_state.time_left_seconds)

    body = (
        f"当前有番茄钟正在运行：{label}中，剩余 {left}。\n"
        f"开始新的番茄钟将替换当前的，确认吗？"
    )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "番茄钟 · 替换确认"},
            "template": "orange",
        },
        "elements": [
            {"tag": "markdown", "content": body},
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    _btn("确认替换", "pomo_confirm_replace", "primary"),
                    _btn("取消", "pomo_cancel_replace"),
                ],
            },
        ],
    }


# ────────────────────────── 提醒相关 ──────────────────────────

def build_reminder_list_card(reminders: dict[str, ReminderState]) -> dict:
    """提醒列表卡片"""
    if not reminders:
        body = "暂无定时提醒。"
    else:
        lines = []
        for r in reminders.values():
            status = "启用" if r.active else "暂停"
            lines.append(f"**{r.name}** — `{r.cron_expr}` · {status}\n　　{r.message}")
        body = "\n\n".join(lines)

    elements: list[dict] = [{"tag": "markdown", "content": body}]

    # 每条提醒的操作按钮
    if reminders:
        elements.append({"tag": "hr"})
        for r in reminders.values():
            toggle_text = "暂停" if r.active else "恢复"
            elements.append({
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": f"{r.name}: {toggle_text}"},
                        "type": "default",
                        "value": {
                            "action": "reminder_toggle",
                            "plugin": PLUGIN_KEYWORD,
                            "remind_id": r.remind_id,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "删除"},
                        "type": "danger",
                        "value": {
                            "action": "reminder_delete",
                            "plugin": PLUGIN_KEYWORD,
                            "remind_id": r.remind_id,
                        },
                    },
                ],
            })

    elements.append({"tag": "hr"})
    elements.append({
        "tag": "action",
        "actions": [
            _btn("新建提醒", "reminder_create", "primary"),
            _btn("返回", "show_setup"),
        ],
    })

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "我的提醒"},
            "template": "orange",
        },
        "elements": elements,
    }


def build_reminder_setup_card() -> dict:
    """新建提醒：预设模板 + 自定义表单"""
    elements: list[dict] = [
        {"tag": "markdown", "content": "**快捷模板**"},
        {
            "tag": "action",
            "actions": [
                _btn("整点喝水 (8-20点)", "reminder_template",
                     template="water"),
                _btn("每2小时站立", "reminder_template",
                     template="stand"),
            ],
        },
        {
            "tag": "action",
            "actions": [
                _btn("工作日晨报 9:00", "reminder_template",
                     template="morning"),
                _btn("午休提醒 12:00", "reminder_template",
                     template="lunch"),
            ],
        },
        {"tag": "hr"},
        {"tag": "markdown", "content": "**自定义提醒**\ncron 格式: `分 时 日 月 周`，例如 `0 8-20 * * *`"},
        {
            "tag": "form",
            "name": "reminder_form",
            "elements": [
                {
                    "tag": "input",
                    "name": "reminder_name",
                    "input_type": "text",
                    "width": "default",
                    "label": {"tag": "plain_text", "content": "提醒名称"},
                    "placeholder": {"tag": "plain_text", "content": "喝水提醒"},
                },
                {
                    "tag": "input",
                    "name": "reminder_cron",
                    "input_type": "text",
                    "width": "default",
                    "label": {"tag": "plain_text", "content": "cron 表达式"},
                    "placeholder": {"tag": "plain_text", "content": "0 8-20 * * *"},
                },
                {
                    "tag": "input",
                    "name": "reminder_message",
                    "input_type": "text",
                    "width": "default",
                    "label": {"tag": "plain_text", "content": "提醒内容"},
                    "placeholder": {"tag": "plain_text", "content": "该喝水啦！"},
                },
                {
                    "tag": "button",
                    "name": "submit_reminder",
                    "text": {"tag": "plain_text", "content": "创建"},
                    "type": "primary",
                    "form_action_type": "submit",
                    "value": {"action": "reminder_submit", "plugin": PLUGIN_KEYWORD},
                },
            ],
        },
    ]

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "新建定时提醒"},
            "template": "orange",
        },
        "elements": elements + [
            {"tag": "action", "actions": [_btn("返回", "show_setup")]},
        ],
    }


def build_reminder_notification_card(reminder: ReminderState) -> dict:
    """提醒触发时的通知卡片"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"提醒 · {reminder.name}"},
            "template": "orange",
        },
        "elements": [
            {"tag": "markdown", "content": reminder.message},
        ],
    }


# ────────────────────────── 专注统计 ──────────────────────────

def build_stats_card(today: "DailyStats",
                     total_seconds: float, total_days: int,
                     total_phases: int) -> dict:
    """专注统计卡片"""
    today_focus = fmt_duration(today.focus_seconds) if today.focus_seconds > 0 else "0"
    total_focus = fmt_duration(total_seconds) if total_seconds > 0 else "0"

    body = (
        f"**今日专注**: {today_focus}\n"
        f"**今日完成**: {today.work_phases} 个周期 · {today.sessions} 次番茄钟\n"
        f"---\n"
        f"**累计专注**: {total_focus}\n"
        f"**累计天数**: {total_days} 天\n"
        f"**累计周期**: {total_phases} 个"
    )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "番茄钟 · 我的统计"},
            "template": "blue",
        },
        "elements": [
            {"tag": "markdown", "content": body},
            {"tag": "hr"},
            {"tag": "action", "actions": [
                _btn("返回", "show_setup"),
                _btn("清空数据", "clear_stats", "danger"),
            ]},
        ],
    }


def build_confirm_clear_stats_card() -> dict:
    """确认清空统计数据的卡片"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "确认清空统计数据"},
            "template": "orange",
        },
        "elements": [
            {"tag": "markdown", "content": "清空后所有历史专注记录将永久删除，无法恢复。\n确认要清空吗？"},
            {"tag": "hr"},
            {"tag": "action", "actions": [
                _btn("确认清空", "confirm_clear_stats", "danger"),
                _btn("取消", "show_stats"),
            ]},
        ],
    }
