"""
Claude Code 桥接插件

通过 subprocess 调用本地 Claude Code CLI，
将飞书消息作为 prompt 发送，实时流式回显结果到飞书卡片。
支持会话持续（--session-id）、取消运行、清空会话、切换工作目录等操作。
支持交互式权限确认：通过 PermissionRequest Hook + HTTP 服务器，
将 Claude Code 的权限请求转发给飞书用户确认。
"""

import json
import logging
import os
import pathlib
import pwd
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

# 默认配置
_DEFAULT_TIMEOUT = 600          # 默认超时 10 分钟
_DEFAULT_MAX_OUTPUT = 28000     # 飞书卡片 markdown 最大字符数
_DEFAULT_MAX_TURNS = 50         # Claude Code 最大轮次
_DEFAULT_PERM_PORT = 9876       # 权限确认 HTTP 服务器默认端口
_DEFAULT_PERM_TIMEOUT = 120     # 用户确认超时（秒）

PLUGIN_KEYWORD = "CC"


class ClaudeCodePlugin(Plugin):
    """Claude Code 桥接插件

    在服务器本地通过 subprocess 调用 claude CLI，
    将用户飞书消息作为 prompt，流式回显 Claude Code 的输出。
    """

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
                "permission_mode": cc_cfg.get("permission_mode", "bypassPermissions"),
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
            self.user_states[user_id] = {
                "active": False,
                "session_id": str(uuid.uuid4()),
                "session_started": False,
                "running": False,
                "working_dir": cfg["default_working_dir"],
                "last_chat_id": "",
                "bypass_permission": False,  # 会话级免确认模式，默认关闭
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
        state["bypass_permission"] = False
        return state["session_id"]

    # ---- 权限确认服务器 ----

    def _needs_permission_server(self) -> bool:
        """判断是否需要启动权限确认服务器

        默认始终启用交互式权限确认（用户可在会话内切换免确认模式），
        因此权限服务器始终需要启动。
        """
        return True

    def _ensure_permission_server(self) -> None:
        """确保权限确认服务器已启动（懒初始化）"""
        if self._perm_server_started or not self._needs_permission_server():
            return

        cfg = self._load_plugin_config()
        port = cfg["permission_server_port"]
        timeout = cfg["permission_timeout"]

        self._perm_server = PermissionServer(
            port=port,
            timeout=timeout,
            on_permission_request=self._on_permission_request,
        )
        try:
            self._perm_server.start()
            self._perm_server_started = True
            # 写入端口文件，供 Hook 脚本读取
            self._write_port_file(port)
            # 自动注册 Hook 到 Claude 设置
            self._setup_hook()
        except Exception as e:
            logger.error("[CC] 权限确认服务器启动失败: %s", e, exc_info=True)
            self._perm_server = None

    def _write_port_file(self, port: int) -> None:
        """将端口号写入 ~/.claude/.feishu_perm_port，供 Hook 脚本读取"""
        cfg = self._load_plugin_config()
        run_as_user = cfg.get("run_as_user", "")

        # 确定目标用户的 HOME 目录
        if run_as_user and os.getuid() == 0:
            try:
                home_dir = pwd.getpwnam(run_as_user).pw_dir
            except KeyError:
                home_dir = os.path.expanduser("~")
        else:
            home_dir = os.path.expanduser("~")

        port_file = pathlib.Path(home_dir) / ".claude" / ".feishu_perm_port"
        try:
            port_file.parent.mkdir(parents=True, exist_ok=True)
            port_file.write_text(str(port))
            # 如果以 root 运行且有 run_as_user，修正文件归属
            if run_as_user and os.getuid() == 0:
                try:
                    pw = pwd.getpwnam(run_as_user)
                    os.chown(port_file, pw.pw_uid, pw.pw_gid)
                except (KeyError, OSError) as e:
                    logger.warning("[CC] 修正端口文件归属失败: %s", e)
            logger.info("[CC] 端口文件已写入: %s", port_file)
        except OSError as e:
            logger.error("[CC] 写入端口文件失败: %s", e)

    def _setup_hook(self) -> None:
        """自动注册 PreToolUse Hook 到 Claude 用户设置

        将 Hook 脚本路径写入目标用户的 ~/.claude/settings.json。
        """
        cfg = self._load_plugin_config()
        run_as_user = cfg.get("run_as_user", "")

        if run_as_user and os.getuid() == 0:
            try:
                home_dir = pwd.getpwnam(run_as_user).pw_dir
            except KeyError:
                logger.error("[CC] Hook 注册失败: 用户 %s 不存在", run_as_user)
                return
        else:
            home_dir = os.path.expanduser("~")

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
        """权限请求回调：根据会话设置决定自动放行或发送飞书确认卡片

        若用户当前会话已开启免确认模式（bypass_permission=True），直接放行；
        否则发送飞书交互卡片等待用户点击确认。
        """
        # 检查会话级免确认模式
        state = self._get_state(user_id)
        if state.get("bypass_permission", False):
            logger.info(
                "[CC] 免确认模式自动放行: user=%s, tool=%s, request=%s",
                user_id, tool_name, request_id[:8],
            )
            if self._perm_server:
                self._perm_server.resolve_request(request_id, "allow")
            return

        # 默认模式：发送飞书权限确认卡片
        card = self._build_permission_card(request_id, tool_name, tool_input)
        try:
            self.bot.send_message(chat_id, "interactive", card)
            logger.info(
                "[CC] 已发送权限确认卡片: user=%s, tool=%s, request=%s",
                user_id, tool_name, request_id[:8],
            )
        except Exception as e:
            logger.error("[CC] 发送权限确认卡片失败: %s", e, exc_info=True)
            raise

    @staticmethod
    def _build_permission_card(request_id: str, tool_name: str, tool_input: dict) -> str:
        """构造权限确认飞书卡片"""
        # 格式化工具输入的展示内容
        if tool_name == "Bash" and "command" in tool_input:
            input_display = f"```\n{tool_input['command']}\n```"
        elif tool_name == "Edit" or tool_name == "Write":
            file_path = tool_input.get("file_path", tool_input.get("path", ""))
            input_display = f"文件: `{file_path}`"
        else:
            # 通用展示：JSON 格式
            input_display = f"```json\n{json.dumps(tool_input, indent=2, ensure_ascii=False)[:1000]}\n```"

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
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "允许"},
                            "type": "primary",
                            "value": {
                                "action": "perm_allow",
                                "plugin": PLUGIN_KEYWORD,
                                "request_id": request_id,
                            },
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "拒绝"},
                            "type": "danger",
                            "value": {
                                "action": "perm_deny",
                                "plugin": PLUGIN_KEYWORD,
                                "request_id": request_id,
                            },
                        },
                    ],
                },
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
            cwd = state.get("working_dir") or cfg["default_working_dir"] or None

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

            line_count = 0
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                line_count += 1

                text_chunk, meta = self._parse_stream_line(line, has_assistant_text)
                if text_chunk:
                    if not has_assistant_text:
                        logger.info("[CC] 收到首条输出: user=%s, pid=%d", user_id, proc.pid)
                    has_assistant_text = True
                    full_text += text_chunk

                    # 截断保护
                    if len(full_text) > cfg["max_output_chars"]:
                        full_text = full_text[:cfg["max_output_chars"]]
                        full_text += "\n\n**[输出已截断，超出飞书卡片字符限制]**"
                        logger.warning(
                            "[CC] 输出截断: user=%s, 字符数已达 %d 上限",
                            user_id, cfg["max_output_chars"],
                        )
                        self._kill_process(user_id)
                        break

                    # 节流更新
                    if message_id:
                        now = time.time()
                        chars_since = len(full_text) - last_patch_len
                        time_since = now - last_patch_time
                        if chars_since >= _PATCH_MIN_CHARS or time_since >= _PATCH_INTERVAL:
                            self._patch_card(message_id, full_text, running=True)
                            last_patch_len = len(full_text)
                            last_patch_time = now

                if meta:
                    cost_info = meta
                    logger.info("[CC] 收到结果统计: user=%s, %s", user_id, meta)

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

            # 最终内容组装
            if not full_text:
                full_text = "Claude Code 未产生输出。"
                if stderr_output:
                    full_text += f"\n\nstderr:\n```\n{stderr_output[:2000]}\n```"

            if cost_info:
                full_text += f"\n\n---\n{cost_info}"

            if message_id:
                self._patch_card(message_id, full_text, running=False)

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
    def _parse_stream_line(line: str, has_previous_text: bool) -> tuple[str, str]:
        """解析 stream-json 单行

        Args:
            line: 一行 JSON 字符串
            has_previous_text: 之前是否已提取到 assistant 文本

        Returns:
            (text_chunk, meta_info) 元组
        """
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return ("", "")

        event_type = data.get("type", "")
        text_chunk = ""
        meta_info = ""

        if event_type == "assistant":
            # 提取 assistant 消息中的文本块
            message = data.get("message", {})
            content_blocks = message.get("content", [])
            parts = []
            for block in content_blocks:
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
            if parts:
                text_chunk = "\n\n".join(parts)
                # 如果之前已有文本，加分隔
                if has_previous_text:
                    text_chunk = "\n\n---\n\n" + text_chunk

        elif event_type == "result":
            # result 事件提取统计信息，不重复追加文本
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

            # 如果之前没有 assistant 文本，用 result 的文本
            if not has_previous_text:
                result_text = data.get("result", "")
                if result_text:
                    text_chunk = result_text

        return (text_chunk, meta_info)

    # ---- 飞书卡片 ----

    @staticmethod
    def _build_card(text: str, running: bool = False) -> str:
        """构造飞书卡片 JSON"""
        if running:
            template = "turquoise"
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

    def _patch_card(self, message_id: str, text: str, running: bool = True) -> None:
        """更新飞书卡片消息"""
        content = self._build_card(text, running=running)
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
            session_id = state["session_id"]
            working_dir = state.get("working_dir") or "(默认)"
            status = "运行中" if state["running"] else "空闲"
            perm_mode = "bypass" if state.get("bypass_permission", False) else "interactive"
            self.bot.reply(
                chat_id,
                f"Claude Code 已激活。\n"
                f"会话: {session_id[:8]}...\n"
                f"工作目录: {working_dir}\n"
                f"状态: {status}\n"
                f"权限模式: {perm_mode}\n\n"
                f"直接发送消息作为 prompt 执行。\n"
                f"特殊指令:\n"
                f"- `/new` 重置会话\n"
                f"- `/cancel` 终止运行中的任务\n"
                f"- `/status` 查看当前状态\n"
                f"- `/bypass` 切换权限确认模式\n"
                f"- `/cd <路径>` 切换工作目录（会同时重置会话）",
            )
            return

        # 2. 特殊指令：新会话
        if text == "/new":
            logger.info("[CC] 用户重置会话: user=%s, 旧session=%s", user_id, state["session_id"][:8])
            new_sid = self._reset_session(user_id)
            self.bot.reply(chat_id, f"会话已重置。新会话: {new_sid[:8]}...")
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
            session_id = state["session_id"]
            working_dir = state.get("working_dir") or "(默认)"
            status = "运行中" if state["running"] else "空闲"
            perm_mode = "bypass" if state.get("bypass_permission", False) else "interactive"
            self.bot.reply(
                chat_id,
                f"会话: {session_id[:8]}...\n"
                f"工作目录: {working_dir}\n"
                f"状态: {status}\n"
                f"权限模式: {perm_mode}",
            )
            return

        # 5. 特殊指令：切换目录
        if text.startswith("/cd "):
            new_dir = text[len("/cd "):].strip()
            if os.path.isdir(new_dir):
                old_session = state["session_id"]
                new_sid = self._reset_session(user_id)
                state["working_dir"] = new_dir
                logger.info(
                    "[CC] 用户切换目录: user=%s, dir=%s, 旧session=%s, 新session=%s",
                    user_id, new_dir, old_session[:8], new_sid[:8],
                )
                self.bot.reply(
                    chat_id,
                    f"工作目录已切换: {new_dir}\n"
                    f"会话已重置（新会话: {new_sid[:8]}...）",
                )
            else:
                self.bot.reply(chat_id, f"目录不存在: {new_dir}")
            return

        # 6. 特殊指令：切换权限确认模式
        if text == "/bypass":
            if state["running"]:
                self.bot.reply(chat_id, "任务运行中，请等待完成后再切换权限模式。")
                return
            state["bypass_permission"] = not state.get("bypass_permission", False)
            if state["bypass_permission"]:
                logger.info("[CC] 用户开启免确认模式: user=%s", user_id)
                self.bot.reply(
                    chat_id,
                    "已开启 bypass 模式。本会话内所有权限请求将自动放行。\n"
                    "再次发送 `/bypass` 可切换回交互确认模式。",
                )
            else:
                logger.info("[CC] 用户关闭免确认模式: user=%s", user_id)
                self.bot.reply(
                    chat_id,
                    "已恢复交互确认模式。权限请求将通过飞书卡片确认。\n"
                    "再次发送 `/bypass` 可切换为 bypass 模式。",
                )
            return

        # 7. 并发控制：运行中拒绝新任务
        if state["running"]:
            logger.info("[CC] 拒绝新任务（上一个仍在运行）: user=%s", user_id)
            self.bot.reply(
                chat_id,
                "上一个任务仍在运行中，请等待完成或发送 `/cancel` 终止。",
            )
            return

        # 7. 正常执行：发送 prompt 到 Claude Code
        state["running"] = True
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
