"""
番茄钟插件数据模型
"""

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

# 北京时间
_BJT = timezone(timedelta(hours=8))


def fmt_duration(seconds: float) -> str:
    """将秒数格式化为人类可读的时长描述（如 '1小时30分钟'、'25分钟'、'30秒'）"""
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} 小时")
    if m:
        parts.append(f"{m} 分钟")
    if s or not parts:
        parts.append(f"{s} 秒")
    return "".join(parts)


class Phase(str, Enum):
    """番茄钟阶段"""
    WORK = "work"
    REST = "rest"


@dataclass
class PomodoroState:
    """单个 (user_id, chat_id) 的番茄钟状态"""

    # 基本设置（以秒为单位存储，支持时/分/秒精度）
    title: str = "工作"
    work_seconds: int = 25 * 60
    rest_seconds: int = 5 * 60
    total_cycles: int = 1
    current_cycle: int = 1
    phase: Phase = Phase.WORK

    # 运行状态: idle / running / paused / completed / waiting
    # waiting: 阶段结束，等待用户确认开始下一阶段
    status: str = "idle"
    job_id: Optional[str] = None
    phase_start_time: float = 0.0
    remaining_seconds: float = 0.0

    # 消息追踪（用于原地更新卡片）
    card_message_id: Optional[str] = None
    refresh_job_id: Optional[str] = None

    # 统计
    completed_work_phases: int = 0
    total_focus_seconds: float = 0.0

    @property
    def phase_duration_seconds(self) -> float:
        """当前阶段的总时长（秒）"""
        return self.work_seconds if self.phase == Phase.WORK else self.rest_seconds

    @property
    def time_left_seconds(self) -> float:
        """当前阶段剩余时间（秒）"""
        if self.status == "paused":
            return self.remaining_seconds
        if self.status != "running":
            return 0.0
        elapsed = time.time() - self.phase_start_time
        left = self.phase_duration_seconds - elapsed
        return max(0.0, left)

    @property
    def end_time_str(self) -> str:
        """当前阶段的预计结束时间（北京时间 HH:MM:SS 格式）"""
        if self.status == "running":
            end_ts = self.phase_start_time + self.phase_duration_seconds
        elif self.status == "paused":
            return f"恢复后还需 {fmt_duration(self.remaining_seconds)}"
        else:
            return "--:--:--"
        end_dt = datetime.fromtimestamp(end_ts, tz=_BJT)
        return end_dt.strftime("%H:%M:%S")

    @property
    def work_duration_str(self) -> str:
        """工作时长的可读描述"""
        return fmt_duration(self.work_seconds)

    @property
    def rest_duration_str(self) -> str:
        """休息时长的可读描述"""
        return fmt_duration(self.rest_seconds)


@dataclass
class ReminderState:
    """一条定时提醒"""
    remind_id: str
    name: str
    cron_expr: str
    message: str
    job_id: str
    active: bool = True
    created_at: float = field(default_factory=time.time)


@dataclass
class LastSettings:
    """记录上次番茄钟的设置参数"""
    title: str = "工作"
    work_seconds: int = 25 * 60
    rest_seconds: int = 5 * 60
    cycles: int = 1


@dataclass
class UserTimers:
    """单个 (user_id, chat_id) 的所有定时器"""
    pomodoro: Optional[PomodoroState] = None
    reminders: dict[str, ReminderState] = field(default_factory=dict)
    last_settings: LastSettings = field(default_factory=LastSettings)
