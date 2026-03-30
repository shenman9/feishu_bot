"""
番茄钟插件 — 主插件类

功能:
- 单次 / 多周期番茄钟计时
- 定时提醒（cron 表达式）
- 运行中热改: 暂停、恢复、跳过、停止、改时
"""

import json
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from core.plugin import Plugin

from . import cards
from .cards import _fmt_time
from .models import Phase, PomodoroState, LastSettings, ReminderState, UserTimers
from .stats import FocusStatsStore

logger = logging.getLogger(__name__)

PLUGIN_KEYWORD = "番茄钟"

# 预设提醒模板
_REMINDER_TEMPLATES: dict[str, dict] = {
    "water": {
        "name": "整点喝水",
        "cron_expr": "0 8-20 * * *",
        "message": "该喝水啦！起来活动一下吧。",
    },
    "stand": {
        "name": "站立休息",
        "cron_expr": "0 */2 8-20 * *",
        "message": "已经坐了两个小时，站起来活动一下！",
    },
    "morning": {
        "name": "工作日晨报",
        "cron_expr": "0 9 * * 1-5",
        "message": "新的一天开始了！看看今天的计划吧。",
    },
    "lunch": {
        "name": "午休提醒",
        "cron_expr": "0 12 * * *",
        "message": "午饭时间到！别忘了休息。",
    },
}


class PomodoroPlugin(Plugin):
    """番茄钟插件: 计时器、多周期专注、定时提醒"""

    def __init__(self):
        super().__init__()
        self._user_timers: dict[str, UserTimers] = {}   # "user_id:chat_id" → UserTimers
        self._active_users: set[str] = set()              # 正在交互中的用户
        self._scheduler: Optional[BackgroundScheduler] = None
        self._lock = threading.RLock()
        self._stats = FocusStatsStore()

    # ──────────────── 元信息 ────────────────

    @property
    def name(self) -> str:
        return "番茄钟"

    @property
    def keyword(self) -> str:
        return PLUGIN_KEYWORD

    @property
    def description(self) -> str:
        return "番茄计时、多周期专注、定时提醒"

    # ──────────────── 生命周期 ────────────────

    def on_register(self, bot: "HubBot") -> None:  # noqa: F821
        """注册时初始化 APScheduler"""
        super().on_register(bot)
        self._scheduler = BackgroundScheduler(daemon=True)
        self._scheduler.start()
        logger.info("番茄钟: APScheduler 已启动")

    def is_user_active(self, user_id: str, chat_id: str = "") -> bool:
        return self._state_key(user_id, chat_id) in self._active_users

    def deactivate_user(self, user_id: str, chat_id: str = "") -> None:
        """用户退出插件时清理交互状态，不停止定时器"""
        self._active_users.discard(self._state_key(user_id, chat_id))

    # ──────────────── 状态管理 ────────────────

    @staticmethod
    def _state_key(user_id: str, chat_id: str) -> str:
        return f"{user_id}:{chat_id}"

    def _get_timers(self, user_id: str, chat_id: str) -> UserTimers:
        key = self._state_key(user_id, chat_id)
        if key not in self._user_timers:
            self._user_timers[key] = UserTimers()
        return self._user_timers[key]

    # ──────────────── 消息处理 ────────────────

    def handle_message(self, user_id: str, chat_id: str, text: str,
                       message_id: str = "") -> None:
        key = self._state_key(user_id, chat_id)
        self._active_users.add(key)
        timers = self._get_timers(user_id, chat_id)
        text = text.strip()

        # 激活关键词 / 菜单
        if text == PLUGIN_KEYWORD or text in ("菜单", "menu", "帮助"):
            self.bot.reply_card(chat_id, cards.build_pomodoro_setup_card(timers.last_settings))
            return

        # 文本指令 — 返回的 card 通过 reply_card 发新消息
        if text in ("暂停", "pause"):
            card = self._pause_pomodoro(user_id, chat_id, timers)
            if card:
                self.bot.reply_card(chat_id, card)
        elif text in ("恢复", "继续", "resume"):
            card = self._resume_pomodoro(user_id, chat_id, timers)
            if card:
                self.bot.reply_card(chat_id, card)
        elif text in ("停止", "stop"):
            card = self._stop_pomodoro(user_id, chat_id, timers)
            if card:
                self.bot.reply_card(chat_id, card)
        elif text in ("跳过", "skip"):
            self._skip_phase(user_id, chat_id, timers)
        elif text in ("状态", "status"):
            self._show_status(user_id, chat_id, timers)
        elif text in ("提醒", "提醒列表"):
            self.bot.reply_card(chat_id, cards.build_reminder_list_card(timers.reminders))
        elif text.startswith("专注"):
            # "专注" / "专注 30" / "专注30" (分钟)
            # 提取"专注"后面的数字部分（支持有无空格）
            num_str = text[len("专注"):].strip()
            if num_str.isdigit() and int(num_str) > 0:
                work_secs = int(num_str) * 60
            else:
                # 无数字 → 用上次设置的工作时长
                work_secs = timers.last_settings.work_seconds
            timers.last_settings = LastSettings(
                title="工作", work_seconds=work_secs, rest_seconds=0, cycles=1)
            card = self._start_pomodoro(user_id, chat_id, timers,
                                        work_seconds=work_secs, rest_seconds=0, cycles=1)
            if card:
                self._send_card_as_new(chat_id, timers, card)
        else:
            self._send_help(chat_id)

    def _send_help(self, chat_id: str) -> None:
        self.bot.reply(
            chat_id,
            "番茄钟指令:\n"
            "- **专注** / **专注 30** — 开始番茄钟\n"
            "- **暂停** / **恢复** / **停止** / **跳过** — 控制番茄钟\n"
            "- **状态** — 查看当前番茄钟状态\n"
            "- **提醒** — 查看定时提醒列表\n"
            "- **菜单** — 返回主菜单",
        )

    def _show_status(self, user_id: str, chat_id: str, timers: UserTimers) -> None:
        pomo = timers.pomodoro
        if not pomo or pomo.status in ("idle", "completed"):
            self.bot.reply(chat_id, "当前没有进行中的番茄钟。")
            return
        label = pomo.title if pomo.phase == Phase.WORK else "休息"
        left = _fmt_time(pomo.time_left_seconds) if pomo.status == "running" else _fmt_time(pomo.remaining_seconds)
        cycle_info = f"第 {pomo.current_cycle}/{pomo.total_cycles} 轮"
        if pomo.status == "running":
            self.bot.reply(chat_id, f"番茄钟运行中：{label} · 剩余 {left} · {cycle_info}")
        elif pomo.status == "paused":
            self.bot.reply(chat_id, f"番茄钟已暂停：{label} · 剩余 {left} · {cycle_info}")
        elif pomo.status == "waiting":
            self.bot.reply(chat_id, f"番茄钟等待中：{label}阶段已结束 · {cycle_info}，请在卡片上点击按钮继续。")

    # ──────────────── 番茄钟核心 ────────────────

    def _start_pomodoro(self, user_id: str, chat_id: str, timers: UserTimers,
                        work_seconds: int = 25 * 60, rest_seconds: int = 5 * 60,
                        cycles: int = 1, confirmed: bool = False,
                        start_cycle: int = 1,
                        carry_focus: float = 0.0,
                        carry_work_phases: int = 0,
                        title: str = "工作") -> Optional[dict]:
        """启动番茄钟，返回卡片 dict（由调用者决定原地更新还是发新消息）"""
        # 检查已有番茄钟
        if timers.pomodoro and timers.pomodoro.status in ("running", "paused"):
            if not confirmed:
                return cards.build_confirm_replace_card(timers.pomodoro)
            # 用户已确认替换
            self._cancel_pomodoro_job(timers.pomodoro)

        state = PomodoroState(
            title=title,
            work_seconds=work_seconds,
            rest_seconds=rest_seconds,
            total_cycles=cycles,
            current_cycle=start_cycle,
            phase=Phase.WORK,
            status="running",
            phase_start_time=time.time(),
            completed_work_phases=carry_work_phases,
            total_focus_seconds=carry_focus,
        )
        timers.pomodoro = state
        self._schedule_phase_end(user_id, chat_id, state)
        self._start_refresh_job(user_id, chat_id, state)

        return cards.build_pomodoro_running_card(state)

    def _schedule_phase_end(self, user_id: str, chat_id: str,
                            state: PomodoroState) -> None:
        """调度当前阶段结束回调"""
        duration = state.phase_duration_seconds
        self._schedule_phase_end_after(user_id, chat_id, state, duration)

    def _schedule_phase_end_after(self, user_id: str, chat_id: str,
                                  state: PomodoroState, seconds: float) -> None:
        """在 seconds 秒后触发阶段结束"""
        job_id = f"pomo_{user_id}_{chat_id}_{time.time_ns()}"
        run_time = datetime.now() + timedelta(seconds=seconds)
        self._scheduler.add_job(
            self._on_phase_end,
            trigger=DateTrigger(run_date=run_time),
            args=[user_id, chat_id],
            id=job_id,
            replace_existing=True,
        )
        state.job_id = job_id

    def _on_phase_end(self, user_id: str, chat_id: str,
                      urgent: bool = True) -> None:
        """阶段结束回调

        Args:
            urgent: 是否发送加急通知（自然到期=True，用户主动跳过=False）
        """
        try:
            card = None
            send_new = False
            prev_label = ""
            prev_cycle = 0  # 记录递增前的轮次，用于旧卡片显示
            with self._lock:
                timers = self._user_timers.get(self._state_key(user_id, chat_id))
                if not timers or not timers.pomodoro:
                    return
                state = timers.pomodoro
                if state.status != "running":
                    return  # 竞态保护: 已暂停或已停止

                # 统计工作阶段的专注时间
                if state.phase == Phase.WORK:
                    elapsed = time.time() - state.phase_start_time
                    state.total_focus_seconds += elapsed
                    state.completed_work_phases += 1
                    # 持久化到用户统计
                    self._stats.add_focus(user_id, elapsed, work_phases=1)

                # 停止当前阶段的刷新
                self._stop_refresh_job(state)
                state.job_id = None

                # 判断下一步
                if state.phase == Phase.WORK and state.rest_seconds > 0:
                    # 工作结束 → 等待用户确认开始休息
                    prev_label = state.title
                    prev_cycle = state.current_cycle
                    state.status = "waiting"
                    # phase 还未切换，保持 WORK，等用户确认后再切到 REST
                    card = cards.build_phase_wait_card(state, "rest")
                    send_new = True

                elif state.phase == Phase.WORK and state.rest_seconds == 0 \
                        and state.current_cycle < state.total_cycles:
                    # 无休息，工作结束 → 等待用户确认开始下一轮工作
                    prev_label = state.title
                    prev_cycle = state.current_cycle
                    state.current_cycle += 1
                    state.status = "waiting"
                    card = cards.build_phase_wait_card(state, "work")
                    send_new = True

                elif state.phase == Phase.REST \
                        and state.current_cycle < state.total_cycles:
                    # 休息结束 → 等待用户确认开始下一轮工作
                    prev_label = "休息"
                    prev_cycle = state.current_cycle
                    state.current_cycle += 1
                    state.status = "waiting"
                    card = cards.build_phase_wait_card(state, "work")
                    send_new = True

                else:
                    # 所有周期完成
                    prev_label = state.title if state.phase == Phase.WORK else "休息"
                    prev_cycle = state.current_cycle
                    state.status = "completed"
                    card = cards.build_pomodoro_completed_card(state)
                    self._stats.increment_sessions(user_id)
                    send_new = True

            # 网络 I/O 在锁外执行
            if card and send_new:
                old_msg_id = state.card_message_id
                # 发送新卡片
                msg_id = self.bot.send_message_get_id(
                    chat_id, "interactive", json.dumps(card))
                if timers and timers.pomodoro:
                    timers.pomodoro.card_message_id = msg_id
                # 加急通知（仅自然到期时发送）
                if msg_id and urgent:
                    self.bot.urgent_message(msg_id, [user_id])
                # 把旧卡片 patch 成精简的"已结束"样式（避免撤回提示）
                if old_msg_id:
                    finished = cards.build_phase_finished_card(
                        state, prev_label, cycle=prev_cycle)
                    self.bot.patch_message(
                        old_msg_id, json.dumps(finished))

        except Exception as e:
            logger.error("番茄钟阶段结束回调异常: user=%s, chat=%s, error=%s",
                         user_id, chat_id, e, exc_info=True)

    # ──────────────── 热改操作 ────────────────
    # 以下方法返回 Optional[dict]：
    #   - 被 handle_card_action 调用时，返回卡片 dict 由调用者走 make_card_response
    #   - 被 handle_message 调用时，调用者用 reply_card 发新卡片

    def _pause_pomodoro(self, user_id: str, chat_id: str,
                        timers: UserTimers) -> Optional[dict]:
        """暂停番茄钟，返回新卡片 dict 或 None"""
        logger.info("[番茄钟][pause] 开始, user=%s", user_id)
        with self._lock:
            state = timers.pomodoro
            if not state or state.status != "running":
                logger.info("[番茄钟][pause] 状态不是 running (status=%s)，跳过",
                            state.status if state else "None")
                self.bot.reply(chat_id, "当前没有进行中的番茄钟。")
                return None

            elapsed = time.time() - state.phase_start_time
            state.remaining_seconds = max(0, state.phase_duration_seconds - elapsed)
            logger.info("[番茄钟][pause] 取消 jobs, job_id=%s, refresh_job_id=%s",
                        state.job_id, state.refresh_job_id)
            self._cancel_pomodoro_job(state)
            state.status = "paused"
            logger.info("[番茄钟][pause] status 已设为 paused, remaining=%.1f秒",
                        state.remaining_seconds)
            return cards.build_pomodoro_paused_card(state)

    def _resume_pomodoro(self, user_id: str, chat_id: str,
                         timers: UserTimers) -> Optional[dict]:
        """恢复番茄钟，返回新卡片 dict 或 None"""
        with self._lock:
            state = timers.pomodoro
            if not state or state.status != "paused":
                self.bot.reply(chat_id, "当前没有暂停中的番茄钟。")
                return None

            state.status = "running"
            # 将 phase_start_time 回拨，使 time_left_seconds 计算结果 = remaining_seconds
            # 即 phase_duration - (now - start) = remaining  →  start = now - (duration - remaining)
            state.phase_start_time = time.time() - (
                state.phase_duration_seconds - state.remaining_seconds)
            self._schedule_phase_end_after(
                user_id, chat_id, state, state.remaining_seconds)
            self._start_refresh_job(user_id, chat_id, state)
            return cards.build_pomodoro_running_card(state)

    def _skip_phase(self, user_id: str, chat_id: str,
                    timers: UserTimers) -> None:
        """跳过当前阶段（始终发新卡片，不走 card response）"""
        with self._lock:
            state = timers.pomodoro
            if not state or state.status not in ("running", "paused"):
                self.bot.reply(chat_id, "当前没有进行中的番茄钟。")
                return

            # 如果处于暂停状态，用 remaining_seconds 恢复正确的 phase_start_time
            # 避免 _on_phase_end 用 wall clock 计算出包含暂停时间的 elapsed
            if state.status == "paused":
                state.status = "running"
                state.phase_start_time = time.time() - (
                    state.phase_duration_seconds - state.remaining_seconds)
            self._cancel_pomodoro_job(state)

        # 直接触发阶段转换（用户主动跳过，不发加急）
        self._on_phase_end(user_id, chat_id, urgent=False)

    def _stop_pomodoro(self, user_id: str, chat_id: str,
                       timers: UserTimers) -> Optional[dict]:
        """停止番茄钟，返回新卡片 dict 或 None"""
        with self._lock:
            state = timers.pomodoro
            if not state or state.status in ("idle", "completed"):
                self.bot.reply(chat_id, "当前没有进行中的番茄钟。")
                return None

            self._cancel_pomodoro_job(state)
            # 统计当前工作阶段的已用时间并持久化
            if state.phase == Phase.WORK:
                if state.status == "running":
                    elapsed = time.time() - state.phase_start_time
                    state.total_focus_seconds += elapsed
                    self._stats.add_focus(user_id, elapsed)
                elif state.status == "paused":
                    worked = state.phase_duration_seconds - state.remaining_seconds
                    state.total_focus_seconds += worked
                    self._stats.add_focus(user_id, worked)
            state.status = "completed"
            self._stats.increment_sessions(user_id)
            return cards.build_pomodoro_completed_card(state)

    def _start_next_phase(self, user_id: str, chat_id: str,
                          timers: UserTimers) -> Optional[dict]:
        """用户确认开始下一阶段（从 waiting 状态启动计时）"""
        with self._lock:
            state = timers.pomodoro
            if not state or state.status != "waiting":
                return None

            # 判断下一阶段
            if state.phase == Phase.WORK and state.rest_seconds > 0:
                # 工作结束，有休息 → 开始休息
                state.phase = Phase.REST
            elif state.phase == Phase.WORK:
                # 工作结束，无休息 → 直接开始下一轮工作（cycle 已在 _on_phase_end 中递增）
                pass  # phase 保持 WORK
            else:
                # 休息结束 → 开始下一轮工作
                state.phase = Phase.WORK

            state.status = "running"
            state.phase_start_time = time.time()
            self._schedule_phase_end(user_id, chat_id, state)
            self._start_refresh_job(user_id, chat_id, state)
            return cards.build_pomodoro_running_card(state)

    def _skip_rest(self, user_id: str, chat_id: str,
                   timers: UserTimers) -> Optional[dict]:
        """跳过休息，直接进入下一轮工作或结束（从 waiting 状态）

        此方法从"工作结束，等待开始休息"的 waiting 状态调用。
        此时 current_cycle 未递增（WORK→REST 分支不递增），需要在这里递增。
        """
        with self._lock:
            state = timers.pomodoro
            if not state or state.status != "waiting":
                return None

            if state.current_cycle < state.total_cycles:
                # 还有下一轮 → 跳过休息，递增轮次，进入下一轮工作
                state.current_cycle += 1
                state.phase = Phase.WORK
                state.status = "running"
                state.phase_start_time = time.time()
                self._schedule_phase_end(user_id, chat_id, state)
                self._start_refresh_job(user_id, chat_id, state)
                return cards.build_pomodoro_running_card(state)
            else:
                # 最后一轮 → 跳过休息直接完成
                state.status = "completed"
                self._stats.increment_sessions(user_id)
                return cards.build_pomodoro_completed_card(state)

    # ──────────────── 定时提醒 ────────────────

    def _create_reminder(self, user_id: str, chat_id: str, timers: UserTimers,
                         name: str, cron_expr: str, message: str) -> None:
        """创建定时提醒"""
        remind_id = f"r_{user_id[:8]}_{int(time.time())}"
        job_id = f"remind_{remind_id}"

        try:
            trigger = CronTrigger.from_crontab(cron_expr)
        except ValueError as e:
            self.bot.reply(
                chat_id,
                f"cron 表达式格式错误: {e}\n"
                "格式: `分 时 日 月 周`\n"
                "示例: `0 8-20 * * *`（每天8-20点整点）",
            )
            return

        self._scheduler.add_job(
            self._on_reminder_fire,
            trigger=trigger,
            args=[user_id, chat_id, remind_id],
            id=job_id,
        )

        reminder = ReminderState(
            remind_id=remind_id,
            name=name,
            cron_expr=cron_expr,
            message=message,
            job_id=job_id,
        )
        timers.reminders[remind_id] = reminder

        self.bot.reply_card(chat_id, cards.build_reminder_list_card(timers.reminders))

    def _on_reminder_fire(self, user_id: str, chat_id: str,
                          remind_id: str) -> None:
        """提醒触发回调（APScheduler 后台线程调用）"""
        try:
            timers = self._user_timers.get(self._state_key(user_id, chat_id))
            if not timers:
                return
            reminder = timers.reminders.get(remind_id)
            if not reminder or not reminder.active:
                return
            self.bot.reply_card(chat_id,
                                cards.build_reminder_notification_card(reminder))
        except Exception as e:
            logger.error("提醒触发异常: remind_id=%s, error=%s",
                         remind_id, e, exc_info=True)

    def _toggle_reminder(self, user_id: str, chat_id: str, timers: UserTimers,
                         remind_id: str) -> None:
        """暂停/恢复提醒"""
        reminder = timers.reminders.get(remind_id)
        if not reminder:
            return
        try:
            if reminder.active:
                self._scheduler.pause_job(reminder.job_id)
                reminder.active = False
            else:
                self._scheduler.resume_job(reminder.job_id)
                reminder.active = True
        except JobLookupError:
            pass

    def _delete_reminder(self, user_id: str, chat_id: str, timers: UserTimers,
                         remind_id: str) -> None:
        """删除提醒"""
        reminder = timers.reminders.pop(remind_id, None)
        if reminder:
            try:
                self._scheduler.remove_job(reminder.job_id)
            except JobLookupError:
                pass

    # ──────────────── 卡片动作处理 ────────────────

    def handle_card_action(
        self, user_id: str, chat_id: str, message_id: str, action_value: dict
    ) -> "P2CardActionTriggerResponse":
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

        action = action_value.get("action", "")
        timers = self._get_timers(user_id, chat_id)
        logger.info("[番茄钟][card_action] action=%s, user=%s, chat=%s",
                    action, user_id[:8], chat_id[:8])

        # 表单提交
        if not action and "_form_value" in action_value:
            return self._handle_form_submit(
                user_id, chat_id, message_id, timers, action_value)

        # 有时表单提交时 action 也有值
        if action == "pomo_start" and "_form_value" in action_value:
            return self._handle_form_submit(
                user_id, chat_id, message_id, timers, action_value)
        if action == "reminder_submit" and "_form_value" in action_value:
            return self._handle_form_submit(
                user_id, chat_id, message_id, timers, action_value)

        # 番茄钟操作 — 通过 card response 原地更新卡片
        if action == "pomo_pause":
            card = self._pause_pomodoro(user_id, chat_id, timers)
            if card:
                return self.bot.make_card_response(card=card)
        elif action == "pomo_resume":
            card = self._resume_pomodoro(user_id, chat_id, timers)
            if card:
                return self.bot.make_card_response(card=card)
        elif action == "pomo_skip":
            # 记录跳过前的标签和轮次，用于灰色卡片
            state = timers.pomodoro
            if state and state.status in ("running", "paused"):
                label = state.title if state.phase == Phase.WORK else "休息"
                cycle = state.current_cycle
                self._skip_phase(user_id, chat_id, timers)
                grey = cards.build_phase_finished_card(
                    state, label, "已跳过", cycle=cycle)
                return self.bot.make_card_response(card=grey)
        elif action == "pomo_stop":
            state = timers.pomodoro
            if state and state.status not in ("idle", "completed"):
                label = state.title if state.phase == Phase.WORK else "休息"
                cycle = state.current_cycle
                completed = self._stop_pomodoro(user_id, chat_id, timers)
                if completed:
                    # 发送完成统计卡片作为新消息
                    self._send_card_as_new(chat_id, timers, completed)
                grey = cards.build_phase_finished_card(
                    state, label, "已停止", cycle=cycle)
                return self.bot.make_card_response(card=grey)
        elif action == "pomo_start_next":
            card = self._start_next_phase(user_id, chat_id, timers)
            if card:
                self._set_card_message_id(timers, message_id)
                return self.bot.make_card_response(card=card)
        elif action == "pomo_skip_rest":
            card = self._skip_rest(user_id, chat_id, timers)
            if card:
                self._set_card_message_id(timers, message_id)
                return self.bot.make_card_response(card=card)
        elif action == "pomo_confirm_replace":
            # 用上次保存的设置（即用户刚提交的新表单值）启动
            ls = timers.last_settings
            card = self._start_pomodoro(
                user_id, chat_id, timers,
                ls.work_seconds, ls.rest_seconds,
                ls.cycles, confirmed=True,
                title=ls.title)
            if card:
                self._set_card_message_id(timers, message_id)
                return self.bot.make_card_response(card=card)
        elif action == "pomo_cancel_replace":
            return self.bot.make_card_response(toast="已取消")
        elif action == "pomo_restart":
            old = timers.pomodoro
            if old:
                # "再来一轮"：在已完成基础上追加一个周期，延续统计
                card = self._start_pomodoro(
                    user_id, chat_id, timers,
                    old.work_seconds, old.rest_seconds,
                    old.total_cycles + 1, confirmed=True,
                    start_cycle=old.total_cycles + 1,
                    carry_focus=old.total_focus_seconds,
                    carry_work_phases=old.completed_work_phases,
                    title=old.title)
                if card:
                    self._set_card_message_id(timers, message_id)
                    return self.bot.make_card_response(card=card)

        # 菜单 / 设置 — 原地切换页面
        elif action == "show_setup":
            return self.bot.make_card_response(
                card=cards.build_pomodoro_setup_card(timers.last_settings))
        elif action == "show_stats":
            today = self._stats.load_today(user_id)
            total_secs, total_days, total_phases = self._stats.load_total(user_id)
            return self.bot.make_card_response(
                card=cards.build_stats_card(
                    today, total_secs, total_days, total_phases))
        elif action == "clear_stats":
            return self.bot.make_card_response(
                card=cards.build_confirm_clear_stats_card())
        elif action == "confirm_clear_stats":
            self._stats.clear(user_id)
            from .models import DailyStats
            return self.bot.make_card_response(
                card=cards.build_stats_card(DailyStats(), 0, 0, 0),
                toast="统计数据已清空")

        # 提醒操作 — 原地切换页面
        elif action == "reminder_create":
            return self.bot.make_card_response(
                card=cards.build_reminder_setup_card())
        elif action == "show_reminders":
            return self.bot.make_card_response(
                card=cards.build_reminder_list_card(timers.reminders))
        elif action == "reminder_toggle":
            self._toggle_reminder(
                user_id, chat_id, timers,
                action_value.get("remind_id", ""))
            return self.bot.make_card_response(
                card=cards.build_reminder_list_card(timers.reminders))
        elif action == "reminder_delete":
            self._delete_reminder(
                user_id, chat_id, timers,
                action_value.get("remind_id", ""))
            return self.bot.make_card_response(
                card=cards.build_reminder_list_card(timers.reminders))
        elif action == "reminder_template":
            self._create_from_template(
                user_id, chat_id, timers, action_value)
            return self.bot.make_card_response(
                card=cards.build_reminder_list_card(timers.reminders))

        return P2CardActionTriggerResponse()

    def _handle_form_submit(
        self, user_id: str, chat_id: str, message_id: str,
        timers: UserTimers, action_value: dict,
    ) -> "P2CardActionTriggerResponse":
        """处理表单提交"""
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

        form = action_value.get("_form_value", {})
        action = action_value.get("action", "")

        # 番茄钟设置表单（通过 work_m 字段识别）
        if "work_m" in form:
            title = form.get("pomo_title", "").strip() or "工作"
            work_secs = self._parse_hms(form, "work")
            rest_secs = self._parse_hms(form, "rest")
            cycles = self._parse_int(form.get("cycles", "1"), 1)

            if work_secs < 1 or work_secs > 12 * 3600:
                return self.bot.make_card_response(
                    toast="工作时长需在 1秒 ~ 12小时之间", toast_type="error")
            if rest_secs < 0 or rest_secs > 3600:
                return self.bot.make_card_response(
                    toast="休息时长需在 0 ~ 1小时之间", toast_type="error")
            if cycles < 1 or cycles > 20:
                return self.bot.make_card_response(
                    toast="周期数需在 1-20 之间", toast_type="error")

            # 保存设置参数，下次打开设置页时回显
            timers.last_settings = LastSettings(
                title=title, work_seconds=work_secs,
                rest_seconds=rest_secs, cycles=cycles)

            card = self._start_pomodoro(user_id, chat_id, timers,
                                        work_seconds=work_secs, rest_seconds=rest_secs,
                                        cycles=cycles, title=title)
            if card:
                self._set_card_message_id(timers, message_id)
                return self.bot.make_card_response(card=card)
            return P2CardActionTriggerResponse()

        # 提醒表单
        if "reminder_name" in form:
            name = form.get("reminder_name", "").strip()
            cron_expr = form.get("reminder_cron", "").strip()
            message = form.get("reminder_message", "").strip()

            if not name:
                return self.bot.make_card_response(
                    toast="请填写提醒名称", toast_type="error")
            if not cron_expr:
                return self.bot.make_card_response(
                    toast="请填写 cron 表达式", toast_type="error")
            if not message:
                return self.bot.make_card_response(
                    toast="请填写提醒内容", toast_type="error")

            self._create_reminder(user_id, chat_id, timers,
                                  name, cron_expr, message)
            return P2CardActionTriggerResponse()

        return P2CardActionTriggerResponse()

    def _create_from_template(self, user_id: str, chat_id: str,
                              timers: UserTimers, action_value: dict) -> None:
        """从预设模板创建提醒"""
        template_key = action_value.get("template", "")
        template = _REMINDER_TEMPLATES.get(template_key)
        if not template:
            return
        self._create_reminder(user_id, chat_id, timers, **template)

    # ──────────────── 辅助方法 ────────────────

    # 卡片刷新间隔（秒）
    _REFRESH_INTERVAL = 5

    def _cancel_pomodoro_job(self, state: PomodoroState) -> None:
        """安全移除 APScheduler job（阶段结束 + 刷新）"""
        for jid_attr in ("job_id", "refresh_job_id"):
            jid = getattr(state, jid_attr, None)
            if jid:
                try:
                    self._scheduler.remove_job(jid)
                    logger.info("[番茄钟] cancel %s=%s 成功", jid_attr, jid)
                except JobLookupError:
                    logger.info("[番茄钟] cancel %s=%s: job已不存在", jid_attr, jid)
                except Exception as e:
                    logger.warning("[番茄钟] cancel %s=%s 异常: %s", jid_attr, jid, e)
                setattr(state, jid_attr, None)
            else:
                logger.info("[番茄钟] cancel %s: 无job_id，跳过", jid_attr)

    def _start_refresh_job(self, user_id: str, chat_id: str,
                           state: PomodoroState) -> None:
        """启动周期性卡片刷新任务"""
        self._stop_refresh_job(state)
        refresh_id = f"refresh_{user_id}_{chat_id}_{time.time_ns()}"
        self._scheduler.add_job(
            self._refresh_card,
            trigger="interval",
            seconds=self._REFRESH_INTERVAL,
            args=[user_id, chat_id],
            id=refresh_id,
        )
        state.refresh_job_id = refresh_id
        logger.info("[番茄钟] 启动 refresh job=%s, interval=%ds",
                    refresh_id, self._REFRESH_INTERVAL)
        # 打印当前所有 job 用于排查
        all_jobs = self._scheduler.get_jobs()
        logger.info("[番茄钟] 当前 scheduler 中共 %d 个 job: %s",
                    len(all_jobs), [j.id for j in all_jobs])

    def _stop_refresh_job(self, state: PomodoroState) -> None:
        """停止周期性卡片刷新任务"""
        if state.refresh_job_id:
            try:
                self._scheduler.remove_job(state.refresh_job_id)
            except (JobLookupError, Exception):
                pass
            state.refresh_job_id = None

    def _refresh_card(self, user_id: str, chat_id: str) -> None:
        """定时刷新卡片上的剩余时间（APScheduler 后台线程调用）

        patch_message 在锁内执行，确保不会与 pause/stop 的卡片更新交叉。
        """
        tid = threading.current_thread().name
        try:
            with self._lock:
                timers = self._user_timers.get(self._state_key(user_id, chat_id))
                if not timers or not timers.pomodoro:
                    logger.info("[番茄钟][refresh][%s] 无 timers/pomodoro，跳过", tid)
                    return
                state = timers.pomodoro
                if state.status != "running" or not state.card_message_id:
                    logger.info("[番茄钟][refresh][%s] status=%s，跳过 patch",
                                tid, state.status)
                    return
                card = cards.build_pomodoro_running_card(state)
                logger.info("[番茄钟][refresh][%s] status=%s, 正在 patch msg=%s",
                            tid, state.status, state.card_message_id)
                self.bot.patch_message(
                    state.card_message_id, json.dumps(card))
                logger.info("[番茄钟][refresh][%s] patch 完成", tid)
        except Exception as e:
            logger.warning("[番茄钟][refresh][%s] 异常: %s", tid, e)

    @staticmethod
    def _set_card_message_id(timers: UserTimers, message_id: str) -> None:
        """将当前卡片的 message_id 记录到 state，供 refresh 使用"""
        if timers.pomodoro:
            timers.pomodoro.card_message_id = message_id

    def _send_card_as_new(self, chat_id: str, timers: UserTimers,
                          card: dict) -> None:
        """发送新卡片并记录 message_id（用于文本指令触发的场景）"""
        msg_id = self.bot.send_message_get_id(
            chat_id, "interactive", json.dumps(card))
        if timers.pomodoro:
            timers.pomodoro.card_message_id = msg_id

    @staticmethod
    def _parse_int(value: str, default: int) -> int:
        """安全解析整数"""
        try:
            return int(value)
        except (ValueError, TypeError):
            return default

    @classmethod
    def _parse_hms(cls, form: dict, prefix: str) -> int:
        """从表单的 {prefix}_h / {prefix}_m / {prefix}_s 解析出总秒数"""
        h = cls._parse_int(form.get(f"{prefix}_h", "0"), 0)
        m = cls._parse_int(form.get(f"{prefix}_m", "0"), 0)
        s = cls._parse_int(form.get(f"{prefix}_s", "0"), 0)
        return h * 3600 + m * 60 + s
