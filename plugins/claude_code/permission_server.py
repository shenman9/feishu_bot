"""
权限确认 HTTP 服务器

在 Claude Code CLI 使用 PreToolUse Hook 时，作为 Hook 脚本与飞书机器人之间的 IPC 桥梁。

流程:
1. Hook 脚本被 Claude Code PreToolUse 事件触发，通过 curl 将权限请求 POST 到本服务器
2. 服务器通过 session_id 反查用户，调用回调函数发送飞书权限确认卡片
3. 用户在飞书点击「允许」或「拒绝」按钮
4. 按钮回调通过 resolve_request() 解除阻塞
5. HTTP 响应返回 {"decision": "allow/deny", "reason": "..."} JSON
6. Hook 脚本根据 decision 字段决定 exit 0（允许）或 exit 2（拒绝）
"""

import json
import logging
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

logger = logging.getLogger(__name__)

def _make_hook_response(decision: str, reason: str = "") -> dict:
    """构造返回给 Hook 脚本的响应 JSON

    Hook 脚本根据 decision 字段决定 exit 0（allow）或 exit 2（deny）。
    reason 字段在拒绝时作为错误消息输出到 stderr。
    """
    resp: dict = {"decision": decision}
    if reason:
        resp["reason"] = reason
    return resp


class _PermissionRequestHandler(BaseHTTPRequestHandler):
    """处理来自 Hook 脚本的权限请求"""

    # 引用外层 PermissionServer 实例
    server: "PermissionHTTPServer"

    def do_POST(self):
        if self.path != "/permission-request":
            self.send_error(404)
            return

        # 读取请求体
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._respond_json(200, _make_hook_response("deny", "请求体为空"))
            return

        body = self.rfile.read(content_length)
        try:
            request_data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self._respond_json(200, _make_hook_response("deny", "无效 JSON"))
            return

        # 提取关键字段
        session_id = request_data.get("session_id", "")
        tool_name = request_data.get("tool_name", "")
        tool_input = request_data.get("tool_input", {})

        logger.debug(
            "[权限服务器] 请求体: session=%s, tool=%s, input=%s",
            session_id[:8] if session_id else "?",
            tool_name,
            str(tool_input)[:200],
        )

        perm_server: PermissionServer = self.server.perm_server

        # 通过 session_id 反查用户
        user_info = perm_server.get_session_user(session_id)
        if not user_info:
            # 未注册的 session 说明是 CLI 直接调用（非飞书 Bot 发起），直接放行。
            # 飞书 Bot 发起的子进程在启动前必定已调用 register_session()。
            logger.info(
                "[权限服务器] 未知 session_id=%s, 非飞书会话自动放行 (已注册会话: %s)",
                session_id[:8],
                [s[:8] for s in perm_server._session_map.keys()],
            )
            self._respond_json(200, _make_hook_response("allow"))
            return

        user_id, chat_id = user_info

        # 生成请求 ID，创建等待事件
        request_id = str(uuid.uuid4())
        event = threading.Event()
        perm_server._pending_requests[request_id] = {
            "event": event,
            "decision": None,
            "session_id": session_id,
            "user_id": user_id,
        }

        logger.info(
            "[权限服务器] 收到权限请求: request_id=%s, session=%s, user=%s, "
            "tool=%s",
            request_id[:8], session_id[:8], user_id, tool_name,
        )

        # 回调通知插件发送飞书卡片
        try:
            perm_server._on_permission_request(
                user_id=user_id,
                chat_id=chat_id,
                request_id=request_id,
                tool_name=tool_name,
                tool_input=tool_input,
            )
        except Exception as e:
            logger.error("[权限服务器] 发送飞书卡片失败: %s", e, exc_info=True)
            perm_server._pending_requests.pop(request_id, None)
            self._respond_json(200, _make_hook_response("deny", "通知失败"))
            return

        # 阻塞等待用户响应
        timeout = perm_server._timeout
        wait_start = time.monotonic()
        logger.info("[权限服务器] 等待用户响应: request_id=%s, timeout=%ds", request_id[:8], timeout)
        responded = event.wait(timeout=timeout)
        elapsed = time.monotonic() - wait_start
        # 获取决策并清理
        pending = perm_server._pending_requests.pop(request_id, None)
        if not responded or pending is None or pending["decision"] is None:
            logger.warning(
                "[权限服务器] 等待结束: request_id=%s, decision=timeout, 耗时=%.1fs",
                request_id[:8], elapsed,
            )
            # 通知插件发生了权限超时
            if perm_server._on_permission_timeout:
                try:
                    perm_server._on_permission_timeout(
                        user_id=user_id,
                        chat_id=chat_id,
                        request_id=request_id,
                        tool_name=tool_name,
                        timeout=timeout,
                    )
                except Exception:
                    logger.debug("[权限服务器] 超时回调执行失败", exc_info=True)
            self._respond_json(
                200, _make_hook_response("deny", f"用户未在 {timeout}s 内响应，自动拒绝")
            )
            return

        behavior = pending["decision"]
        logger.info(
            "[权限服务器] 等待结束: request_id=%s, decision=%s, 耗时=%.1fs",
            request_id[:8], behavior, elapsed,
        )
        resp = _make_hook_response(behavior)
        logger.debug("[权限服务器] 返回响应: %s", resp)
        self._respond_json(200, resp)

    def _respond_json(self, status: int, data: dict) -> None:
        """发送 JSON 响应"""
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        """覆盖默认日志，使用 logging 模块"""
        logger.debug("[权限服务器 HTTP] %s", format % args)


class PermissionHTTPServer(ThreadingHTTPServer):
    """多线程 HTTP 服务器，每个请求在独立线程处理

    继承 ThreadingHTTPServer 确保一个请求的 event.wait() 阻塞不影响
    后续请求的接入。HTTPServer（单线程）下，首个未响应的请求会阻塞
    整个服务器，导致后续所有权限请求因连接超时被 hook 脚本绕过。
    """

    daemon_threads = True  # handler 线程随主线程退出

    def __init__(self, server_address, handler_class, perm_server: "PermissionServer"):
        self.perm_server = perm_server
        super().__init__(server_address, handler_class)


class PermissionServer:
    """权限确认服务器

    管理 session 映射、待处理请求，并运行 HTTP 服务器等待 Hook 脚本连接。

    Args:
        port: HTTP 服务器监听端口
        timeout: 用户响应超时（秒）
        on_permission_request: 回调函数，签名:
            (user_id, chat_id, request_id, tool_name, tool_input) -> None
        on_permission_timeout: 超时回调函数（可选），签名:
            (user_id, chat_id, request_id, tool_name, timeout) -> None
    """

    def __init__(
        self,
        port: int,
        timeout: int,
        on_permission_request: Callable,
        on_permission_timeout: Optional[Callable] = None,
    ):
        self._port = port
        self._timeout = timeout
        self._on_permission_request = on_permission_request
        self._on_permission_timeout = on_permission_timeout

        # session_id -> (user_id, chat_id)
        self._session_map: dict[str, tuple[str, str]] = {}
        # request_id -> {"event": Event, "decision": str|None, ...}
        self._pending_requests: dict[str, dict] = {}

        self._server: Optional[PermissionHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """启动 HTTP 服务器（后台线程）"""
        self._server = PermissionHTTPServer(
            ("127.0.0.1", self._port),
            _PermissionRequestHandler,
            perm_server=self,
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
        )
        self._thread.start()
        logger.info("[权限服务器] 已启动: port=%d, timeout=%ds", self._port, self._timeout)

    def stop(self) -> None:
        """停止 HTTP 服务器"""
        if self._server:
            self._server.shutdown()
            self._server = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("[权限服务器] 已停止")

    # ---- Session 映射 ----

    def register_session(self, session_id: str, user_id: str, chat_id: str) -> None:
        """注册 session_id 到 user_id/chat_id 的映射"""
        self._session_map[session_id] = (user_id, chat_id)
        logger.debug(
            "[权限服务器] 注册会话: session=%s, user=%s", session_id[:8], user_id
        )

    def unregister_session(self, session_id: str) -> None:
        """移除 session 映射，并自动拒绝该会话所有未决请求

        任务结束时调用。未决请求可能因 hook 端 curl 提前超时（放行/拒绝后
        Claude Code 继续执行）而成为孤儿，若不清理会在服务器超时后触发迟到
        的回调，干扰后续任务的 perm_timeout_count 统计。
        """
        self._session_map.pop(session_id, None)
        # 清理该会话所有未决请求：设置 deny 决策并唤醒阻塞的 handler 线程
        orphaned = [
            (rid, pending)
            for rid, pending in self._pending_requests.items()
            if pending.get("session_id") == session_id
        ]
        for rid, pending in orphaned:
            pending["decision"] = "deny"
            pending["event"].set()
            logger.debug("[权限服务器] 清理遗留请求: request=%s, session=%s", rid[:8], session_id[:8])
        logger.debug("[权限服务器] 移除会话: session=%s, 清理遗留请求=%d", session_id[:8], len(orphaned))

    def get_session_user(self, session_id: str) -> Optional[tuple[str, str]]:
        """查询 session 对应的 (user_id, chat_id)"""
        return self._session_map.get(session_id)

    # ---- 请求响应 ----

    def resolve_request(self, request_id: str, behavior: str) -> bool:
        """解决挂起的权限请求

        Args:
            request_id: 请求唯一 ID
            behavior: "allow" 或 "deny"

        Returns:
            True 表示成功设置，False 表示请求不存在或已超时
        """
        pending = self._pending_requests.get(request_id)
        if pending is None:
            logger.warning(
                "[权限服务器] 请求不存在或已超时: request_id=%s", request_id[:8]
            )
            return False
        pending["decision"] = behavior
        pending["event"].set()
        return True

    def has_pending_request(self, request_id: str) -> bool:
        """检查是否有指定的挂起请求"""
        return request_id in self._pending_requests

    @property
    def pending_count(self) -> int:
        """当前挂起的权限请求数量"""
        return len(self._pending_requests)
