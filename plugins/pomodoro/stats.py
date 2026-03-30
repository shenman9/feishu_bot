"""
番茄钟插件 — 专注统计持久化

按 user_id 存储，跨群聊共享统计。
每个用户一个 JSON 文件: plugins/pomodoro/stats/{user_id}.json
"""

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import DailyStats

logger = logging.getLogger(__name__)

# 北京时间
_BJT = timezone(timedelta(hours=8))
_STATS_DIR = Path(__file__).parent / "stats"


def _today_str() -> str:
    """当前北京时间日期字符串"""
    return datetime.now(tz=_BJT).strftime("%Y-%m-%d")


class FocusStatsStore:
    """按用户持久化专注统计"""

    def __init__(self):
        self._lock = threading.Lock()
        _STATS_DIR.mkdir(exist_ok=True)

    def _user_path(self, user_id: str) -> Path:
        return _STATS_DIR / f"{user_id}.json"

    def _load_raw(self, user_id: str) -> dict:
        """加载用户的完整统计数据"""
        path = self._user_path(user_id)
        if not path.exists():
            return {"daily": {}}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("读取统计文件失败: %s, %s", path, e)
            return {"daily": {}}

    def _save_raw(self, user_id: str, data: dict) -> None:
        """保存用户的完整统计数据"""
        path = self._user_path(user_id)
        try:
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            logger.error("写入统计文件失败: %s, %s", path, e)

    def load_today(self, user_id: str) -> DailyStats:
        """加载今日统计"""
        with self._lock:
            data = self._load_raw(user_id)
            day = data.get("daily", {}).get(_today_str(), {})
            return DailyStats(
                focus_seconds=day.get("focus_seconds", 0.0),
                sessions=day.get("sessions", 0),
                work_phases=day.get("work_phases", 0),
            )

    def load_total(self, user_id: str) -> tuple[float, int, int]:
        """加载累计统计

        Returns:
            (总专注秒数, 有记录的天数, 总工作周期数)
        """
        with self._lock:
            data = self._load_raw(user_id)
            daily = data.get("daily", {})
            total_seconds = 0.0
            total_days = 0
            total_phases = 0
            for day_data in daily.values():
                secs = day_data.get("focus_seconds", 0.0)
                if secs > 0:
                    total_days += 1
                total_seconds += secs
                total_phases += day_data.get("work_phases", 0)
            return total_seconds, total_days, total_phases

    def add_focus(self, user_id: str, seconds: float,
                  work_phases: int = 0) -> None:
        """累加专注时间到今日记录（线程安全）"""
        if seconds <= 0 and work_phases <= 0:
            return
        with self._lock:
            data = self._load_raw(user_id)
            daily = data.setdefault("daily", {})
            today = _today_str()
            day = daily.setdefault(today, {
                "focus_seconds": 0.0, "sessions": 0, "work_phases": 0,
            })
            day["focus_seconds"] = day.get("focus_seconds", 0.0) + seconds
            day["work_phases"] = day.get("work_phases", 0) + work_phases
            self._save_raw(user_id, data)

    def increment_sessions(self, user_id: str) -> None:
        """番茄钟完成时，累加今日会话数"""
        with self._lock:
            data = self._load_raw(user_id)
            daily = data.setdefault("daily", {})
            today = _today_str()
            day = daily.setdefault(today, {
                "focus_seconds": 0.0, "sessions": 0, "work_phases": 0,
            })
            day["sessions"] = day.get("sessions", 0) + 1
            self._save_raw(user_id, data)

    def clear(self, user_id: str) -> None:
        """清空用户的所有历史统计数据"""
        with self._lock:
            path = self._user_path(user_id)
            if path.exists():
                path.unlink()
