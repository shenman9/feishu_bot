"""
Claude Code 会话持久化存储

管理历史会话的 JSON 文件读写、过期清理，
以及 Claude Code JSONL 会话文件的定位与解析。
"""

import datetime
import json
import logging
import os
import pathlib
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# 未收藏会话过期天数（超过此天数未活跃则自动清理）
_SESSION_EXPIRE_DAYS = 7


class SessionStore:
    """会话持久化存储，封装文件读写和锁"""

    def __init__(self, data_dir: pathlib.Path):
        self._data_dir = data_dir
        self._lock = threading.Lock()

    def _file_path(self) -> pathlib.Path:
        """返回历史会话存储文件路径"""
        return self._data_dir / "feishu_sessions.json"

    def load_user_sessions(self, user_id: str) -> list[dict]:
        """读取指定用户的历史会话列表（文件不存在则返回空列表）"""
        path = self._file_path()
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                return data.get(user_id, [])
        except Exception as e:
            logger.warning("[CC] 读取历史会话文件失败: %s", e)
        return []

    def upsert_session(
        self, user_id: str, session_id: str, working_dir: str, title: str
    ) -> None:
        """新增或更新一条历史会话记录，清理过期会话（超过 _SESSION_EXPIRE_DAYS 天未活跃且未收藏）"""
        now = datetime.datetime.utcnow().isoformat(timespec="seconds")
        path = self._file_path()

        with self._lock:
            # 读取全量数据
            try:
                data: dict = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            except Exception as e:
                logger.warning("[CC] 读取历史会话文件失败，重置: %s", e)
                data = {}

            sessions: list[dict] = data.get(user_id, [])

            # 查找是否已存在该 session_id
            existing = next((s for s in sessions if s["session_id"] == session_id), None)
            if existing:
                existing["last_activity"] = now
                # 用户未手动重命名过的会话，自动更新标题为最新一条消息
                if not existing.get("title_customized"):
                    existing["title"] = title
            else:
                sessions.insert(0, {
                    "session_id": session_id,
                    "working_dir": working_dir,
                    "title": title,
                    "created_at": now,
                    "last_activity": now,
                    "starred": False,
                })

            # 按最近活跃时间倒序，清理过期会话（超过指定天数未活跃且未收藏）
            sessions.sort(key=lambda s: s["last_activity"], reverse=True)
            cutoff = (
                datetime.datetime.utcnow()
                - datetime.timedelta(days=_SESSION_EXPIRE_DAYS)
            ).isoformat(timespec="seconds")
            data[user_id] = [
                s for s in sessions
                if s.get("starred") or s.get("last_activity", "") >= cutoff
            ]

            # 写回文件（原子操作：先写临时文件，再 rename 替换）
            self._write_file(path, data)

    def toggle_star(self, user_id: str, session_id: str) -> bool | None:
        """切换会话收藏状态，返回新状态；会话不存在时返回 None"""
        path = self._file_path()
        with self._lock:
            try:
                data: dict = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            except Exception:
                return None
            sessions: list[dict] = data.get(user_id, [])
            target = next((s for s in sessions if s["session_id"] == session_id), None)
            if not target:
                return None
            new_starred = not target.get("starred", False)
            target["starred"] = new_starred
            data[user_id] = sessions
            self._write_file(path, data)
            return new_starred

    def rename_session(self, user_id: str, session_id: str, new_title: str) -> bool:
        """重命名会话标题，返回是否成功"""
        path = self._file_path()
        with self._lock:
            try:
                data: dict = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            except Exception:
                return False
            sessions: list[dict] = data.get(user_id, [])
            target = next((s for s in sessions if s["session_id"] == session_id), None)
            if not target:
                return False
            target["title"] = new_title
            target["title_customized"] = True
            data[user_id] = sessions
            self._write_file(path, data)
            return True

    def _write_file(self, path: pathlib.Path, data: dict) -> None:
        """原子写入会话文件（先写临时文件，再 rename 替换）"""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(".json.tmp")
            tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(str(tmp_path), str(path))
        except Exception as e:
            logger.error("[CC] 写入历史会话文件失败: %s", e)


# ---- 模块级纯函数：不需要 data_dir 和锁 ----


def find_session_jsonl(session_id: str, working_dir: str) -> Optional[pathlib.Path]:
    """定位 Claude Code 会话的 JSONL 文件

    优先按 working_dir 编码路径查找，找不到则全局搜索。
    """
    claude_projects = pathlib.Path.home() / ".claude" / "projects"
    if not claude_projects.exists():
        return None
    # 优先：按 working_dir 构造路径（将 / 全替换为 -）
    if working_dir:
        encoded = working_dir.replace("/", "-")
        candidate = claude_projects / encoded / f"{session_id}.jsonl"
        if candidate.exists():
            return candidate
    # 回退：全局搜索（working_dir 可能已变更或编码规则有差异）
    for path in claude_projects.glob(f"*/{session_id}.jsonl"):
        return path
    return None


def parse_session_rounds(
    jsonl_path: pathlib.Path, n: int
) -> list[tuple[str, str]]:
    """解析 JSONL，返回最后 n 轮 (user_text, assistant_text) 对

    跳过 progress、tool_use、thinking 等非文本条目。
    """
    rounds: list[tuple[str, str]] = []
    pending_user: Optional[str] = None

    try:
        with open(jsonl_path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = entry.get("type")
                if msg_type == "user":
                    content = entry.get("message", {}).get("content", "")
                    if isinstance(content, str):
                        text = content.strip()
                        if text:
                            pending_user = text
                    elif isinstance(content, list):
                        parts = [
                            c.get("text", "") for c in content
                            if c.get("type") == "text"
                        ]
                        text = "\n".join(parts).strip()
                        # 过滤工具调用结果（tool_result 类型的 user 消息无文本内容）
                        if text:
                            pending_user = text
                elif msg_type == "assistant" and pending_user is not None:
                    content = entry.get("message", {}).get("content", [])
                    if isinstance(content, list):
                        parts = [
                            c.get("text", "") for c in content
                            if c.get("type") == "text"
                        ]
                        assistant_text = "\n".join(parts).strip()
                        if assistant_text:
                            rounds.append((pending_user, assistant_text))
                            pending_user = None
    except Exception as e:
        logger.warning("[CC] 读取会话历史失败: %s", e)

    return rounds[-n:]
