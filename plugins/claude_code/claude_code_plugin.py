"""
Claude Code 桥接插件

通过 subprocess 调用本地 Claude Code CLI，
将飞书消息作为 prompt 发送，实时流式回显结果到飞书卡片。
支持会话持续（--session-id）、取消运行、清空会话、切换工作目录等操作。
"""

import json
import logging
import os
import pwd
import subprocess
import threading
import time
import uuid
from typing import Optional

from config import load_config
from core.plugin import Plugin

logger = logging.getLogger(__name__)

# 流式更新控制（与 claude_chat 一致）
_PATCH_INTERVAL = 0.5       # 最小更新间隔（秒）
_PATCH_MIN_CHARS = 50       # 最小新增字符数触发更新

# 默认配置
_DEFAULT_TIMEOUT = 600          # 默认超时 10 分钟
_DEFAULT_MAX_OUTPUT = 28000     # 飞书卡片 markdown 最大字符数
_DEFAULT_MAX_TURNS = 50         # Claude Code 最大轮次

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
            }
        return self.user_states[user_id]

    def is_user_active(self, user_id: str) -> bool:
        """用户是否在活跃会话中"""
        return self._get_state(user_id).get("active", False)

    def deactivate_user(self, user_id: str) -> None:
        """清理用户状态，终止运行中的进程"""
        self._kill_process(user_id)
        self.user_states.pop(user_id, None)

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
        perm = cfg.get("permission_mode", "")
        if perm:
            cmd.extend(["--permission-mode", perm])
        max_turns = cfg.get("max_turns", _DEFAULT_MAX_TURNS)
        cmd.extend(["--max-turns", str(max_turns)])
        return cmd

    def _kill_process(self, user_id: str) -> None:
        """安全终止用户的运行中进程"""
        proc = self._running_processes.pop(user_id, None)
        if proc and proc.poll() is None:
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

        try:
            cmd = self._build_command(
                prompt, state["session_id"],
                resume=state.get("session_started", False),
            )
            cwd = state.get("working_dir") or cfg["default_working_dir"] or None

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

            # 启动超时定时器
            timer = self._start_timeout_timer(user_id, cfg["timeout"])

            # 流式读取并更新卡片
            full_text = ""
            last_patch_len = 0
            last_patch_time = time.time()
            cost_info = ""
            has_assistant_text = False

            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue

                text_chunk, meta = self._parse_stream_line(line, has_assistant_text)
                if text_chunk:
                    has_assistant_text = True
                    full_text += text_chunk

                    # 截断保护
                    if len(full_text) > cfg["max_output_chars"]:
                        full_text = full_text[:cfg["max_output_chars"]]
                        full_text += "\n\n**[输出已截断，超出飞书卡片字符限制]**"
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

            # 等待进程结束
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._kill_process(user_id)

            stderr_output = proc.stderr.read() if proc.stderr else ""

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
            logger.error(error_msg)
            if message_id:
                self._patch_card(message_id, error_msg, running=False)
            else:
                self.bot.reply(chat_id, error_msg)

        except Exception as e:
            logger.error("Claude Code 执行失败: %s", e, exc_info=True)
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
            state["active"] = True
            state["last_chat_id"] = chat_id
            session_id = state["session_id"]
            working_dir = state.get("working_dir") or "(默认)"
            status = "运行中" if state["running"] else "空闲"
            self.bot.reply(
                chat_id,
                f"Claude Code 已激活。\n"
                f"会话: {session_id[:8]}...\n"
                f"工作目录: {working_dir}\n"
                f"状态: {status}\n\n"
                f"直接发送消息作为 prompt 执行。\n"
                f"特殊指令:\n"
                f"- 「新会话」重置会话\n"
                f"- 「取消」终止运行中的任务\n"
                f"- 「状态」查看当前状态\n"
                f"- 「切换目录 <路径>」切换工作目录",
            )
            return

        # 2. 特殊指令：新会话
        if text == "新会话":
            self._kill_process(user_id)
            state["session_id"] = str(uuid.uuid4())
            state["session_started"] = False
            state["running"] = False
            self.bot.reply(
                chat_id,
                f"会话已重置。新会话: {state['session_id'][:8]}...",
            )
            return

        # 3. 特殊指令：取消
        if text == "取消":
            if state["running"]:
                self._kill_process(user_id)
                state["running"] = False
                self.bot.reply(chat_id, "已取消当前任务。")
            else:
                self.bot.reply(chat_id, "当前没有运行中的任务。")
            return

        # 4. 特殊指令：状态
        if text == "状态":
            session_id = state["session_id"]
            working_dir = state.get("working_dir") or "(默认)"
            status = "运行中" if state["running"] else "空闲"
            self.bot.reply(
                chat_id,
                f"会话: {session_id[:8]}...\n"
                f"工作目录: {working_dir}\n"
                f"状态: {status}",
            )
            return

        # 5. 特殊指令：切换目录
        if text.startswith("切换目录 "):
            new_dir = text[len("切换目录 "):].strip()
            if os.path.isdir(new_dir):
                state["working_dir"] = new_dir
                self.bot.reply(chat_id, f"工作目录已切换: {new_dir}")
            else:
                self.bot.reply(chat_id, f"目录不存在: {new_dir}")
            return

        # 6. 并发控制：运行中拒绝新任务
        if state["running"]:
            self.bot.reply(
                chat_id,
                "上一个任务仍在运行中，请等待完成或发送「取消」终止。",
            )
            return

        # 7. 正常执行：发送 prompt 到 Claude Code
        state["running"] = True
        state["last_chat_id"] = chat_id

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
        """处理卡片按钮点击（取消按钮）"""
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

        action = action_value.get("action", "")

        if action == "cancel":
            state = self._get_state(user_id)
            if state.get("running"):
                self._kill_process(user_id)
                state["running"] = False
                return self.bot.make_card_response(toast="已取消执行")
            return self.bot.make_card_response(toast="当前没有运行中的任务")

        return P2CardActionTriggerResponse()
