"""
Claude Code 权限服务器生命周期管理

管理权限确认 HTTP 服务器的启动、Hook 注册、端口文件管理，
以及权限请求的回调决策逻辑。
通过回调注入与主插件解耦。
"""

import json
import logging
import os
import pathlib
import pwd
from typing import Callable, Optional

from plugins.claude_code.permission_server import PermissionServer
from plugins.claude_code import cards

logger = logging.getLogger(__name__)

# 默认配置
_DEFAULT_PERM_PORT = 9876       # 权限确认 HTTP 服务器默认端口
_DEFAULT_PERM_TIMEOUT = 120     # 用户确认超时（秒）
_ASK_USER_TIMEOUT = 300         # AskUserQuestion 用户响应超时（秒）

import threading


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
    """

    def __init__(
        self,
        data_dir: pathlib.Path,
        load_config: Callable[[], dict],
        get_state: Callable[[str], dict],
        send_card: Callable[[str, str], None],
        send_card_get_id: Callable[[str, str], Optional[str]],
    ):
        self._data_dir = data_dir
        self._load_config = load_config
        self._get_state = get_state
        self._send_card = send_card
        self._send_card_get_id = send_card_get_id

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

    def ensure_server(self) -> None:
        """确保权限确认服务器已启动（懒初始化，线程安全）"""
        if self._started:
            return

        with self._lock:
            # 双重检查：加锁后再次确认，防止并发重复初始化
            if self._started:
                return

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
            except Exception as e:
                # 仅 start()（端口绑定）失败时删除端口文件，让 hook 降级为自动放行
                # 避免 hook 脚本向无效端口发请求后等待 curl 超时（每次工具调用卡 180s）
                logger.error("[CC] 权限确认服务器启动失败: %s", e, exc_info=True)
                self._delete_port_file()
                return

            # start() 成功后才更新状态——_setup_hook 等后续步骤的失败不应回滚端口文件
            self._server = perm_server
            self._started = True
            self._write_port_file(port)
            self._setup_hook()

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
        state = self._get_state(user_id)
        perm_mode = state.get("session_perm_mode", "interactive")
        effective_working_dir = state["working_dir"]  # 始终为有效绝对路径
        logger.info(
            "[CC] 权限请求: user=%s, tool=%s, perm_mode=%s, request=%s, input=%s",
            user_id, tool_name, perm_mode, request_id[:8], input_summary.replace("\n", "↵"),
        )

        # bypass 模式：直接放行所有权限请求（AskUserQuestion 除外，它是用户输入而非权限确认）
        if perm_mode == "bypass" and tool_name != "AskUserQuestion":
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
                state = self._get_state(user_id)
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
        state = self._get_state(user_id)
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
        """删除端口文件和超时文件，让 hook 脚本不再尝试连接（降级为自动放行）"""
        port_file = self._data_dir / ".feishu_perm_port"
        timeout_file = self._data_dir / ".feishu_perm_timeout"
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
