"""
Claude Code 桥接插件

通过 subprocess 调用本地 Claude Code CLI，
将飞书消息作为 prompt 发送，实时流式回显结果到飞书卡片。
支持会话持续（--session-id）、取消运行、清空会话、切换工作目录等操作。
支持交互式权限确认：通过 PermissionRequest Hook + HTTP 服务器，
将 Claude Code 的权限请求转发给飞书用户确认。
"""

import difflib
import datetime
import json
import logging
import os
import pathlib
import pwd
import select
import subprocess
import threading
import time
import uuid
from typing import Optional

from config import load_config
from core.plugin import Plugin
from plugins.claude_code.permission_server import PermissionServer

logger = logging.getLogger(__name__)

# 流式更新控制（与 claude_chat 一致）
_PATCH_INTERVAL = 0.5       # 最小更新间隔（秒）
_PATCH_MIN_CHARS = 50       # 最小新增字符数触发更新
_IDLE_PATCH_INTERVAL = 2.0  # 无新数据时进度提示刷新间隔（秒）

# 工具调用日志显示控制
_MAX_STREAK_DISPLAY = 15    # 连续工具调用段最多显示条数（超出则折叠）
_TOOL_PARAM_MAX = 60        # 工具参数摘要最大字符数

# 默认配置
_DEFAULT_TIMEOUT = 600          # 默认超时 10 分钟
_DEFAULT_MAX_OUTPUT = 28000     # 飞书卡片 markdown 最大字符数
_DEFAULT_MAX_TURNS = 50         # Claude Code 最大轮次
_DEFAULT_PERM_PORT = 9876       # 权限确认 HTTP 服务器默认端口
_DEFAULT_PERM_TIMEOUT = 120     # 用户确认超时（秒）
_MAX_SESSIONS_PER_USER = 20     # 每用户历史会话最大保留条数

# 插件运行时数据目录：<项目根目录>/data/claude_code/（不提交 VCS）
_PLUGIN_DIR = pathlib.Path(__file__).parent      # plugins/claude_code/
_PROJECT_ROOT = _PLUGIN_DIR.parent.parent        # feishu_bot/
_CC_DATA_DIR = _PROJECT_ROOT / "data" / "claude_code"

PLUGIN_KEYWORD = "CC"


def _display_path(path: str, base: str = "") -> str:
    """格式化路径用于展示：若相对路径更短则优先使用

    Args:
        path: 原始路径（若非绝对路径则原样返回）
        base: 参考基准目录（通常为工作目录），用于计算相对路径

    Returns:
        原路径、相对于 base 的路径、以及 ~ 缩写中最短的一个
    """
    if not path or not os.path.isabs(path):
        return path
    candidates = [path]
    # 尝试相对于 base 的路径
    if base:
        try:
            rel = os.path.relpath(path, base)
            candidates.append(rel)
        except ValueError:
            pass
    # 尝试 ~ 缩写
    home = os.path.expanduser("~")
    if path == home:
        candidates.append("~")
    elif path.startswith(home + os.sep):
        candidates.append("~" + path[len(home):])
    return min(candidates, key=len)


def _resolve_working_dir(raw: str = "") -> str:
    """将 working_dir 配置值解析为有效绝对路径

    使用 realpath 归一化，解析符号链接和 .. 等相对路径组件。
    空字符串表示"使用默认目录"，回落到进程当前工作目录 os.getcwd()。
    这样 state["working_dir"] 始终持有真实绝对路径，避免下游散落多处 fallback。
    """
    return os.path.realpath(raw) if raw else os.path.realpath(os.getcwd())


class ClaudeCodePlugin(Plugin):
    """Claude Code 桥接插件

    在服务器本地通过 subprocess 调用 claude CLI，
    将用户飞书消息作为 prompt，流式回显 Claude Code 的输出。
    """

    # 特殊指令定义表（统一维护，供激活消息和 /help 复用）
    # brief: 激活欢迎词中的简短说明（None 表示不在简短列表中显示）
    # detail: /help 中的详细说明
    _SPECIAL_COMMANDS: list[dict] = [
        {"usage": "/new",       "brief": "重置会话",                    "detail": "重置当前会话（清除上下文，开启新对话）"},
        {"usage": "/session",   "brief": "查看并恢复历史会话",             "detail": "列出最近历史会话，可点击选择恢复"},
        {"usage": "/cancel",    "brief": "终止运行中的任务",             "detail": "终止当前正在运行的任务"},
        {"usage": "/status",    "brief": "查看当前状态",                 "detail": "查看当前会话状态（目录、session、权限模式等）"},
        {"usage": "/permission", "brief": "切换权限确认模式",              "detail": "弹出权限模式选择卡片，可选 interactive / accept_edits / bypass"},
        {"usage": "/cd <路径>",    "brief": "切换工作目录（会同时重置会话）", "detail": "切换工作目录并重置会话"},
        {"usage": "/cd",        "brief": None,                          "detail": "重置工作目录为默认并重置会话"},
        {"usage": "/help",      "brief": "查看帮助信息",                 "detail": "显示此帮助信息"},
    ]

    def __init__(self):
        super().__init__()
        # user_id -> 用户状态
        self.user_states: dict[str, dict] = {}
        # user_id -> 运行中的子进程
        self._running_processes: dict[str, subprocess.Popen] = {}
        # user_id -> 运行中的线程
        self._running_threads: dict[str, threading.Thread] = {}
        self._config: Optional[dict] = None
        # 权限确认服务器（懒初始化）
        self._perm_server: Optional[PermissionServer] = None
        self._perm_server_started = False
        self._perm_server_lock = threading.Lock()
        # 历史会话文件读写锁
        self._sessions_lock = threading.Lock()

    # ---- 元信息 ----

    @property
    def name(self) -> str:
        return "Claude Code"

    @property
    def keyword(self) -> str:
        return PLUGIN_KEYWORD

    @property
    def description(self) -> str:
        return "通过飞书远程使用 Claude Code"

    @classmethod
    def _commands_brief(cls) -> str:
        """生成激活欢迎词中的简短指令列表（brief 为 None 的条目跳过）"""
        lines = [
            f"- `{cmd['usage']}` {cmd['brief']}"
            for cmd in cls._SPECIAL_COMMANDS
            if cmd["brief"] is not None
        ]
        return "\n".join(lines)

    @classmethod
    def _commands_detail(cls) -> str:
        """生成 /help 中的详细指令列表（包含所有条目）"""
        lines = [
            f"• `{cmd['usage']}` — {cmd['detail']}"
            for cmd in cls._SPECIAL_COMMANDS
        ]
        return "\n".join(lines)

    # ---- 配置 ----

    def _load_plugin_config(self) -> dict:
        """懒加载插件配置"""
        if self._config is None:
            cfg = load_config()
            cc_cfg = cfg.get("claude_code", {})
            self._config = {
                "claude_path": cc_cfg.get("claude_path", "/usr/bin/claude"),
                "default_working_dir": cc_cfg.get("default_working_dir", ""),
                "timeout": cc_cfg.get("timeout", _DEFAULT_TIMEOUT),
                "max_output_chars": cc_cfg.get("max_output_chars", _DEFAULT_MAX_OUTPUT),
                "default_perm_mode": cc_cfg.get("default_perm_mode", "interactive"),
                "max_turns": cc_cfg.get("max_turns", _DEFAULT_MAX_TURNS),
                "run_as_user": cc_cfg.get("run_as_user", ""),
                "permission_server_port": cc_cfg.get("permission_server_port", _DEFAULT_PERM_PORT),
                "permission_timeout": cc_cfg.get("permission_timeout", _DEFAULT_PERM_TIMEOUT),
            }
        return self._config

    # ---- 状态管理 ----

    def _get_state(self, user_id: str) -> dict:
        """获取用户会话状态，不存在则初始化"""
        if user_id not in self.user_states:
            cfg = self._load_plugin_config()
            default_perm = cfg["default_perm_mode"]
            # manual_select 模式：新会话暂用 interactive 作为安全默认，创建后弹卡片由用户选择
            init_perm = "interactive" if default_perm == "manual_select" else default_perm
            self.user_states[user_id] = {
                "active": False,
                "session_id": str(uuid.uuid4()),
                "session_started": False,
                "running": False,
                "working_dir": _resolve_working_dir(cfg["default_working_dir"]),
                "last_chat_id": "",
                "session_perm_mode": init_perm,  # 会话级权限模式: interactive / bypass / accept_edits
                "perm_timeout_count": 0,  # 当前任务中权限确认超时次数
            }
        return self.user_states[user_id]

    def is_user_active(self, user_id: str) -> bool:
        """用户是否在活跃会话中"""
        return self._get_state(user_id).get("active", False)

    def deactivate_user(self, user_id: str) -> None:
        """清理用户状态，终止运行中的进程"""
        self._kill_process(user_id)
        self.user_states.pop(user_id, None)

    def _reset_session(self, user_id: str) -> str:
        """重置会话状态，终止运行中的进程，返回新会话 ID"""
        self._kill_process(user_id)
        state = self._get_state(user_id)
        state["session_id"] = str(uuid.uuid4())
        state["session_started"] = False
        state["running"] = False
        default_perm = self._load_plugin_config()["default_perm_mode"]
        # manual_select 模式：暂用 interactive 作为安全默认，后续弹卡片由用户选择
        state["session_perm_mode"] = "interactive" if default_perm == "manual_select" else default_perm
        return state["session_id"]

    def _send_perm_select_card_if_manual(self, chat_id: str, user_id: str) -> None:
        """若 default_perm_mode 配置为 manual_select，向用户发送权限模式选择卡片"""
        if self._load_plugin_config()["default_perm_mode"] == "manual_select":
            state = self._get_state(user_id)
            card = self._build_permission_mode_card(state["session_perm_mode"])
            self.bot.send_message(chat_id, "interactive", card)

    def _format_status(self, user_id: str) -> str:
        """格式化当前会话状态文本（复用于激活、/status、/new、/cd）"""
        state = self._get_state(user_id)
        working_dir = state["working_dir"]  # 始终为有效绝对路径（由 _resolve_working_dir 保证）
        cfg = self._load_plugin_config()
        default_dir = _resolve_working_dir(cfg.get("default_working_dir", ""))
        if working_dir == default_dir:
            working_dir_display = f"{_display_path(working_dir)} (默认)"
        else:
            working_dir_display = _display_path(working_dir)
        status = "运行中" if state["running"] else "空闲"
        perm_mode = state.get("session_perm_mode", "interactive")
        return (
            f"会话: {state['session_id'][:8]}...\n"
            f"工作目录: {working_dir_display}\n"
            f"状态: {status}\n"
            f"权限模式: {perm_mode}"
        )

    # ---- 历史会话持久化 ----

    def _sessions_file_path(self) -> pathlib.Path:
        """返回历史会话存储文件路径"""
        return _CC_DATA_DIR / "feishu_sessions.json"

    def _load_user_sessions(self, user_id: str) -> list[dict]:
        """读取指定用户的历史会话列表（文件不存在则返回空列表）"""
        path = self._sessions_file_path()
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                return data.get(user_id, [])
        except Exception as e:
            logger.warning("[CC] 读取历史会话文件失败: %s", e)
        return []

    def _upsert_session(
        self, user_id: str, session_id: str, working_dir: str, title: str
    ) -> None:
        """新增或更新一条历史会话记录，按最近活跃时间倒序保留最多 _MAX_SESSIONS_PER_USER 条"""
        now = datetime.datetime.now().isoformat(timespec="seconds")
        path = self._sessions_file_path()

        with self._sessions_lock:
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
            else:
                sessions.insert(0, {
                    "session_id": session_id,
                    "working_dir": working_dir,
                    "title": title,
                    "created_at": now,
                    "last_activity": now,
                })

            # 按最近活跃时间倒序，截断
            sessions.sort(key=lambda s: s["last_activity"], reverse=True)
            data[user_id] = sessions[:_MAX_SESSIONS_PER_USER]

            # 写回文件（原子操作：先写临时文件，再 rename 替换）
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = path.with_suffix(".json.tmp")
                tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(str(tmp_path), str(path))
            except Exception as e:
                logger.error("[CC] 写入历史会话文件失败: %s", e)

    @staticmethod
    def _build_sessions_card(sessions: list[dict], current_session_id: str) -> str:
        """构造历史会话选择卡片"""
        elements: list[dict] = [
            {
                "tag": "markdown",
                "content": "点击选择要恢复的会话（将替换当前会话上下文）",
            },
            {"tag": "hr"},
        ]
        for session in sessions:
            sid = session["session_id"]
            short_id = sid[:8]
            working_dir = _display_path(session.get("working_dir") or "") or "默认目录"
            title = session.get("title", "（无标题）")
            last_activity = session.get("last_activity", "")
            # 格式化时间（去掉秒）
            try:
                dt = datetime.datetime.fromisoformat(last_activity)
                time_str = dt.strftime("%m-%d %H:%M")
            except Exception:
                time_str = last_activity[:16]

            is_current = sid == current_session_id
            label = f"{'✓ 当前  ' if is_current else ''}{short_id}…"
            elements.append({
                "tag": "markdown",
                "content": f"**{short_id}…** | `{working_dir}` | {time_str}\n{title}",
            })
            elements.append({
                "tag": "action",
                "actions": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": label},
                    "type": "primary" if is_current else "default",
                    "value": {
                        "action": "resume_session",
                        "plugin": PLUGIN_KEYWORD,
                        "session_id": sid,
                        "working_dir": session.get("working_dir", ""),
                    },
                }],
            })
            elements.append({"tag": "hr"})

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "Claude Code 历史会话"},
                "template": "blue",
            },
            "elements": elements,
        }
        return json.dumps(card)

    # ---- 权限确认服务器 ----

    def _needs_permission_server(self) -> bool:
        """判断是否需要启动权限确认服务器

        默认始终启用交互式权限确认（用户可在会话内切换免确认模式），
        因此权限服务器始终需要启动。
        """
        return True

    def _ensure_permission_server(self) -> None:
        """确保权限确认服务器已启动（懒初始化，线程安全）"""
        if self._perm_server_started or not self._needs_permission_server():
            return

        with self._perm_server_lock:
            # 双重检查：加锁后再次确认，防止并发重复初始化
            if self._perm_server_started:
                return

            cfg = self._load_plugin_config()
            port = cfg["permission_server_port"]
            timeout = cfg["permission_timeout"]

            perm_server = PermissionServer(
                port=port,
                timeout=timeout,
                on_permission_request=self._on_permission_request,
                on_permission_timeout=self._on_permission_timeout,
            )
            try:
                perm_server.start()
            except Exception as e:
                # 仅 start()（端口绑定）失败时删除端口文件，让 hook 降级为自动放行
                # 避免 hook 脚本向无效端口发请求后等待 curl 超时（每次工具调用卡 180s）
                logger.error("[CC] 权限确认服务器启动失败: %s", e, exc_info=True)
                self._delete_port_file()
                return

            # start() 成功后才更新状态——_setup_hook 等后续步骤的失败不应回滚端口文件
            self._perm_server = perm_server
            self._perm_server_started = True
            self._write_port_file(port)
            self._setup_hook()

    def _get_target_home_dir(self, run_as_user: str) -> str | None:
        """获取目标用户的 HOME 目录。

        若 run_as_user 已设置且当前为 root，返回该用户主目录；
        用户不存在时返回 None；其余情况返回当前用户主目录。
        """
        if run_as_user and os.getuid() == 0:
            try:
                return pwd.getpwnam(run_as_user).pw_dir
            except KeyError:
                return None
        return os.path.expanduser("~")

    def _write_port_file(self, port: int) -> None:
        """将端口号和超时值写入项目数据目录，供 Hook 脚本读取

        写入两个文件：
        - .feishu_perm_port: 权限服务器端口号
        - .feishu_perm_timeout: 权限确认超时秒数（hook 用于设置 curl --max-time）
        """
        port_file = _CC_DATA_DIR / ".feishu_perm_port"
        timeout_file = _CC_DATA_DIR / ".feishu_perm_timeout"
        try:
            port_file.parent.mkdir(parents=True, exist_ok=True)
            port_file.write_text(str(port))
            # 写入超时值，hook 脚本据此设置 curl --max-time
            cfg = self._load_plugin_config()
            timeout_file.write_text(str(cfg["permission_timeout"]))
            # 如果以 root 运行且有 run_as_user，修正文件归属让 hook 脚本可读
            run_as_user = cfg.get("run_as_user", "")
            if run_as_user and os.getuid() == 0:
                try:
                    pw = pwd.getpwnam(run_as_user)
                    os.chown(port_file, pw.pw_uid, pw.pw_gid)
                    os.chown(timeout_file, pw.pw_uid, pw.pw_gid)
                except (KeyError, OSError) as e:
                    logger.warning("[CC] 修正端口/超时文件归属失败: %s", e)
            logger.info("[CC] 端口文件已写入: %s", port_file)
        except OSError as e:
            logger.error("[CC] 写入端口文件失败: %s", e)

    def _delete_port_file(self) -> None:
        """删除端口文件和超时文件，让 hook 脚本不再尝试连接（降级为自动放行）"""
        port_file = _CC_DATA_DIR / ".feishu_perm_port"
        timeout_file = _CC_DATA_DIR / ".feishu_perm_timeout"
        try:
            port_file.unlink(missing_ok=True)
            timeout_file.unlink(missing_ok=True)
            logger.info("[CC] 端口/超时文件已删除: %s", port_file)
        except OSError as e:
            logger.warning("[CC] 删除端口/超时文件失败: %s", e)

    def _setup_hook(self) -> None:
        """自动注册 PreToolUse Hook 到 Claude 用户设置

        将 Hook 脚本路径写入目标用户的 ~/.claude/settings.json。
        """
        cfg = self._load_plugin_config()
        run_as_user = cfg.get("run_as_user", "")
        home_dir = self._get_target_home_dir(run_as_user)
        if home_dir is None:
            logger.error("[CC] Hook 注册失败: 用户 %s 不存在", run_as_user)
            return

        settings_path = pathlib.Path(home_dir) / ".claude" / "settings.json"

        # 读取现有设置
        settings = {}
        if settings_path.exists():
            try:
                settings = json.loads(settings_path.read_text())
            except (json.JSONDecodeError, OSError):
                logger.warning("[CC] 读取 Claude 设置失败，将创建新文件")

        # Hook 脚本绝对路径
        hook_script = str(
            pathlib.Path(__file__).parent / "permission_hook.sh"
        )

        # 检查是否已配置（PreToolUse）
        hooks = settings.get("hooks", {})
        pre_hooks = hooks.get("PreToolUse", [])

        already_configured = False
        for rule in pre_hooks:
            for h in rule.get("hooks", []):
                if h.get("command") == hook_script:
                    already_configured = True
                    break

        if already_configured:
            logger.info("[CC] PreToolUse Hook 已配置，跳过注册")
            return

        # 移除旧的 PermissionRequest hook（如有）
        if "PermissionRequest" in hooks:
            del hooks["PermissionRequest"]
            logger.info("[CC] 已移除旧的 PermissionRequest Hook")

        # 添加 PreToolUse Hook 配置
        new_hook_rule = {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": hook_script,
                }
            ],
        }
        pre_hooks.append(new_hook_rule)
        hooks["PreToolUse"] = pre_hooks
        settings["hooks"] = hooks

        try:
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False))
            # 修正文件归属
            if run_as_user and os.getuid() == 0:
                try:
                    pw = pwd.getpwnam(run_as_user)
                    os.chown(settings_path, pw.pw_uid, pw.pw_gid)
                except (KeyError, OSError) as e:
                    logger.warning("[CC] 修正设置文件归属失败: %s", e)
            logger.info("[CC] PreToolUse Hook 已注册: %s", hook_script)
        except OSError as e:
            logger.error("[CC] 写入 Claude 设置失败: %s", e)

    def _on_permission_request(
        self,
        user_id: str,
        chat_id: str,
        request_id: str,
        tool_name: str,
        tool_input: dict,
    ) -> None:
        """权限请求回调：根据会话权限模式决定自动放行或发送飞书确认卡片

        三种模式:
        - interactive (默认): 所有请求均通过飞书卡片确认
        - bypass: 所有请求自动放行
        - accept_edits: Write/Edit/NotebookEdit 在工作目录内自动放行，其余仍需确认
        """
        # 格式化工具调用详情用于日志
        if tool_name == "Bash" and "command" in tool_input:
            input_summary = tool_input["command"]
        elif tool_name in ("Edit", "Write"):
            file_path = tool_input.get("file_path", tool_input.get("path", ""))
            input_summary = f"file={file_path}"
        else:
            input_summary = json.dumps(tool_input, ensure_ascii=False)[:500]

        # 读取当前会话权限模式
        state = self._get_state(user_id)
        perm_mode = state.get("session_perm_mode", "interactive")
        effective_working_dir = state["working_dir"]  # 始终为有效绝对路径
        logger.info(
            "[CC] 权限请求: user=%s, tool=%s, perm_mode=%s, request=%s, input=%s",
            user_id, tool_name, perm_mode, request_id[:8], input_summary.replace("\n", "↵"),
        )

        # bypass 模式：直接放行所有请求
        if perm_mode == "bypass":
            if self._perm_server:
                self._perm_server.resolve_request(request_id, "allow")
            return

        # accept_edits 模式：文件修改类工具在工作目录内自动放行
        if perm_mode == "accept_edits":
            if tool_name in ("Write", "Edit", "NotebookEdit"):
                fp = tool_input.get("file_path") or tool_input.get("notebook_path", "")
                if fp and self._is_within_working_dir(fp, effective_working_dir):
                    logger.info(
                        "[CC] accept_edits 模式自动放行: tool=%s, file=%s", tool_name, fp,
                    )
                    if self._perm_server:
                        self._perm_server.resolve_request(request_id, "allow")
                    return
            # 工作目录外的文件修改或其他工具（Bash 等）继续走卡片确认

        # interactive 模式或 accept_edits 模式未匹配自动放行：发送飞书权限确认卡片
        # 仅当请求本身属于 accept_edits 自动放行范围（工作目录内的文件修改）时，
        # 才在卡片上显示「允许本次会话所有修改」按钮，否则该按钮语义上不合适
        fp = tool_input.get("file_path") or tool_input.get("notebook_path", "")
        show_accept_edits_option = (
            perm_mode == "interactive"
            and tool_name in ("Write", "Edit", "NotebookEdit")
            and bool(fp)
            and self._is_within_working_dir(fp, effective_working_dir)
        )
        card = self._build_permission_card(
            request_id, tool_name, tool_input, show_accept_edits_option, effective_working_dir
        )
        try:
            self.bot.send_message(chat_id, "interactive", card)
            logger.info(
                "[CC] 已发送权限确认卡片: user=%s, tool=%s, request=%s",
                user_id, tool_name, request_id[:8],
            )
        except Exception as e:
            logger.error("[CC] 发送权限确认卡片失败: %s", e, exc_info=True)
            raise

    def _on_permission_timeout(
        self,
        user_id: str,
        chat_id: str,
        request_id: str,
        tool_name: str,
        timeout: int,
    ) -> None:
        """权限确认超时回调：记录超时事件到用户状态，供任务结束时在卡片中提示"""
        state = self._get_state(user_id)
        state["perm_timeout_count"] = state.get("perm_timeout_count", 0) + 1
        logger.warning(
            "[CC] 权限确认超时: user=%s, tool=%s, request=%s, timeout=%ds",
            user_id, tool_name, request_id[:8], timeout,
        )

    @staticmethod
    def _is_within_working_dir(file_path: str, working_dir: str) -> bool:
        """检查文件路径是否在工作目录内（含工作目录本身）

        用于 accept_edits 模式判断是否可以自动放行文件修改请求。
        """
        if not working_dir:
            working_dir = os.getcwd()
        try:
            abs_file = os.path.realpath(os.path.abspath(file_path))
            abs_dir = os.path.realpath(os.path.abspath(working_dir))
            return os.path.commonpath([abs_file, abs_dir]) == abs_dir
        except (ValueError, OSError):
            return False

    @staticmethod
    def _build_permission_mode_card(current_mode: str) -> str:
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

    @staticmethod
    def _build_permission_card(
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
            input_display = f"文件: `{_display_path(file_path, working_dir)}`"
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

    # ---- 子进程管理 ----

    def _build_command(
        self, prompt: str, session_id: str, *, resume: bool = False
    ) -> list[str]:
        """构造 Claude Code CLI 命令行参数

        Args:
            prompt: 用户提示词
            session_id: 会话 ID
            resume: 是否为恢复已有会话（第二次及后续调用）
        """
        cfg = self._load_plugin_config()
        cmd = [
            cfg["claude_path"],
            "-p", prompt,
            "--output-format", "stream-json",
        ]
        if resume:
            cmd.extend(["--resume", session_id])
        else:
            cmd.extend(["--session-id", session_id])
        cmd.append("--verbose")
        # 权限服务器（PreToolUse hook）始终作为唯一权限门控：
        # CLI 以 bypassPermissions 运行，hook 负责拦截并通过飞书卡片或会话级免确认设置决策。
        # 在非交互（-p）模式下，default 模式会直接拒绝 Write/Bash，hook 形同虚设。
        cmd.extend(["--permission-mode", "bypassPermissions"])
        max_turns = cfg.get("max_turns", _DEFAULT_MAX_TURNS)
        cmd.extend(["--max-turns", str(max_turns)])
        return cmd

    def _kill_process(self, user_id: str) -> None:
        """安全终止用户的运行中进程"""
        proc = self._running_processes.pop(user_id, None)
        if proc and proc.poll() is None:
            logger.info("[CC] 终止进程: user=%s, pid=%d", user_id, proc.pid)
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            except Exception as e:
                logger.warning("终止进程失败: %s", e)

    def _prepare_subprocess_env(self) -> tuple[dict, Optional[callable]]:
        """准备子进程的环境变量和 preexec_fn

        当配置了 run_as_user 时，子进程切换到该用户的 uid/gid，
        并将 HOME 设置为该用户的主目录，从而：
        1. 绕过 root 用户不能使用 bypassPermissions 的限制
        2. Claude 配置文件（.claude/）放在用户主目录下，用户自然有读写权限
        3. 工作目录也应设在用户主目录下，避免文件权限问题

        Returns:
            (env_dict, preexec_fn) 元组
        """
        cfg = self._load_plugin_config()
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)

        run_as_user = cfg.get("run_as_user", "")
        preexec_fn = None

        if run_as_user and os.getuid() == 0:
            try:
                pw = pwd.getpwnam(run_as_user)
                uid, gid = pw.pw_uid, pw.pw_gid
                home_dir = pw.pw_dir

                # 切换 uid/gid，同时设置 HOME 为用户主目录
                # claude 读取 $HOME/.claude/ 下的鉴权和配置文件
                env["HOME"] = home_dir

                def _switch_user():
                    os.setgid(gid)
                    os.setuid(uid)

                preexec_fn = _switch_user
                logger.info(
                    "子进程将以用户 %s (uid=%d) 身份运行, HOME=%s",
                    run_as_user, uid, home_dir,
                )
            except KeyError:
                logger.error("配置的 run_as_user '%s' 不存在", run_as_user)

        return env, preexec_fn

    def _run_claude_code(
        self, user_id: str, chat_id: str, prompt: str, message_id: Optional[str]
    ) -> None:
        """在后台线程执行 Claude Code 子进程，流式更新飞书卡片"""
        state = self._get_state(user_id)
        cfg = self._load_plugin_config()
        timer: Optional[threading.Timer] = None
        start_time = time.time()
        session_id = state["session_id"]

        # 注册会话到权限服务器（如果已启动）
        if self._perm_server and self._perm_server_started:
            self._perm_server.register_session(session_id, user_id, chat_id)

        try:
            cmd = self._build_command(
                prompt, state["session_id"],
                resume=state.get("session_started", False),
            )
            cwd = state["working_dir"]  # 始终为有效绝对路径

            logger.info(
                "[CC] 启动子进程: user=%s, session=%s, resume=%s, cwd=%s",
                user_id, state["session_id"][:8], state.get("session_started", False), cwd,
            )
            logger.debug("[CC] 完整命令: %s", cmd)

            env, preexec_fn = self._prepare_subprocess_env()

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                text=True,
                bufsize=1,
                env=env,
                preexec_fn=preexec_fn,
            )
            self._running_processes[user_id] = proc
            logger.info("[CC] 子进程已启动: pid=%d, user=%s", proc.pid, user_id)

            # 启动超时定时器
            timer = self._start_timeout_timer(user_id, cfg["timeout"])

            # 流式读取并更新卡片
            full_text = ""
            last_patch_len = 0
            last_patch_time = time.time()
            cost_info = ""
            has_assistant_text = False

            # 统一内容段列表：工具调用段与文字段按实际执行顺序交错存储
            segments: list[dict] = []            # {"type":"tools","entries":[...]} 或 {"type":"text","content":"..."}
            current_streak: list[dict] = []      # 当前连续工具调用
            active_tool_ids: dict[str, int] = {} # tool_use_id → current_streak 索引
            log_dirty = False                    # 是否有变化（触发节流更新）
            model_thinking = True                # 初始即为思考阶段；收到 assistant 事件后置 False
            phase_start_time = start_time        # 当前阶段（思考/处理）开始时间；阶段切换时重置

            line_count = 0
            while True:
                # 使用 select 等待新数据（最多 _IDLE_PATCH_INTERVAL 秒）；
                # select 不可用时（如测试 mock 对象）直接降级为阻塞读
                timed_out = False
                try:
                    ready, _, _ = select.select([proc.stdout], [], [], _IDLE_PATCH_INTERVAL)
                    timed_out = not ready
                except (TypeError, ValueError, OSError):
                    pass  # select 不可用，timed_out 保持 False，直接 readline

                if timed_out:
                    # 超时：无新数据，检查进程是否已结束
                    if proc.poll() is not None:
                        break
                    # 无新数据时刷新进度计时
                    if message_id:
                        elapsed = int(time.time() - phase_start_time)
                        card_text = self._render_log(
                            segments, current_streak, running=True,
                            elapsed=elapsed, thinking=model_thinking,
                        )
                        self._patch_card(message_id, card_text, running=True,
                                         elapsed=int(time.time() - start_time))
                        last_patch_time = time.time()
                    continue

                line = proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                line_count += 1

                text_chunk, log_actions, meta = self._parse_stream_line(
                    line, has_assistant_text, cwd
                )

                # 更新模型思考阶段状态，并在阶段切换时重置计时器：
                # - user 事件（工具结果返回）→ 模型将开始推理，置 True，重置计时
                # - assistant 事件产出任意内容（thinking/tool_use/text）→ 模型已响应，置 False，重置计时
                # thinking block 在切换前计算本次思考用时，并更新对应日志行
                has_tool_results = any(a["action"] == "result" for a in log_actions)
                has_assistant_output = (
                    any(a["action"] == "add" for a in log_actions) or bool(text_chunk)
                )
                prev_thinking = model_thinking
                if has_tool_results:
                    model_thinking = True
                if has_assistant_output:
                    if model_thinking:
                        # 从思考阶段切换到处理阶段：计算本次思考用时，更新 thinking block 日志行
                        thinking_duration = int(time.time() - phase_start_time)
                        for action in log_actions:
                            if action.get("is_thinking"):
                                action["line"] = f"💭 思考完成 (用时 {thinking_duration}s)"
                    model_thinking = False
                # 阶段发生切换时重置计时器
                if model_thinking != prev_thinking:
                    phase_start_time = time.time()

                # 处理日志动作
                for action in log_actions:
                    if action["action"] == "add":
                        idx = len(current_streak)
                        current_streak.append({
                            "line": action["line"],
                            "tool_use_id": action["tool_use_id"],
                        })
                        if action["tool_use_id"]:
                            active_tool_ids[action["tool_use_id"]] = idx
                        log_dirty = True
                    elif action["action"] == "result":
                        tid = action["tool_use_id"]
                        if tid in active_tool_ids:
                            idx = active_tool_ids[tid]
                            suffix = (
                                f" → ❌ {action['summary']}" if action["is_error"]
                                else " → ✅"
                            )
                            current_streak[idx]["line"] += suffix
                            log_dirty = True

                if text_chunk:
                    if current_streak:
                        # 将工具调用段刷入统一列表，工具段本身已提供视觉分隔
                        segments.append({
                            "type": "tools",
                            "entries": [e["line"] for e in current_streak],
                        })
                        current_streak = []
                        active_tool_ids = {}
                    elif has_assistant_text and segments and segments[-1]["type"] == "text":
                        # 连续文字段（无工具调用间隔），补分隔线
                        text_chunk = "\n\n---\n\n" + text_chunk

                    if not has_assistant_text:
                        logger.info("[CC] 收到首条输出: user=%s, pid=%d", user_id, proc.pid)
                    has_assistant_text = True
                    # 将文字段加入统一列表，保持与工具调用的交错顺序
                    segments.append({"type": "text", "content": text_chunk})
                    full_text += text_chunk
                    log_dirty = True

                    # 截断保护
                    if len(full_text) > cfg["max_output_chars"]:
                        full_text = full_text[:cfg["max_output_chars"]]
                        trunc_msg = "\n\n**[输出已截断，超出飞书卡片字符限制]**"
                        segments[-1]["content"] += trunc_msg
                        full_text += trunc_msg
                        logger.warning(
                            "[CC] 输出截断: user=%s, 字符数已达 %d 上限",
                            user_id, cfg["max_output_chars"],
                        )
                        self._kill_process(user_id)
                        break

                if meta:
                    cost_info = meta
                    logger.info("[CC] 收到结果统计: user=%s, %s", user_id, meta)

                # 节流更新：有变化时按时间间隔刷新卡片
                if message_id and log_dirty:
                    now = time.time()
                    chars_since = len(full_text) - last_patch_len
                    time_since = now - last_patch_time
                    if chars_since >= _PATCH_MIN_CHARS or time_since >= _PATCH_INTERVAL:
                        elapsed = int(now - phase_start_time)
                        card_text = self._render_log(
                            segments, current_streak, running=True,
                            elapsed=elapsed, thinking=model_thinking,
                        )
                        self._patch_card(message_id, card_text, running=True,
                                         elapsed=int(now - start_time))
                        last_patch_len = len(full_text)
                        last_patch_time = now
                        log_dirty = False

            # 等待进程结束
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                logger.warning("[CC] 进程等待超时，强制终止: user=%s, pid=%d", user_id, proc.pid)
                self._kill_process(user_id)

            stderr_output = proc.stderr.read() if proc.stderr else ""
            elapsed = time.time() - start_time

            logger.info(
                "[CC] 子进程结束: user=%s, pid=%d, returncode=%s, "
                "耗时=%.1fs, 输出行数=%d, 输出字符数=%d",
                user_id, proc.pid, proc.returncode,
                elapsed, line_count, len(full_text),
            )
            if stderr_output:
                logger.debug("[CC] stderr 输出: %s", stderr_output[:500])

            # 进程正常结束后标记会话已启动，后续调用使用 --resume
            if proc.returncode == 0:
                state["session_started"] = True
                # 保存/更新历史会话记录
                title = (prompt[:50] + "…") if len(prompt) > 50 else prompt
                self._upsert_session(
                    user_id, session_id,
                    state["working_dir"],
                    title,
                )

            # 最终内容组装
            if not full_text:
                fallback = "Claude Code 未产生输出。"
                if stderr_output:
                    fallback += f"\n\nstderr:\n```\n{stderr_output[:2000]}\n```"
                segments.append({"type": "text", "content": fallback})

            # 将剩余未提交的 streak 也入段（任务结束时可能还有工具调用未出文字）
            if current_streak:
                segments.append({
                    "type": "tools",
                    "entries": [e["line"] for e in current_streak],
                })

            card_text = self._render_log(segments, [], running=False)
            if cost_info:
                card_text += f"\n\n---\n{cost_info}"

            # 如果本次任务中发生过权限确认超时，追加警告
            timeout_count = state.get("perm_timeout_count", 0)
            if timeout_count > 0:
                perm_timeout = self._load_plugin_config()["permission_timeout"]
                card_text += (
                    f"\n\n---\n⚠️ 本次任务中有 {timeout_count} 次权限确认超时"
                    f"（{perm_timeout}s），操作已自动拒绝"
                )

            if message_id:
                self._patch_card(message_id, card_text, running=False)

        except FileNotFoundError:
            error_msg = "Claude Code CLI 未安装或路径错误，请检查配置。"
            logger.error("[CC] CLI 未找到: %s", self._load_plugin_config()["claude_path"])
            if message_id:
                self._patch_card(message_id, error_msg, running=False)
            else:
                self.bot.reply(chat_id, error_msg)

        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(
                "[CC] 执行异常: user=%s, 耗时=%.1fs, error=%s",
                user_id, elapsed, e, exc_info=True,
            )
            error_msg = f"执行失败: {e}"
            if message_id:
                self._patch_card(message_id, error_msg, running=False)
            else:
                self.bot.reply(chat_id, error_msg)

        finally:
            if timer:
                timer.cancel()
            state["running"] = False
            self._running_processes.pop(user_id, None)
            self._running_threads.pop(user_id, None)
            # 移除权限服务器中的会话映射
            if self._perm_server and self._perm_server_started:
                self._perm_server.unregister_session(session_id)
            # 任务结束后发送加急通知，让用户收到提醒
            if message_id:
                try:
                    self.bot.urgent_message(message_id, [user_id])
                except Exception as ue:
                    logger.debug("[CC] 加急通知发送失败: %s", ue)
            logger.info("[CC] 任务清理完成: user=%s", user_id)

    def _start_timeout_timer(self, user_id: str, timeout: int) -> threading.Timer:
        """启动超时定时器，超时后终止进程"""
        def _on_timeout():
            logger.warning("Claude Code 执行超时 (%ds), user=%s", timeout, user_id)
            self._kill_process(user_id)

        timer = threading.Timer(timeout, _on_timeout)
        timer.daemon = True
        timer.start()
        return timer

    # ---- stream-json 解析 ----

    @staticmethod
    def _parse_stream_line(line: str, has_previous_text: bool, working_dir: str = "") -> tuple[str, list[dict], str]:
        """解析 stream-json 单行

        Args:
            line: 一行 JSON 字符串
            has_previous_text: 之前是否已提取到 assistant 文本

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
                    log_line = ClaudeCodePlugin._format_tool_call(tool_name, tool_input, working_dir)
                    log_actions.append({"action": "add", "line": log_line, "tool_use_id": tool_use_id})
            if text_parts:
                text_chunk = "\n\n".join(text_parts)

        elif event_type == "user":
            # 工具执行结果：追加 ✅/❌ 到对应日志行
            message = data.get("message", {})
            content_blocks = message.get("content", [])
            for block in content_blocks:
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

    # ---- 工具调用日志渲染 ----

    # 工具名 → 展示图标
    _TOOL_ICONS: dict[str, str] = {
        "Read":         "📖",
        "Write":        "✍️",
        "Edit":         "📝",
        "NotebookEdit": "📓",
        "Bash":         "💻",
        "Glob":         "🔍",
        "Grep":         "🔍",
        "Task":         "🤖",
        "WebFetch":     "🌐",
    }

    @staticmethod
    def _format_tool_call(tool_name: str, tool_input: dict, working_dir: str = "") -> str:
        """将工具调用格式化为单行摘要，用于过程日志"""
        icon = ClaudeCodePlugin._TOOL_ICONS.get(tool_name, "🔧")
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
        param = _display_path(param, working_dir)
        if len(param) > _TOOL_PARAM_MAX:
            param = param[:_TOOL_PARAM_MAX - 3] + "..."
        return f"{icon} {tool_name} `{param}`" if param else f"{icon} {tool_name}"

    @staticmethod
    def _render_log(
        log_segments: list[dict],
        current_streak: list[dict],
        running: bool,
        elapsed: int = 0,
        thinking: bool = False,
    ) -> str:
        """将统一内容段列表和当前连续区间渲染为 markdown 文本

        工具调用段与文字段按实际执行顺序交错渲染，保持与原始 CC 输出一致的顺序。

        Args:
            log_segments: 已完成的段列表，每段为以下之一：
                {"type": "tools", "entries": list[str]}  工具调用段
                {"type": "text",  "content": str}        文字段
            current_streak: 当前正在进行的连续工具调用，每项 {"line": str, "tool_use_id": str|None}
            running: 是否仍在执行中（控制末尾提示）
            elapsed: 任务已运行秒数（>0 时在提示后附加计时）
            thinking: 是否处于模型思考阶段（工具结果已返回、等待模型下一步响应）
        """
        lines: list[str] = []

        for segment in log_segments:
            if segment["type"] == "tools":
                entries: list[str] = segment["entries"]
                n = len(entries)
                if n > _MAX_STREAK_DISPLAY:
                    lines.append(f"*... 已省略 {n - _MAX_STREAK_DISPLAY} 次工具调用*")
                    entries = entries[-_MAX_STREAK_DISPLAY:]
                lines.extend(entries)
                lines.append("")  # 段间空行
            elif segment["type"] == "text":
                lines.append(segment["content"])
                lines.append("")  # 段间空行

        if current_streak:
            entries = [e["line"] for e in current_streak]
            n = len(entries)
            if n > _MAX_STREAK_DISPLAY:
                lines.append(f"*... 已省略 {n - _MAX_STREAK_DISPLAY} 次工具调用*")
                entries = entries[-_MAX_STREAK_DISPLAY:]
            lines.extend(entries)

        if running:
            elapsed_text = f" (已等待 {elapsed}s)" if elapsed > 0 else ""
            if thinking:
                lines.append(f"💭 思考中...{elapsed_text}")
            else:
                lines.append(f"⏳ 正在处理...{elapsed_text}")

        return "\n".join(lines)

    @staticmethod
    def _assemble_card_text(log_text: str, reply_text: str) -> str:
        """组装两段式卡片内容：过程日志（上）+ 文字回复（下）"""
        if log_text and reply_text:
            return log_text + "\n\n---\n\n" + reply_text
        return log_text or reply_text

    # ---- 飞书卡片 ----

    @staticmethod
    def _build_card(text: str, running: bool = False, elapsed: int = 0) -> str:
        """构造飞书卡片 JSON"""
        if running:
            template = "turquoise"
            if elapsed > 0:
                header_content = f"Claude Code (执行中...已用时 {elapsed}s)"
            else:
                header_content = "Claude Code (执行中...)"
        else:
            template = "blue"
            header_content = "Claude Code"

        card: dict = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": header_content},
                "template": template,
            },
            "elements": [
                {"tag": "markdown", "content": text},
            ],
        }

        # 运行中时添加取消按钮
        if running:
            card["elements"].append({"tag": "hr"})
            card["elements"].append({
                "tag": "action",
                "actions": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "取消执行"},
                    "type": "danger",
                    "value": {"action": "cancel", "plugin": PLUGIN_KEYWORD},
                }],
            })

        return json.dumps(card)

    def _patch_card(self, message_id: str, text: str, running: bool = True, elapsed: int = 0) -> None:
        """更新飞书卡片消息"""
        content = self._build_card(text, running=running, elapsed=elapsed)
        # 调试日志：将卡片 markdown 文本追加写入文件，用于排查路径缩短问题
        try:
            debug_log = _CC_DATA_DIR / "cc_card_debug.log"
            debug_log.parent.mkdir(parents=True, exist_ok=True)
            with debug_log.open("a", encoding="utf-8") as _f:
                _f.write(f"\n{'='*60}\n[{datetime.datetime.now().isoformat()}] message_id={message_id} running={running}\n")
                _f.write(text)
                _f.write("\n")
        except Exception:
            pass
        try:
            self.bot.patch_message(message_id, content)
        except Exception as e:
            logger.warning("卡片更新失败: %s", e)

    # ---- Plugin 接口实现 ----

    def handle_message(self, user_id: str, chat_id: str, text: str) -> None:
        """处理用户消息"""
        state = self._get_state(user_id)

        # 1. 关键词激活
        if text == self.keyword:
            logger.info("[CC] 用户激活插件: user=%s", user_id)
            state["active"] = True
            state["last_chat_id"] = chat_id
            self.bot.reply(
                chat_id,
                f"Claude Code 已激活。\n"
                f"{self._format_status(user_id)}\n\n"
                f"直接发送消息作为 prompt 执行。\n"
                f"特殊指令:\n"
                f"{self._commands_brief()}",
            )
            self._send_perm_select_card_if_manual(chat_id, user_id)
            return

        # 2. 特殊指令：新会话
        if text == "/new":
            logger.info("[CC] 用户重置会话: user=%s, 旧session=%s", user_id, state["session_id"][:8])
            self._reset_session(user_id)
            self.bot.reply(chat_id, f"会话已重置。\n{self._format_status(user_id)}")
            self._send_perm_select_card_if_manual(chat_id, user_id)
            return

        # 3. 特殊指令：取消
        if text == "/cancel":
            if state["running"]:
                logger.info("[CC] 用户取消任务: user=%s", user_id)
                self._kill_process(user_id)
                state["running"] = False
                self.bot.reply(chat_id, "已取消当前任务。")
            else:
                self.bot.reply(chat_id, "当前没有运行中的任务。")
            return

        # 4. 特殊指令：状态
        if text == "/status":
            self.bot.reply(chat_id, self._format_status(user_id))
            return

        # 5. 特殊指令：历史会话
        if text == "/session":
            sessions = self._load_user_sessions(user_id)
            if not sessions:
                self.bot.reply(
                    chat_id,
                    "暂无历史会话记录。完成第一次任务后将自动记录，可在此查看并恢复。",
                )
                return
            card = self._build_sessions_card(sessions[:10], state["session_id"])
            self.bot.send_message(chat_id, "interactive", card)
            return

        # 6. 特殊指令：切换目录（不带路径则重置为默认）
        if text == "/cd":
            old_session = state["session_id"]
            self._reset_session(user_id)
            state["working_dir"] = _resolve_working_dir(self._load_plugin_config().get("default_working_dir", ""))
            logger.info(
                "[CC] 用户重置工作目录为默认: user=%s, 旧session=%s, 新session=%s",
                user_id, old_session[:8], state["session_id"][:8],
            )
            self.bot.reply(
                chat_id,
                f"工作目录已重置为默认。\n{self._format_status(user_id)}",
            )
            self._send_perm_select_card_if_manual(chat_id, user_id)
            return

        if text.startswith("/cd "):
            new_dir = os.path.realpath(text[len("/cd "):].strip())
            if os.path.isdir(new_dir):
                old_session = state["session_id"]
                self._reset_session(user_id)
                state["working_dir"] = new_dir
                logger.info(
                    "[CC] 用户切换目录: user=%s, dir=%s, 旧session=%s, 新session=%s",
                    user_id, new_dir, old_session[:8], state["session_id"][:8],
                )
                self.bot.reply(
                    chat_id,
                    f"工作目录已切换: {new_dir}\n"
                    f"{self._format_status(user_id)}",
                )
                self._send_perm_select_card_if_manual(chat_id, user_id)
            else:
                self.bot.reply(chat_id, f"目录不存在: {new_dir}")
            return

        # 7. 特殊指令：切换权限确认模式（弹出选择卡片）
        if text == "/permission":
            if state["running"]:
                self.bot.reply(chat_id, "任务运行中，请等待完成后再切换权限模式。")
                return
            card = self._build_permission_mode_card(state["session_perm_mode"])
            self.bot.send_message(chat_id, "interactive", card)
            return

        # 8. 特殊指令：帮助
        if text == "/help":
            help_text = (
                "**CC 插件使用帮助**\n\n"
                "**基本用法**\n"
                "直接发送任意文本，即可将其作为 prompt 提交给 Claude Code 执行。\n\n"
                "**特殊指令**\n"
                f"{self._commands_detail()}\n\n"
                "**权限确认**\n"
                "Claude Code 执行敏感操作时，会通过飞书卡片请求确认：\n"
                "• 「允许」— 放行本次请求\n"
                "• 「拒绝」— 拒绝本次请求\n"
                "• 「允许本次会话所有请求」— 切换为 bypass 模式，后续请求自动放行\n\n"
                "**权限模式**（发送 `/permission` 可切换）\n"
                "• `interactive` — 所有操作均需确认（默认）\n"
                "• `accept_edits` — 工作目录内文件修改自动放行，其余仍需确认\n"
                "• `bypass` — 所有操作自动放行（危险，慎用）\n\n"
                "**退出插件**\n"
                "发送「退出」或「返回」可退出 CC 插件，回到主菜单。"
            )
            self.bot.reply(chat_id, help_text)
            return

        # 9. 未知特殊指令拦截（以 / 开头但不匹配任何已知指令）
        if text.startswith("/"):
            input_cmd = text.split()[0]
            # 从指令定义表中提取纯指令名（去掉参数部分，如 "/cd <路径>" → "/cd"）
            known_cmds = list({cmd["usage"].split()[0] for cmd in self._SPECIAL_COMMANDS})
            matches = difflib.get_close_matches(input_cmd, known_cmds, n=1, cutoff=0.6)
            hint = f"\n您是不是想输入 `{matches[0]}`？" if matches else ""
            self.bot.reply(chat_id, f"未知指令 `{input_cmd}`，发送 `/help` 查看所有可用指令。{hint}")
            return

        # 10. 并发控制：运行中拒绝新任务
        if state["running"]:
            logger.info("[CC] 拒绝新任务（上一个仍在运行）: user=%s", user_id)
            self.bot.reply(
                chat_id,
                "上一个任务仍在运行中，请等待完成或发送 `/cancel` 终止。",
            )
            return

        # 11. 正常执行：发送 prompt 到 Claude Code
        state["running"] = True
        state["perm_timeout_count"] = 0
        state["last_chat_id"] = chat_id
        logger.info(
            "[CC] 开始执行 prompt: user=%s, session=%s, prompt长度=%d",
            user_id, state["session_id"][:8], len(text),
        )

        # 确保权限确认服务器已启动（首次调用时初始化）
        self._ensure_permission_server()

        # 发送占位卡片
        placeholder = self._build_card("正在启动 Claude Code...", running=True)
        message_id = self.bot.send_message_get_id(chat_id, "interactive", placeholder)

        # 启动后台线程
        t = threading.Thread(
            target=self._run_claude_code,
            args=(user_id, chat_id, text, message_id),
            daemon=True,
        )
        t.start()
        self._running_threads[user_id] = t

    def handle_card_action(
        self, user_id: str, chat_id: str, message_id: str, action_value: dict
    ) -> "P2CardActionTriggerResponse":
        """处理卡片按钮点击（取消按钮、权限确认按钮）"""
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

        action = action_value.get("action", "")

        if action == "cancel":
            state = self._get_state(user_id)
            if state.get("running"):
                logger.info("[CC] 卡片取消按钮点击: user=%s", user_id)
                self._kill_process(user_id)
                state["running"] = False
                return self.bot.make_card_response(toast="已取消执行")
            return self.bot.make_card_response(toast="当前没有运行中的任务")

        if action in ("perm_allow", "perm_deny"):
            request_id = action_value.get("request_id", "")
            behavior = "allow" if action == "perm_allow" else "deny"
            behavior_cn = "允许" if behavior == "allow" else "拒绝"

            if not self._perm_server:
                return self.bot.make_card_response(toast="权限服务器未启动")

            ok = self._perm_server.resolve_request(request_id, behavior)
            if ok:
                logger.info(
                    "[CC] 用户权限响应: user=%s, request=%s, decision=%s",
                    user_id, request_id[:8], behavior,
                )
                # 更新卡片为已处理状态
                handled_card = self._build_permission_handled_card(behavior_cn)
                return self.bot.make_card_response(
                    card=json.loads(handled_card),
                    toast=f"已{behavior_cn}",
                )
            return self.bot.make_card_response(toast="该请求已过期或已处理")

        if action == "set_perm_mode":
            new_mode = action_value.get("mode", "interactive")
            if new_mode not in ("interactive", "accept_edits", "bypass"):
                return self.bot.make_card_response(toast="无效的权限模式")
            state = self._get_state(user_id)
            if state["running"]:
                return self.bot.make_card_response(toast="任务运行中，无法切换权限模式")
            state["session_perm_mode"] = new_mode
            mode_cn = {"interactive": "交互确认", "accept_edits": "自动接受编辑", "bypass": "全部放行"}[new_mode]
            logger.info("[CC] 用户切换权限模式: user=%s, mode=%s", user_id, new_mode)
            updated_card = self._build_permission_mode_card(new_mode)
            return self.bot.make_card_response(
                card=json.loads(updated_card),
                toast=f"已切换为{mode_cn}模式",
            )

        if action == "perm_accept_edits":
            request_id = action_value.get("request_id", "")

            if not self._perm_server:
                return self.bot.make_card_response(toast="权限服务器未启动")

            # 切换为 accept_edits 模式，同时放行当前挂起的请求
            state = self._get_state(user_id)
            state["session_perm_mode"] = "accept_edits"
            logger.info(
                "[CC] 用户通过权限卡片开启 accept_edits 模式: user=%s, request=%s",
                user_id, request_id[:8] if request_id else "?",
            )
            ok = self._perm_server.resolve_request(request_id, "allow")
            if ok:
                handled_card = self._build_permission_handled_card("允许（已开启 accept_edits 模式）")
                return self.bot.make_card_response(
                    card=json.loads(handled_card),
                    toast="已开启 accept_edits 模式，工作目录内文件修改自动放行",
                )
            return self.bot.make_card_response(toast="该请求已过期或已处理")

        if action == "perm_bypass":
            request_id = action_value.get("request_id", "")

            if not self._perm_server:
                return self.bot.make_card_response(toast="权限服务器未启动")

            # 开启会话级 bypass 模式
            state = self._get_state(user_id)
            state["session_perm_mode"] = "bypass"
            logger.info(
                "[CC] 用户通过权限卡片开启 bypass 模式: user=%s, request=%s",
                user_id, request_id[:8] if request_id else "?",
            )

            # 同时放行当前挂起的请求
            ok = self._perm_server.resolve_request(request_id, "allow")
            if ok:
                handled_card = self._build_permission_handled_card("允许（已开启 bypass 模式）")
                return self.bot.make_card_response(
                    card=json.loads(handled_card),
                    toast="已开启 bypass 模式，本会话后续请求自动放行",
                )
            return self.bot.make_card_response(toast="该请求已过期或已处理")

        if action == "resume_session":
            state = self._get_state(user_id)
            if state.get("running"):
                return self.bot.make_card_response(toast="任务运行中，无法切换会话")
            target_sid = action_value.get("session_id", "")
            target_dir = action_value.get("working_dir", "")
            if not target_sid:
                return self.bot.make_card_response(toast="无效的会话 ID")
            if target_sid == state["session_id"]:
                return self.bot.make_card_response(toast="当前会话无需恢复")
            # 校验工作目录是否存在
            resolved_dir = _resolve_working_dir(target_dir)
            if not os.path.isdir(resolved_dir):
                return self.bot.make_card_response(
                    toast=f"工作目录已不存在: {resolved_dir}，请使用 /cd 切换到有效目录"
                )
            # 终止旧进程并切换到目标会话
            self._kill_process(user_id)
            state["session_id"] = target_sid
            state["session_started"] = True   # 下次调用使用 --resume
            state["working_dir"] = resolved_dir
            state["running"] = False
            default_perm = self._load_plugin_config()["default_perm_mode"]
            state["session_perm_mode"] = "interactive" if default_perm == "manual_select" else default_perm
            logger.info(
                "[CC] 用户恢复历史会话: user=%s, session=%s, dir=%s",
                user_id, target_sid[:8], target_dir,
            )
            self._send_perm_select_card_if_manual(chat_id, user_id)
            return self.bot.make_card_response(
                toast=f"已切换到会话 {target_sid[:8]}…，发送消息继续"
            )

        return P2CardActionTriggerResponse()

    @staticmethod
    def _build_permission_handled_card(decision: str) -> str:
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
