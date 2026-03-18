"""
Claude Code 权限服务器生命周期管理

管理权限确认 HTTP 服务器的启动、Hook 注册、端口文件管理，
以及权限请求的回调决策逻辑。
通过回调注入与主插件解耦。
"""

import atexit
import json
import logging
import os
import pathlib
import pwd
import socket
import subprocess
from typing import Callable, Optional

from plugins.claude_code.permission_server import PermissionServer
from plugins.claude_code import cards

logger = logging.getLogger(__name__)

# 默认配置
_DEFAULT_PERM_PORT = 9876       # 权限确认 HTTP 服务器默认端口
_DEFAULT_PERM_TIMEOUT = 120     # 用户确认超时（秒）
_ASK_USER_TIMEOUT = 300         # AskUserQuestion 用户响应超时（秒）
_EXIT_PLAN_TIMEOUT = 600        # ExitPlanMode 用户审批超时（秒）

import threading


def _diagnose_port_occupation(port: int) -> str:
    """诊断端口占用情况，返回可读的诊断信息

    检查项：
    1. 尝试连接端口，判断是否有活跃服务
    2. 通过 ss/lsof 查找占用进程的 PID 和命令
    """
    lines: list[str] = []
    addr = f"127.0.0.1:{port}"

    # 1. 尝试 TCP 连接，判断端口是否有活跃服务
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
            lines.append(f"端口 {port} 有活跃的 TCP 服务（连接成功）")
    except ConnectionRefusedError:
        lines.append(f"端口 {port} 处于 LISTEN 但连接被拒绝（可能在关闭中）")
    except OSError as e:
        lines.append(f"端口 {port} 连接测试: {e}")

    # 2. 通过 ss 查找占用进程
    try:
        result = subprocess.run(
            ["ss", "-tlnp", f"sport = :{port}"],
            capture_output=True, text=True, timeout=5,
        )
        ss_output = result.stdout.strip()
        if ss_output:
            # 跳过表头，取实际数据行
            data_lines = [l for l in ss_output.splitlines() if str(port) in l]
            for dl in data_lines:
                lines.append(f"ss: {dl.strip()}")
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    # 3. 如果 ss 没输出有用信息，尝试 lsof
    if not any("ss:" in l for l in lines):
        try:
            result = subprocess.run(
                ["lsof", f"-iTCP:{port}", "-sTCP:LISTEN", "-P", "-n"],
                capture_output=True, text=True, timeout=5,
            )
            lsof_output = result.stdout.strip()
            if lsof_output:
                for ll in lsof_output.splitlines():
                    lines.append(f"lsof: {ll.strip()}")
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

    # 4. 通过 /proc/net/tcp 查找 socket inode（兜底方案）
    if not any(("ss:" in l or "lsof:" in l) for l in lines):
        try:
            hex_port = f"{port:04X}"
            with open("/proc/net/tcp") as f:
                for tcp_line in f:
                    parts = tcp_line.split()
                    if len(parts) >= 4 and parts[1].endswith(f":{hex_port}") and parts[3] == "0A":
                        inode = parts[9] if len(parts) > 9 else "?"
                        uid = parts[7] if len(parts) > 7 else "?"
                        lines.append(f"/proc/net/tcp: 监听中, inode={inode}, uid={uid}")
        except (OSError, IndexError):
            pass

    return "; ".join(lines) if lines else "无法获取端口占用详情"


def is_within_working_dir(file_path: str, working_dir: str) -> bool:
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


class PermissionManager:
    """权限服务器生命周期管理

    通过回调函数与主插件解耦：
    - get_state: 获取用户会话状态
    - load_config: 获取插件配置
    - send_card: 发送飞书卡片消息
    - send_card_get_id: 发送飞书卡片消息并返回 message_id
    - urgent_message: 对已有消息发送应用内加急通知
    """

    def __init__(
        self,
        data_dir: pathlib.Path,
        load_config: Callable[[], dict],
        get_state: Callable[[str, str], dict],  # (user_id, chat_id) -> state
        send_card: Callable[[str, str], None],
        send_card_get_id: Callable[[str, str], Optional[str]],
        urgent_message: Optional[Callable[[str, list[str]], bool]] = None,
    ):
        self._data_dir = data_dir
        self._load_config = load_config
        self._get_state = get_state
        self._send_card = send_card
        self._send_card_get_id = send_card_get_id
        self._urgent_message = urgent_message

        self._server: Optional[PermissionServer] = None
        self._started = False
        self._lock = threading.Lock()

    @property
    def server(self) -> Optional[PermissionServer]:
        """获取底层 PermissionServer 实例"""
        return self._server

    @property
    def started(self) -> bool:
        """权限服务器是否已启动"""
        return self._started

    def ensure_server(self) -> bool:
        """确保权限确认服务器已启动（懒初始化，线程安全）

        配置中 port=0 时由 OS 分配空闲端口，彻底避免固定端口冲突。

        Returns:
            True 表示服务器已正常运行，False 表示启动失败
        """
        if self._started:
            return True

        with self._lock:
            # 双重检查：加锁后再次确认，防止并发重复初始化
            if self._started:
                return True

            # 启动前清理可能残留的旧端口文件（上次异常退出遗留）
            # 防止 hook 脚本在服务器启动前读到旧端口并被 fail-close 拒绝
            self._delete_port_file()

            cfg = self._load_config()
            port = cfg["permission_server_port"]
            timeout = cfg["permission_timeout"]

            perm_server = PermissionServer(
                port=port,
                timeout=timeout,
                on_permission_request=self.on_permission_request,
                on_permission_timeout=self.on_permission_timeout,
            )
            try:
                perm_server.start()
            except OSError as e:
                # 端口绑定失败：输出详细诊断信息
                diag = _diagnose_port_occupation(port)
                logger.error(
                    "[CC] 权限确认服务器启动失败 (port=%d): %s | 端口诊断: %s",
                    port, e, diag,
                )
                return False
            except Exception as e:
                logger.error("[CC] 权限确认服务器启动失败 (port=%d): %s", port, e, exc_info=True)
                return False

            # start() 成功后才更新状态
            # 使用实际监听端口（port=0 时为 OS 分配的端口）
            actual_port = perm_server.actual_port
            self._server = perm_server
            self._started = True
            self._write_port_file(actual_port)
            self._register_cleanup()
            self._setup_hook()
            return True

    def on_permission_request(
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
        state = self._get_state(user_id, chat_id)
        perm_mode = state.get("session_perm_mode", "interactive")
        effective_working_dir = state["working_dir"]  # 始终为有效绝对路径
        logger.info(
            "[CC] 权限请求: user=%s, tool=%s, perm_mode=%s, request=%s, input=%s",
            user_id, tool_name, perm_mode, request_id[:8], input_summary.replace("\n", "↵"),
        )

        # EnterPlanMode 特殊处理：标记进入计划模式，清除旧追踪路径，自动放行
        if tool_name == "EnterPlanMode":
            state["_in_plan_mode"] = True
            state.pop("_last_plan_file", None)
            logger.info("[CC] 进入计划模式，已标记 _in_plan_mode")
            if self._server:
                self._server.resolve_request(request_id, "allow")
            return

        # 计划模式下追踪 .md 文件写入，供 ExitPlanMode 兜底定位计划文件
        if tool_name in ("Write", "Edit") and state.get("_in_plan_mode"):
            fp = tool_input.get("file_path", "")
            if fp and fp.endswith(".md"):
                state["_last_plan_file"] = fp
                logger.debug("[CC] 追踪计划文件写入: %s", fp)

        # bypass 模式：直接放行所有权限请求（AskUserQuestion 和 ExitPlanMode 除外，
        # 前者需转发用户输入，后者需展示计划并等待用户审批）
        if perm_mode == "bypass" and tool_name not in ("AskUserQuestion", "ExitPlanMode"):
            if self._server:
                self._server.resolve_request(request_id, "allow")
            return

        # AskUserQuestion 特殊处理：将问题转发到飞书，用户通过卡片按钮回答
        if tool_name == "AskUserQuestion":
            questions = tool_input.get("questions", [])
            if not questions:
                if self._server:
                    self._server.resolve_request(
                        request_id, "deny", reason="AskUserQuestion 未包含有效问题。"
                    )
                return
            card = cards.build_ask_user_card(request_id, questions)
            # 超时按问题数量缩放
            timeout = _ASK_USER_TIMEOUT * len(questions)
            if self._server:
                self._server.set_request_timeout(request_id, timeout)
            # 使用 send_card_get_id 检测发送是否成功
            msg_id = self._send_card_get_id(chat_id, card)
            if msg_id:
                # 保存表单元数据：飞书表单提交时 button.value 丢失，需从此恢复
                state = self._get_state(user_id, chat_id)
                state["_pending_ask_user"] = {
                    "request_id": request_id,
                    "questions": questions,
                    "answers": {},
                    "total": len(questions),
                }
                logger.info(
                    "[CC] 已发送用户问题卡片: user=%s, request=%s, 问题数=%d",
                    user_id, request_id[:8], len(questions),
                )
                # 发送加急通知，提醒用户及时回答
                if self._urgent_message:
                    try:
                        self._urgent_message(msg_id, [user_id])
                    except Exception as ue:
                        logger.debug("[CC] 用户问题卡片加急通知失败: %s", ue)
            else:
                # 卡片发送失败（如含不支持的元素类型），立即拒绝请求避免挂起
                logger.error(
                    "[CC] 用户问题卡片发送失败: user=%s, request=%s", user_id, request_id[:8],
                )
                if self._server:
                    self._server.resolve_request(
                        request_id, "deny",
                        reason="无法向用户发送问题卡片，请根据上下文自行做出最合理的判断。",
                    )
            return

        # ExitPlanMode 特殊处理：获取计划内容，通过飞书卡片展示给用户审批
        # 三级回退：追踪的文件路径 → tool_input["plan"] → 多目录 mtime 搜索
        # 追踪文件最优先：EnterPlanMode 已清除旧路径，Write 追踪到的是本轮实际写入的文件；
        # tool_input["plan"] 是 Claude Code 从其指定文件读取的，当用户写入非指定路径时内容可能过时。
        if tool_name == "ExitPlanMode":
            state.pop("_in_plan_mode", None)  # 退出计划模式
            plan_content = ""
            # 1) 从当前计划模式追踪到的文件路径读取（最可靠）
            tracked_file = state.get("_last_plan_file", "")
            if tracked_file:
                try:
                    content = pathlib.Path(tracked_file).read_text(encoding="utf-8")
                    if content.strip():
                        plan_content = content
                        logger.info(
                            "[CC] 从追踪的计划文件读取: %s (%d chars)",
                            tracked_file, len(plan_content),
                        )
                except OSError as e:
                    logger.warning("[CC] 追踪的计划文件读取失败: %s: %s", tracked_file, e)
            # 2) 使用 Claude Code 在 tool_input 中传递的计划内容（兜底）
            if not plan_content:
                plan_content = tool_input.get("plan", "").strip()
                if plan_content:
                    logger.info("[CC] 使用 tool_input 中的计划内容 (%d chars)", len(plan_content))
            # 3) 多目录 mtime 搜索兜底
            if not plan_content:
                plan_content = self._read_latest_plan_file(effective_working_dir)
            allowed_prompts = tool_input.get("allowedPrompts", [])
            card = cards.build_plan_approval_card(request_id, plan_content, allowed_prompts)
            if self._server:
                self._server.set_request_timeout(request_id, _EXIT_PLAN_TIMEOUT)
            msg_id = self._send_card_get_id(chat_id, card)
            if msg_id:
                state = self._get_state(user_id, chat_id)
                state["_pending_exit_plan"] = {
                    "request_id": request_id,
                    "plan_content": plan_content,
                }
                logger.info(
                    "[CC] 已发送计划审批卡片: user=%s, request=%s, 计划长度=%d",
                    user_id, request_id[:8], len(plan_content),
                )
                # 发送加急通知，提醒用户及时审批
                if self._urgent_message:
                    try:
                        self._urgent_message(msg_id, [user_id])
                    except Exception as ue:
                        logger.debug("[CC] 计划审批卡片加急通知失败: %s", ue)
            else:
                logger.error(
                    "[CC] 计划审批卡片发送失败: user=%s, request=%s",
                    user_id, request_id[:8],
                )
                if self._server:
                    self._server.resolve_request(
                        request_id, "deny",
                        reason="无法向用户发送计划审批卡片，请直接以文本形式展示计划内容。",
                    )
            return

        # accept_edits 模式：文件修改类工具在工作目录内自动放行
        if perm_mode == "accept_edits":
            if tool_name in ("Write", "Edit", "NotebookEdit"):
                fp = tool_input.get("file_path") or tool_input.get("notebook_path", "")
                if fp and is_within_working_dir(fp, effective_working_dir):
                    logger.info(
                        "[CC] accept_edits 模式自动放行: tool=%s, file=%s", tool_name, fp,
                    )
                    if self._server:
                        self._server.resolve_request(request_id, "allow")
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
            and is_within_working_dir(fp, effective_working_dir)
        )
        card = cards.build_permission_card(
            request_id, tool_name, tool_input, show_accept_edits_option, effective_working_dir
        )
        try:
            self._send_card(chat_id, card)
            logger.info(
                "[CC] 已发送权限确认卡片: user=%s, tool=%s, request=%s",
                user_id, tool_name, request_id[:8],
            )
        except Exception as e:
            logger.error("[CC] 发送权限确认卡片失败: %s", e, exc_info=True)
            raise

    def on_permission_timeout(
        self,
        user_id: str,
        chat_id: str,
        request_id: str,
        tool_name: str,
        timeout: int,
    ) -> None:
        """权限确认超时回调：记录超时事件到用户状态，供任务结束时在卡片中提示"""
        state = self._get_state(user_id, chat_id)
        state["perm_timeout_count"] = state.get("perm_timeout_count", 0) + 1
        logger.warning(
            "[CC] 权限确认超时: user=%s, tool=%s, request=%s, timeout=%ds",
            user_id, tool_name, request_id[:8], timeout,
        )

    # ---- 内部方法 ----

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
        port_file = self._data_dir / ".feishu_perm_port"
        timeout_file = self._data_dir / ".feishu_perm_timeout"
        try:
            port_file.parent.mkdir(parents=True, exist_ok=True)
            port_file.write_text(str(port))
            # 写入超时值，hook 脚本据此设置 curl --max-time
            cfg = self._load_config()
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
        """删除端口文件和超时文件，让 hook 脚本不再尝试连接"""
        port_file = self._data_dir / ".feishu_perm_port"
        timeout_file = self._data_dir / ".feishu_perm_timeout"
        try:
            port_file.unlink(missing_ok=True)
            timeout_file.unlink(missing_ok=True)
            logger.info("[CC] 端口/超时文件已删除: %s", port_file)
        except OSError as e:
            logger.warning("[CC] 删除端口/超时文件失败: %s", e)

    def _register_cleanup(self) -> None:
        """注册进程退出时的端口文件清理（通过 atexit）

        配合 main.py 中的 SIGTERM→sys.exit(0) 处理器，
        确保正常退出和 SIGTERM 时都能清理端口文件。
        """
        def _cleanup():
            self._delete_port_file()
            if self._server:
                try:
                    self._server.stop()
                except Exception:
                    pass

        atexit.register(_cleanup)

    def _read_latest_plan_file(self, working_dir: str) -> str:
        """从多个候选目录中读取 mtime 最新的 .claude/plans/*.md 文件

        搜索顺序：工作目录、CC 进程用户 HOME 目录。
        合并所有候选文件后按 mtime 取最新，避免固定搜索单一目录导致读到旧文件。

        Returns:
            计划文件内容字符串；未找到或读取失败时返回空字符串
        """
        # 构建候选 plans 目录列表（去重）
        candidate_dirs: list[pathlib.Path] = []
        seen: set[str] = set()
        for base in self._plan_search_dirs(working_dir):
            plans_dir = pathlib.Path(base) / ".claude" / "plans"
            key = str(plans_dir)
            if key not in seen:
                seen.add(key)
                candidate_dirs.append(plans_dir)

        # 合并所有目录下的 .md 文件
        all_md: list[pathlib.Path] = []
        for plans_dir in candidate_dirs:
            if plans_dir.is_dir():
                all_md.extend(plans_dir.glob("*.md"))

        if not all_md:
            logger.warning("[CC] 所有候选计划目录均无 .md 文件: %s", candidate_dirs)
            return ""

        # 按 mtime 降序取最新
        all_md.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        latest = all_md[0]
        try:
            content = latest.read_text(encoding="utf-8")
            logger.info("[CC] 读取计划文件: %s (%d chars)", latest, len(content))
            return content
        except OSError as e:
            logger.error("[CC] 读取计划文件失败: %s: %s", latest, e)
            return ""

    def _plan_search_dirs(self, working_dir: str) -> list[str]:
        """返回计划文件搜索的基础目录列表

        包含工作目录和 CC 进程用户的 HOME 目录（由 run_as_user 配置决定）。
        """
        dirs = [working_dir]
        cfg = self._load_config()
        run_as_user = cfg.get("run_as_user", "")
        home_dir = self._get_target_home_dir(run_as_user)
        if home_dir:
            dirs.append(home_dir)
        return dirs

    def _setup_hook(self) -> None:
        """自动注册 PreToolUse Hook 到 Claude 用户设置

        将 Hook 脚本路径写入目标用户的 ~/.claude/settings.json。
        """
        cfg = self._load_config()
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
        from plugins.claude_code.constants import PLUGIN_DIR
        hook_script = str(PLUGIN_DIR / "permission_hook.sh")

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
