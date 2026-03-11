"""
Claude Code 桥接插件

通过 subprocess 调用本地 Claude Code CLI，
将飞书消息作为 prompt 发送，实时流式回显结果到飞书卡片。
支持会话持续（--session-id）、取消运行、清空会话、切换工作目录等操作。
支持交互式权限确认：通过 PreToolUse Hook + HTTP 服务器，
将 Claude Code 的权限请求转发给飞书用户确认。
"""

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

from config import load_plugin_config
from core.plugin import Plugin

from plugins.claude_code.constants import (
    PLUGIN_KEYWORD,
    CC_DATA_DIR,
    DEFAULT_MODELS,
    HISTORY_PREVIEW_ROUNDS,
    display_path,
    resolve_working_dir,
)
from plugins.claude_code.stream_parser import (
    DEFAULT_MAX_OUTPUT,
    format_tool_call,
    parse_stream_line,
    render_log,
)
from plugins.claude_code import cards
from plugins.claude_code.cards import _SESSION_INITIAL_COUNT, _SESSION_PAGE_SIZE
from plugins.claude_code.session_store import (
    SessionStore,
    find_session_jsonl,
    parse_session_rounds,
)
from plugins.claude_code.permission_manager import (
    PermissionManager,
    _DEFAULT_PERM_PORT,
    _DEFAULT_PERM_TIMEOUT,
    is_within_working_dir,
)

logger = logging.getLogger(__name__)

# 流式更新控制
_PATCH_INTERVAL = 0.5       # 最小更新间隔（秒）
_PATCH_MIN_CHARS = 50       # 最小新增字符数触发更新
_IDLE_PATCH_INTERVAL = 2.0  # 无新数据时进度提示刷新间隔（秒）

# 默认配置
_DEFAULT_TIMEOUT = 600          # 默认超时 10 分钟
_DEFAULT_MAX_OUTPUT = DEFAULT_MAX_OUTPUT
_DEFAULT_MAX_TURNS = 50         # Claude Code 最大轮次


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
        {"usage": "/star",      "brief": "收藏/取消收藏当前会话",          "detail": "收藏或取消收藏当前会话（收藏后不会被自动清理）"},
        {"usage": "/rename <标题>", "brief": "重命名当前会话",            "detail": "修改当前会话的标题，方便后续查找"},
        {"usage": "/cancel",    "brief": "终止运行中的任务",             "detail": "终止当前正在运行的任务"},
        {"usage": "/status",    "brief": "查看当前状态",                 "detail": "查看当前会话状态（目录、session、权限模式等）"},
        {"usage": "/permission", "brief": "切换权限确认模式",              "detail": "弹出权限模式选择卡片，可选 interactive / accept_edits / bypass"},
        {"usage": "/cd <路径>",    "brief": "切换工作目录（会同时重置会话）", "detail": "切换工作目录并重置会话"},
        {"usage": "/cd",        "brief": None,                          "detail": "重置工作目录为默认并重置会话"},
        {"usage": "/compact",   "brief": "压缩上下文",                   "detail": "压缩当前会话上下文（释放 token 空间）"},
        {"usage": "/model",     "brief": "切换模型",                     "detail": "弹出模型选择卡片，切换当前会话使用的 Claude 模型"},
        {"usage": "/help",      "brief": "查看帮助信息",                 "detail": "显示此帮助信息"},
    ]

    def __init__(
        self,
        data_dir: Optional[pathlib.Path] = None,
        config_dir: Optional[pathlib.Path] = None,
    ):
        super().__init__()
        # 运行时数据目录（子类可注入，实现多实例隔离）
        self._data_dir: pathlib.Path = data_dir if data_dir is not None else CC_DATA_DIR
        # 配置目录（None 表示使用默认的 config/claude_code.yaml）
        self._config_dir: Optional[pathlib.Path] = config_dir
        # "user_id:chat_id" -> 用户状态（按群聊隔离）
        self.user_states: dict[str, dict] = {}
        # "user_id:chat_id" -> 运行中的子进程
        self._running_processes: dict[str, subprocess.Popen] = {}
        # "user_id:chat_id" -> 运行中的线程
        self._running_threads: dict[str, threading.Thread] = {}
        self._config: Optional[dict] = None
        # 会话持久化
        self._session_store = SessionStore(self._data_dir)
        # 权限管理器（懒初始化 — ensure_server 在首次执行时调用）
        self._perm_mgr: Optional[PermissionManager] = None

    def _ensure_perm_manager(self) -> PermissionManager:
        """确保权限管理器已创建（需要 bot 已注册）"""
        if self._perm_mgr is None:
            self._perm_mgr = PermissionManager(
                data_dir=self._data_dir,
                load_config=self._load_plugin_config,
                get_state=self._get_state,
                send_card=lambda chat_id, content: self.bot.send_message(chat_id, "interactive", content),
                send_card_get_id=lambda chat_id, content: self.bot.send_message_get_id(chat_id, "interactive", content),
            )
        return self._perm_mgr

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
        """懒加载插件配置，从 config/claude_code.yaml 读取

        若构造时传入了 config_dir，则从该目录加载；否则使用默认路径。
        """
        if self._config is None:
            if self._config_dir is not None:
                import yaml
                path = self._config_dir / "claude_code.yaml"
                cc = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
            else:
                cc = load_plugin_config("claude_code")
            self._config = {
                "claude_path": cc.get("claude_path", "/usr/bin/claude"),
                "default_working_dir": cc.get("default_working_dir", ""),
                "timeout": cc.get("timeout", _DEFAULT_TIMEOUT),
                "max_output_chars": cc.get("max_output_chars", _DEFAULT_MAX_OUTPUT),
                "default_perm_mode": cc.get("default_perm_mode", "interactive"),
                "max_turns": cc.get("max_turns", _DEFAULT_MAX_TURNS),
                "run_as_user": cc.get("run_as_user", ""),
                "permission_server_port": cc.get("permission_server_port", _DEFAULT_PERM_PORT),
                "permission_timeout": cc.get("permission_timeout", _DEFAULT_PERM_TIMEOUT),
                "models": cc.get("models", DEFAULT_MODELS),
                "default_model": cc.get("default_model", ""),
            }
        return self._config

    # ---- 状态管理 ----

    @staticmethod
    def _state_key(user_id: str, chat_id: str) -> str:
        """生成 per-user-per-chat 的状态 key"""
        return f"{user_id}:{chat_id}"

    def _get_state(self, user_id: str, chat_id: str) -> dict:
        """获取用户在指定群聊中的会话状态，不存在则初始化"""
        key = self._state_key(user_id, chat_id)
        if key not in self.user_states:
            cfg = self._load_plugin_config()
            default_perm = cfg["default_perm_mode"]
            # manual_select 模式：新会话暂用 interactive 作为安全默认，创建后弹卡片由用户选择
            init_perm = "interactive" if default_perm == "manual_select" else default_perm
            self.user_states[key] = {
                "active": False,
                "session_id": str(uuid.uuid4()),
                "session_started": False,
                "running": False,
                "working_dir": resolve_working_dir(cfg["default_working_dir"]),
                "last_chat_id": chat_id,
                "session_perm_mode": init_perm,  # 会话级权限模式: interactive / bypass / accept_edits
                "session_model": cfg["default_model"],  # 会话级模型: "" 表示 CLI 默认
                "perm_timeout_count": 0,  # 当前任务中权限确认超时次数
            }
        return self.user_states[key]

    def is_user_active(self, user_id: str, chat_id: str = "") -> bool:
        """用户是否在指定群聊中处于活跃会话"""
        return self._get_state(user_id, chat_id).get("active", False)

    def deactivate_user(self, user_id: str, chat_id: str = "") -> None:
        """清理用户在指定群聊中的状态，终止运行中的进程"""
        self._kill_process(user_id, chat_id)
        key = self._state_key(user_id, chat_id)
        self.user_states.pop(key, None)

    def _reset_session(self, user_id: str, chat_id: str) -> str:
        """重置会话状态，终止运行中的进程，返回新会话 ID"""
        self._kill_process(user_id, chat_id)
        state = self._get_state(user_id, chat_id)
        state["session_id"] = str(uuid.uuid4())
        state["session_started"] = False
        state["running"] = False
        default_perm = self._load_plugin_config()["default_perm_mode"]
        # manual_select 模式：暂用 interactive 作为安全默认，后续弹卡片由用户选择
        state["session_perm_mode"] = "interactive" if default_perm == "manual_select" else default_perm
        state["session_model"] = self._load_plugin_config()["default_model"]
        return state["session_id"]

    def _send_perm_select_card_if_manual(self, chat_id: str, user_id: str) -> None:
        """若 default_perm_mode 配置为 manual_select，向用户发送权限模式选择卡片"""
        if self._load_plugin_config()["default_perm_mode"] == "manual_select":
            state = self._get_state(user_id, chat_id)
            card = cards.build_permission_mode_card(state["session_perm_mode"])
            self.bot.send_message(chat_id, "interactive", card)

    def _format_status(self, user_id: str, chat_id: str) -> str:
        """格式化当前会话状态文本（复用于激活、/status、/new、/cd）"""
        state = self._get_state(user_id, chat_id)
        working_dir = state["working_dir"]  # 始终为有效绝对路径（由 resolve_working_dir 保证）
        cfg = self._load_plugin_config()
        default_dir = resolve_working_dir(cfg.get("default_working_dir", ""))
        if working_dir == default_dir:
            working_dir_display = f"{display_path(working_dir)} (默认)"
        else:
            working_dir_display = display_path(working_dir)
        status = "运行中" if state["running"] else "空闲"
        perm_mode = state.get("session_perm_mode", "interactive")
        model_alias = state.get("session_model", "")
        model_display = self._get_model_label(model_alias) if model_alias else "CLI 默认"
        return (
            f"会话: {state['session_id'][:8]}...\n"
            f"工作目录: {working_dir_display}\n"
            f"状态: {status}\n"
            f"权限模式: {perm_mode}\n"
            f"模型: {model_display}"
        )

    def _get_model_label(self, alias: str) -> str:
        """根据模型 alias 获取展示用 label，未找到则返回 alias 原值"""
        for m in self._load_plugin_config().get("models", []):
            if m.get("alias") == alias:
                return m.get("label", alias)
        return alias

    # ---- 子进程管理 ----

    def _build_command(
        self, prompt: str, session_id: str, *,
        resume: bool = False, model: str = ""
    ) -> list[str]:
        """构造 Claude Code CLI 命令行参数

        Args:
            prompt: 用户提示词
            session_id: 会话 ID
            resume: 是否为恢复已有会话（第二次及后续调用）
            model: 模型别名或完整名，空字符串表示不指定（使用 CLI 默认）
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
        if model:
            cmd.extend(["--model", model])
        return cmd

    def _kill_process(self, user_id: str, chat_id: str, wait: bool = True) -> None:
        """安全终止用户在指定群聊中运行的进程

        Args:
            user_id: 用户 ID
            chat_id: 群聊 ID
            wait: 是否等待进程退出。在飞书卡片回调中应设为 False，
                  避免阻塞导致回调超时（飞书要求 3s 内响应）。
                  后台线程会自行处理进程退出和资源清理。
        """
        key = self._state_key(user_id, chat_id)
        proc = self._running_processes.pop(key, None)
        if proc and proc.poll() is None:
            logger.info("[CC] 终止进程: user=%s, pid=%d, wait=%s", user_id, proc.pid, wait)
            try:
                proc.terminate()
                if wait:
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
        # 将数据目录路径传递给子进程，hook 脚本据此找到正确的端口文件
        # 支持多 bot 实例（hub_agent / cc_agent）共用同一 hook 脚本但数据目录隔离
        env["FEISHU_CC_DATA_DIR"] = str(self._data_dir)

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
        state = self._get_state(user_id, chat_id)
        cfg = self._load_plugin_config()
        timer: Optional[threading.Timer] = None
        start_time = time.time()
        session_id = state["session_id"]

        # 注册会话到权限服务器（如果已启动）
        perm_mgr = self._ensure_perm_manager()
        if perm_mgr.server and perm_mgr.started:
            perm_mgr.server.register_session(session_id, user_id, chat_id)

        try:
            cmd = self._build_command(
                prompt, state["session_id"],
                resume=state.get("session_started", False),
                model=state.get("session_model", ""),
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
                env=env,
                preexec_fn=preexec_fn,
            )
            self._running_processes[self._state_key(user_id, chat_id)] = proc
            logger.info("[CC] 子进程已启动: pid=%d, user=%s", proc.pid, user_id)

            # 启动超时定时器
            timer = self._start_timeout_timer(user_id, chat_id, cfg["timeout"])

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
                        card_text = render_log(
                            segments, current_streak, running=True,
                            elapsed=elapsed, thinking=model_thinking,
                        )
                        self._patch_card(message_id, card_text, running=True,
                                         elapsed=int(time.time() - start_time))
                        last_patch_time = time.time()
                    continue

                raw = proc.stdout.readline()
                if not raw:
                    break
                # 二进制模式：手动解码，容错处理不完整的 UTF-8 序列
                line = (raw.decode("utf-8", errors="replace")
                        if isinstance(raw, bytes) else raw).strip()
                if not line:
                    continue
                line_count += 1

                text_chunk, log_actions, meta = parse_stream_line(
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
                            "tool_name": action.get("tool_name", ""),
                        })
                        if action["tool_use_id"]:
                            active_tool_ids[action["tool_use_id"]] = idx
                        log_dirty = True
                    elif action["action"] == "result":
                        tid = action["tool_use_id"]
                        if tid in active_tool_ids:
                            idx = active_tool_ids[tid]
                            # AskUserQuestion 通过 hook "deny" 传回用户回答，
                            # is_error=True 但实际上是成功收到回答，显示为 ✅
                            is_ask_user = current_streak[idx].get("tool_name") == "AskUserQuestion"
                            suffix = (
                                f" → ❌ {action['summary']}" if action["is_error"] and not is_ask_user
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
                        self._kill_process(user_id, chat_id)
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
                        card_text = render_log(
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
                self._kill_process(user_id, chat_id)

            stderr_raw = proc.stderr.read() if proc.stderr else b""
            stderr_output = (stderr_raw.decode("utf-8", errors="replace")
                             if isinstance(stderr_raw, bytes) else stderr_raw) if stderr_raw else ""
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
                self._session_store.upsert_session(
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

            card_text = render_log(segments, [], running=False)
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
                cancelled = state.get("cancelled", False)
                self._patch_card(message_id, card_text, running=False, cancelled=cancelled)

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
            key = self._state_key(user_id, chat_id)
            self._running_processes.pop(key, None)
            self._running_threads.pop(key, None)
            # 移除权限服务器中的会话映射
            if perm_mgr.server and perm_mgr.started:
                perm_mgr.server.unregister_session(session_id)
            # 任务结束后发送加急通知，让用户收到提醒
            if message_id:
                try:
                    self.bot.urgent_message(message_id, [user_id])
                except Exception as ue:
                    logger.debug("[CC] 加急通知发送失败: %s", ue)
            logger.info("[CC] 任务清理完成: user=%s", user_id)

    def _start_timeout_timer(self, user_id: str, chat_id: str, timeout: int) -> threading.Timer:
        """启动超时定时器，超时后终止进程"""
        def _on_timeout():
            logger.warning("Claude Code 执行超时 (%ds), user=%s", timeout, user_id)
            self._kill_process(user_id, chat_id)

        timer = threading.Timer(timeout, _on_timeout)
        timer.daemon = True
        timer.start()
        return timer

    # ---- 飞书卡片更新 ----

    def _patch_card(self, message_id: str, text: str, running: bool = True,
                    elapsed: int = 0, cancelled: bool = False) -> None:
        """更新飞书卡片消息"""
        content = cards.build_execution_card(text, running=running, elapsed=elapsed, cancelled=cancelled)
        # 调试日志：将卡片 markdown 文本追加写入文件，用于排查路径缩短问题
        try:
            debug_log = self._data_dir / "cc_card_debug.log"
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

    # ---- AskUserQuestion 响应处理 ----

    def _handle_ask_user_response(
        self, user_id: str, chat_id: str,
        request_id: str, question_index: int, answer: str,
    ):
        """处理用户对 AskUserQuestion 某道题的回答，返回卡片响应

        记录答案后判断：若所有问题已回答则 resolve 请求并返回灰色已回答卡片，
        否则返回更新后的部分回答卡片。
        """
        state = self._get_state(user_id, chat_id)
        pending = state.get("_pending_ask_user")
        if not pending or pending["request_id"] != request_id:
            return self.bot.make_card_response(toast="该请求已过期或已处理")

        perm_mgr = self._ensure_perm_manager()
        if not perm_mgr.server or not perm_mgr.server.has_pending_request(request_id):
            state.pop("_pending_ask_user", None)
            return self.bot.make_card_response(toast="该请求已过期或已处理")

        # 记录本题答案
        answers = pending["answers"]
        answers[question_index] = answer
        questions = pending["questions"]
        total = pending["total"]

        if len(answers) < total:
            # 尚有未回答的问题，刷新卡片
            logger.info(
                "[CC] 用户回答问题 %d/%d: user=%s, request=%s, answer=%s",
                len(answers), total, user_id, request_id[:8], answer,
            )
            updated_card = cards.build_ask_user_card(request_id, questions, answers)
            return self.bot.make_card_response(
                card=json.loads(updated_card),
                toast=f"已回答 {len(answers)}/{total}",
            )

        # 所有问题已回答，resolve 请求
        state.pop("_pending_ask_user", None)

        if total == 1:
            # 单问题：保持原有格式，向后兼容
            reason = (
                f"用户选择了「{answers[0]}」。"
                f"请按照用户的选择继续执行，不要再次调用 AskUserQuestion 询问同一个问题。"
            )
        else:
            lines = []
            for qi in range(total):
                q_text = questions[qi].get("question", "")[:80]
                lines.append(f"{qi + 1}. {q_text} → 「{answers[qi]}」")
            reason = (
                "用户回答了以下问题：\n"
                + "\n".join(lines)
                + "\n请按照用户的回答继续执行，不要再次调用 AskUserQuestion 询问同一个问题。"
            )

        ok = perm_mgr.server.resolve_request(request_id, "deny", reason=reason)
        if ok:
            logger.info(
                "[CC] 用户回答全部完成 (%d题): user=%s, request=%s",
                total, user_id, request_id[:8],
            )
            answered_card = cards.build_ask_user_answered_card(questions, answers)
            return self.bot.make_card_response(
                card=json.loads(answered_card),
                toast="已完成全部回答",
            )
        return self.bot.make_card_response(toast="该请求已过期或已处理")

    # ---- Plugin 接口实现 ----

    def handle_message(self, user_id: str, chat_id: str, text: str) -> None:
        """处理用户消息"""
        state = self._get_state(user_id, chat_id)

        # 1. 关键词激活
        if text == self.keyword:
            logger.info("[CC] 用户激活插件: user=%s, chat=%s", user_id, chat_id)
            state["active"] = True
            state["last_chat_id"] = chat_id
            self.bot.reply(
                chat_id,
                f"Claude Code 已激活。\n"
                f"{self._format_status(user_id, chat_id)}\n\n"
                f"直接发送消息作为 prompt 执行。\n"
                f"特殊指令:\n"
                f"{self._commands_brief()}\n\n"
                f"💡 群聊中默认需要 @机器人 才能触发，发送「唤醒模式」可切换为免@使用。",
            )
            self._send_perm_select_card_if_manual(chat_id, user_id)
            return

        # 2. 特殊指令：新会话
        if text == "/new":
            logger.info("[CC] 用户重置会话: user=%s, 旧session=%s", user_id, state["session_id"][:8])
            self._reset_session(user_id, chat_id)
            self.bot.reply(chat_id, f"会话已重置。\n{self._format_status(user_id, chat_id)}")
            self._send_perm_select_card_if_manual(chat_id, user_id)
            return

        # 3. 特殊指令：取消
        if text == "/cancel":
            if state["running"]:
                logger.info("[CC] 用户取消任务: user=%s", user_id)
                self._kill_process(user_id, chat_id)
                state["running"] = False
                state["cancelled"] = True
                # 立即更新执行卡片，移除取消按钮
                mid = state.get("current_message_id")
                if mid:
                    self._patch_card(mid, "**已取消执行**", cancelled=True)
                self.bot.reply(chat_id, "已取消当前任务。")
            else:
                self.bot.reply(chat_id, "当前没有运行中的任务。")
            return

        # 4. 特殊指令：状态
        if text == "/status":
            self.bot.reply(chat_id, self._format_status(user_id, chat_id))
            return

        # 5. 特殊指令：历史会话
        if text == "/session":
            sessions = self._session_store.load_user_sessions(user_id)
            if not sessions:
                self.bot.reply(
                    chat_id,
                    "暂无历史会话记录。完成第一次任务后将自动记录，可在此查看并恢复。",
                )
                return
            card = cards.build_sessions_card(sessions, state["session_id"])
            self.bot.send_message(chat_id, "interactive", json.dumps(card))
            return

        # 5a. 特殊指令：收藏/取消收藏当前会话
        if text == "/star":
            if not state["session_started"]:
                self.bot.reply(chat_id, "当前会话尚未开始，无法收藏。请先发送一条消息。")
                return
            new_starred = self._session_store.toggle_star(user_id, state["session_id"])
            if new_starred is None:
                self.bot.reply(chat_id, "当前会话尚未记录，完成第一次任务后才能收藏。")
                return
            tip = "已收藏当前会话 ⭐" if new_starred else "已取消收藏当前会话"
            self.bot.reply(chat_id, tip)
            return

        # 5b. 特殊指令：重命名当前会话
        if text == "/rename":
            self.bot.reply(chat_id, "请输入新标题，格式：`/rename 新标题`")
            return

        if text.startswith("/rename "):
            new_title = text[len("/rename "):].strip()
            if not new_title:
                self.bot.reply(chat_id, "请输入新标题，格式：`/rename 新标题`")
                return
            if not state["session_started"]:
                self.bot.reply(chat_id, "当前会话尚未开始，无法重命名。请先发送一条消息。")
                return
            ok = self._session_store.rename_session(user_id, state["session_id"], new_title)
            if not ok:
                self.bot.reply(chat_id, "当前会话尚未记录，完成第一次任务后才能重命名。")
                return
            self.bot.reply(chat_id, f"会话已重命名为：{new_title}")
            return

        # 6. 特殊指令：切换目录（不带路径则重置为默认）
        if text == "/cd":
            old_session = state["session_id"]
            self._reset_session(user_id, chat_id)
            state["working_dir"] = resolve_working_dir(self._load_plugin_config().get("default_working_dir", ""))
            logger.info(
                "[CC] 用户重置工作目录为默认: user=%s, 旧session=%s, 新session=%s",
                user_id, old_session[:8], state["session_id"][:8],
            )
            self.bot.reply(
                chat_id,
                f"工作目录已重置为默认。\n{self._format_status(user_id, chat_id)}",
            )
            self._send_perm_select_card_if_manual(chat_id, user_id)
            return

        if text.startswith("/cd "):
            new_dir = os.path.realpath(text[len("/cd "):].strip())
            if os.path.isdir(new_dir):
                old_session = state["session_id"]
                self._reset_session(user_id, chat_id)
                state["working_dir"] = new_dir
                logger.info(
                    "[CC] 用户切换目录: user=%s, dir=%s, 旧session=%s, 新session=%s",
                    user_id, new_dir, old_session[:8], state["session_id"][:8],
                )
                self.bot.reply(
                    chat_id,
                    f"工作目录已切换: {new_dir}\n"
                    f"{self._format_status(user_id, chat_id)}",
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
            card = cards.build_permission_mode_card(state["session_perm_mode"])
            self.bot.send_message(chat_id, "interactive", card)
            return

        # 8. 特殊指令：切换模型（弹出选择卡片）
        if text == "/model":
            if state["running"]:
                self.bot.reply(chat_id, "任务运行中，请等待完成后再切换模型。")
                return
            card = cards.build_model_select_card(
                state.get("session_model", ""),
                self._load_plugin_config()["models"],
            )
            self.bot.send_message(chat_id, "interactive", card)
            return

        # 9. 特殊指令：帮助
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
                "**模型选择**（发送 `/model` 可切换）\n"
                "• 默认使用 CLI 自带模型，无需手动选择\n"
                "• 切换模型仅影响当前会话，新会话恢复默认\n\n"
                "**群聊唤醒模式**\n"
                "群聊中默认需要 @机器人 才能触发响应。在群内发送「唤醒模式」可弹出设置卡片：\n"
                "• 「全部唤醒」— 群内所有消息都直接发给机器人，无需 @\n"
                "• 「仅@唤醒」— 只有 @机器人 的消息才触发响应（默认）\n\n"
                "**退出插件**\n"
                "发送「退出」或「返回」可退出 CC 插件，回到主菜单。"
            )
            self.bot.reply(chat_id, help_text)
            return

        # 10. 并发控制：运行中拒绝新任务
        if state["running"]:
            logger.info("[CC] 拒绝新任务（上一个仍在运行）: user=%s", user_id)
            self.bot.reply(
                chat_id,
                "上一个任务仍在运行中，请等待完成或发送 `/cancel` 终止。",
            )
            return

        # 12. 正常执行：发送 prompt 到 Claude Code
        state["running"] = True
        state["cancelled"] = False
        state["perm_timeout_count"] = 0
        state["last_chat_id"] = chat_id
        logger.info(
            "[CC] 开始执行 prompt: user=%s, session=%s, prompt长度=%d",
            user_id, state["session_id"][:8], len(text),
        )

        # 确保权限确认服务器已启动（首次调用时初始化）
        self._ensure_perm_manager().ensure_server()

        # 发送占位卡片
        placeholder = cards.build_execution_card("正在启动 Claude Code...", running=True)
        message_id = self.bot.send_message_get_id(chat_id, "interactive", placeholder)
        state["current_message_id"] = message_id

        # 启动后台线程
        t = threading.Thread(
            target=self._run_claude_code,
            args=(user_id, chat_id, text, message_id),
            daemon=True,
        )
        t.start()
        self._running_threads[self._state_key(user_id, chat_id)] = t

    def handle_card_action(
        self, user_id: str, chat_id: str, message_id: str, action_value: dict
    ) -> "P2CardActionTriggerResponse":
        """处理卡片按钮点击（取消按钮、权限确认按钮）"""
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

        action = action_value.get("action", "")
        perm_mgr = self._ensure_perm_manager()

        # 飞书表单提交时 button.value 丢失（action 为空），从 form_value 提取
        if not action and "_form_value" in action_value:
            form_value = action_value["_form_value"]

            # 重命名表单：input name 为 rename_title
            if "rename_title" in form_value:
                new_title = (form_value.get("rename_title") or "").strip()
                if not new_title:
                    return self.bot.make_card_response(toast="请输入新标题")
                state = self._get_state(user_id, chat_id)
                target_sid = state.get("_pending_rename", "")
                if not target_sid:
                    return self.bot.make_card_response(toast="无效的会话 ID")
                ok = self._session_store.rename_session(user_id, target_sid, new_title)
                if not ok:
                    return self.bot.make_card_response(toast="重命名失败，会话可能已不存在")
                state.pop("_pending_rename", None)
                sessions = self._session_store.load_user_sessions(user_id)
                card = cards.build_sessions_card(sessions, state["session_id"])
                return self.bot.make_card_response(card=card, toast=f"已重命名 {target_sid[:8]}…")

            # 自定义回答表单：input name 格式为 custom_answer_{qi}
            custom_answer = ""
            question_index = 0
            for key, val in form_value.items():
                if key.startswith("custom_answer_") and val:
                    custom_answer = str(val).strip()
                    try:
                        question_index = int(key.rsplit("_", 1)[-1])
                    except (ValueError, IndexError):
                        pass
                    break
            if not custom_answer:
                return self.bot.make_card_response(toast="请先在输入框中输入你的回答")

            state = self._get_state(user_id, chat_id)
            pending = state.get("_pending_ask_user")
            if not pending:
                return self.bot.make_card_response(toast="该请求已过期或已处理")

            return self._handle_ask_user_response(
                user_id, chat_id, pending["request_id"], question_index, custom_answer,
            )

        if action == "cancel":
            state = self._get_state(user_id, chat_id)
            if state.get("running"):
                logger.info("[CC] 卡片取消按钮点击: user=%s", user_id)
                # wait=False: 不等待进程退出，避免阻塞飞书卡片回调超时
                self._kill_process(user_id, chat_id, wait=False)
                state["running"] = False
                state["cancelled"] = True
                # 立即返回更新后的卡片，移除取消按钮并更新标题
                cancel_card = json.loads(cards.build_execution_card("**已取消执行**", cancelled=True))
                return self.bot.make_card_response(card=cancel_card, toast="已取消执行")
            return self.bot.make_card_response(toast="当前没有运行中的任务")

        if action in ("perm_allow", "perm_deny"):
            request_id = action_value.get("request_id", "")
            behavior = "allow" if action == "perm_allow" else "deny"
            behavior_cn = "允许" if behavior == "allow" else "拒绝"

            if not perm_mgr.server:
                return self.bot.make_card_response(toast="权限服务器未启动")

            ok = perm_mgr.server.resolve_request(request_id, behavior)
            if ok:
                logger.info(
                    "[CC] 用户权限响应: user=%s, request=%s, decision=%s",
                    user_id, request_id[:8], behavior,
                )
                # 更新卡片为已处理状态
                handled_card = cards.build_permission_handled_card(behavior_cn)
                return self.bot.make_card_response(
                    card=json.loads(handled_card),
                    toast=f"已{behavior_cn}",
                )
            return self.bot.make_card_response(toast="该请求已过期或已处理")

        if action == "set_perm_mode":
            new_mode = action_value.get("mode", "interactive")
            if new_mode not in ("interactive", "accept_edits", "bypass"):
                return self.bot.make_card_response(toast="无效的权限模式")
            state = self._get_state(user_id, chat_id)
            if state["running"]:
                return self.bot.make_card_response(toast="任务运行中，无法切换权限模式")
            state["session_perm_mode"] = new_mode
            mode_cn = {"interactive": "交互确认", "accept_edits": "自动接受编辑", "bypass": "全部放行"}[new_mode]
            logger.info("[CC] 用户切换权限模式: user=%s, mode=%s", user_id, new_mode)
            updated_card = cards.build_permission_mode_card(new_mode)
            return self.bot.make_card_response(
                card=json.loads(updated_card),
                toast=f"已切换为{mode_cn}模式",
            )

        if action == "set_model":
            model_alias = action_value.get("model", "")
            # 验证合法性：空字符串（CLI 默认）始终合法；非空须在配置列表中
            valid_aliases = {""} | {m.get("alias", "") for m in self._load_plugin_config()["models"]}
            if model_alias not in valid_aliases:
                return self.bot.make_card_response(toast="无效的模型选择")
            state = self._get_state(user_id, chat_id)
            if state["running"]:
                return self.bot.make_card_response(toast="任务运行中，无法切换模型")
            state["session_model"] = model_alias
            display = self._get_model_label(model_alias) if model_alias else "CLI 默认"
            logger.info("[CC] 用户切换模型: user=%s, model=%s", user_id, model_alias or "(default)")
            updated_card = cards.build_model_select_card(model_alias, self._load_plugin_config()["models"])
            return self.bot.make_card_response(
                card=json.loads(updated_card),
                toast=f"已切换为 {display}",
            )

        if action == "perm_accept_edits":
            request_id = action_value.get("request_id", "")

            if not perm_mgr.server:
                return self.bot.make_card_response(toast="权限服务器未启动")

            # 切换为 accept_edits 模式，同时放行当前挂起的请求
            state = self._get_state(user_id, chat_id)
            state["session_perm_mode"] = "accept_edits"
            logger.info(
                "[CC] 用户通过权限卡片开启 accept_edits 模式: user=%s, request=%s",
                user_id, request_id[:8] if request_id else "?",
            )
            ok = perm_mgr.server.resolve_request(request_id, "allow")
            if ok:
                handled_card = cards.build_permission_handled_card("允许（已开启 accept_edits 模式）")
                return self.bot.make_card_response(
                    card=json.loads(handled_card),
                    toast="已开启 accept_edits 模式，工作目录内文件修改自动放行",
                )
            return self.bot.make_card_response(toast="该请求已过期或已处理")

        if action == "perm_bypass":
            request_id = action_value.get("request_id", "")

            if not perm_mgr.server:
                return self.bot.make_card_response(toast="权限服务器未启动")

            # 开启会话级 bypass 模式
            state = self._get_state(user_id, chat_id)
            state["session_perm_mode"] = "bypass"
            logger.info(
                "[CC] 用户通过权限卡片开启 bypass 模式: user=%s, request=%s",
                user_id, request_id[:8] if request_id else "?",
            )

            # 同时放行当前挂起的请求
            ok = perm_mgr.server.resolve_request(request_id, "allow")
            if ok:
                handled_card = cards.build_permission_handled_card("允许（已开启 bypass 模式）")
                return self.bot.make_card_response(
                    card=json.loads(handled_card),
                    toast="已开启 bypass 模式，本会话后续请求自动放行",
                )
            return self.bot.make_card_response(toast="该请求已过期或已处理")

        if action == "ask_user_answer":
            request_id = action_value.get("request_id", "")
            answer_label = action_value.get("answer_label", "")
            question_index = action_value.get("question_index", 0)
            return self._handle_ask_user_response(
                user_id, chat_id, request_id, question_index, answer_label,
            )

        if action == "ask_user_custom":
            # "其他" 按钮点击：从表单输入框读取自定义回答
            request_id = action_value.get("request_id", "")
            question_index = action_value.get("question_index", 0)

            form_value = action_value.get("_form_value", {})
            custom_answer = (
                form_value.get(f"custom_answer_{question_index}") or ""
            ).strip()
            if not custom_answer:
                return self.bot.make_card_response(toast="请先在输入框中输入你的回答")

            return self._handle_ask_user_response(
                user_id, chat_id, request_id, question_index, custom_answer,
            )

        if action == "show_more_sessions":
            state = self._get_state(user_id, chat_id)
            sessions = self._session_store.load_user_sessions(user_id)
            if not sessions:
                return self.bot.make_card_response(toast="暂无历史会话记录")
            next_count = action_value.get("show_count", _SESSION_INITIAL_COUNT + _SESSION_PAGE_SIZE)
            card = cards.build_sessions_card(sessions, state["session_id"], show_count=next_count)
            return self.bot.make_card_response(card=card)

        if action == "resume_session":
            state = self._get_state(user_id, chat_id)
            if state.get("running"):
                return self.bot.make_card_response(toast="任务运行中，无法切换会话")
            target_sid = action_value.get("session_id", "")
            target_dir = action_value.get("working_dir", "")
            if not target_sid:
                return self.bot.make_card_response(toast="无效的会话 ID")
            if target_sid == state["session_id"]:
                return self.bot.make_card_response(toast="当前会话无需恢复")
            # 校验工作目录是否存在
            resolved_dir = resolve_working_dir(target_dir)
            if not os.path.isdir(resolved_dir):
                return self.bot.make_card_response(
                    toast=f"工作目录已不存在: {resolved_dir}，请使用 /cd 切换到有效目录"
                )
            # 终止旧进程并切换到目标会话
            self._kill_process(user_id, chat_id)
            state["session_id"] = target_sid
            state["session_started"] = True   # 下次调用使用 --resume
            state["working_dir"] = resolved_dir
            state["running"] = False
            default_perm = self._load_plugin_config()["default_perm_mode"]
            state["session_perm_mode"] = "interactive" if default_perm == "manual_select" else default_perm
            state["session_model"] = self._load_plugin_config()["default_model"]
            logger.info(
                "[CC] 用户恢复历史会话: user=%s, session=%s, dir=%s",
                user_id, target_sid[:8], target_dir,
            )
            # 发送历史对话预览卡片
            jsonl_path = find_session_jsonl(target_sid, target_dir)
            if jsonl_path:
                rounds = parse_session_rounds(jsonl_path, HISTORY_PREVIEW_ROUNDS)
                if rounds:
                    card = cards.build_history_preview_card(target_sid, rounds)
                    self.bot.reply_card(chat_id, card)
            self._send_perm_select_card_if_manual(chat_id, user_id)
            return self.bot.make_card_response(
                toast=f"已切换到会话 {target_sid[:8]}…，发送消息继续"
            )

        if action == "toggle_star_session":
            target_sid = action_value.get("session_id", "")
            if not target_sid:
                return self.bot.make_card_response(toast="无效的会话 ID")
            new_starred = self._session_store.toggle_star(user_id, target_sid)
            if new_starred is None:
                return self.bot.make_card_response(toast="会话不存在")
            # 刷新会话列表卡片
            state = self._get_state(user_id, chat_id)
            sessions = self._session_store.load_user_sessions(user_id)
            show_count = action_value.get("show_count", _SESSION_INITIAL_COUNT)
            card = cards.build_sessions_card(sessions, state["session_id"], show_count=show_count)
            tip = "已收藏" if new_starred else "已取消收藏"
            return self.bot.make_card_response(card=card, toast=f"{tip} {target_sid[:8]}…")

        if action == "show_rename_form":
            target_sid = action_value.get("session_id", "")
            if not target_sid:
                return self.bot.make_card_response(toast="无效的会话 ID")
            sessions = self._session_store.load_user_sessions(user_id)
            target = next((s for s in sessions if s["session_id"] == target_sid), None)
            if not target:
                return self.bot.make_card_response(toast="会话不存在")
            # 暂存待重命名的 session_id，用于表单提交时读取
            state = self._get_state(user_id, chat_id)
            state["_pending_rename"] = target_sid
            card = cards.build_rename_card(target)
            return self.bot.make_card_response(card=card)

        if action == "rename_session":
            form_value = action_value.get("_form_value", {})
            new_title = (form_value.get("rename_title") or "").strip()
            if not new_title:
                return self.bot.make_card_response(toast="请输入新标题")
            target_sid = action_value.get("session_id", "")
            if not target_sid:
                # 飞书表单提交可能丢失 button.value，从 state 回退
                state = self._get_state(user_id, chat_id)
                target_sid = state.get("_pending_rename", "")
            if not target_sid:
                return self.bot.make_card_response(toast="无效的会话 ID")
            ok = self._session_store.rename_session(user_id, target_sid, new_title)
            if not ok:
                return self.bot.make_card_response(toast="重命名失败，会话可能已不存在")
            # 清除暂存状态，刷新会话列表
            state = self._get_state(user_id, chat_id)
            state.pop("_pending_rename", None)
            sessions = self._session_store.load_user_sessions(user_id)
            card = cards.build_sessions_card(sessions, state["session_id"])
            return self.bot.make_card_response(card=card, toast=f"已重命名 {target_sid[:8]}…")

        if action == "cancel_rename":
            state = self._get_state(user_id, chat_id)
            state.pop("_pending_rename", None)
            sessions = self._session_store.load_user_sessions(user_id)
            card = cards.build_sessions_card(sessions, state["session_id"])
            return self.bot.make_card_response(card=card)

        return P2CardActionTriggerResponse()
