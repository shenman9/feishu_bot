"""
权限确认服务器及插件集成单元测试
"""

import json
import threading
import time
import urllib.request
import urllib.error
from unittest.mock import MagicMock, Mock, patch

import pytest

from plugins.claude_code.permission_server import (
    PermissionServer,
    _make_hook_response,
)
from plugins.claude_code.claude_code_plugin import (
    ClaudeCodePlugin,
    PLUGIN_KEYWORD,
    _DEFAULT_TIMEOUT,
    _DEFAULT_MAX_OUTPUT,
    _DEFAULT_MAX_TURNS,
    _DEFAULT_PERM_PORT,
    _DEFAULT_PERM_TIMEOUT,
)


# ---- 辅助函数 ----


def _post_json(port: int, path: str, data: dict, timeout: float = 5) -> dict:
    """向本地 HTTP 服务器发送 POST JSON 请求"""
    url = f"http://127.0.0.1:{port}{path}"
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _find_free_port() -> int:
    """获取一个空闲端口"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---- Hook 响应构造测试 ----


class TestMakeHookResponse:
    """_make_hook_response 函数测试"""

    def test_allow_response(self):
        """构造允许决策"""
        resp = _make_hook_response("allow")
        assert resp["decision"] == "allow"
        assert "reason" not in resp

    def test_deny_response_with_message(self):
        """构造拒绝决策带消息"""
        resp = _make_hook_response("deny", "不允许此操作")
        assert resp["decision"] == "deny"
        assert resp["reason"] == "不允许此操作"

    def test_response_structure(self):
        """响应结构包含 decision 字段"""
        resp = _make_hook_response("allow")
        assert "decision" in resp


# ---- PermissionServer 基础测试 ----


class TestPermissionServerBasic:
    """权限服务器基础功能测试"""

    def test_session_register_and_lookup(self):
        """注册和查找 session 映射"""
        callback = MagicMock()
        server = PermissionServer(port=0, timeout=10, on_permission_request=callback)

        server.register_session("sess-1", "user-1", "chat-1")
        assert server.get_session_user("sess-1") == ("user-1", "chat-1")

    def test_session_unregister(self):
        """移除 session 映射"""
        callback = MagicMock()
        server = PermissionServer(port=0, timeout=10, on_permission_request=callback)

        server.register_session("sess-1", "user-1", "chat-1")
        server.unregister_session("sess-1")
        assert server.get_session_user("sess-1") is None

    def test_unknown_session_returns_none(self):
        """查找不存在的 session 返回 None"""
        callback = MagicMock()
        server = PermissionServer(port=0, timeout=10, on_permission_request=callback)

        assert server.get_session_user("nonexistent") is None

    def test_resolve_nonexistent_request(self):
        """解决不存在的请求返回 False"""
        callback = MagicMock()
        server = PermissionServer(port=0, timeout=10, on_permission_request=callback)

        assert server.resolve_request("nonexistent", "allow") is False

    def test_pending_count_initial(self):
        """初始时无挂起请求"""
        callback = MagicMock()
        server = PermissionServer(port=0, timeout=10, on_permission_request=callback)

        assert server.pending_count == 0


# ---- PermissionServer HTTP 集成测试 ----


class TestPermissionServerHTTP:
    """权限服务器 HTTP 集成测试"""

    @pytest.fixture(autouse=True)
    def setup_server(self):
        """每个测试启动独立的服务器"""
        self.port = _find_free_port()
        self.callback = MagicMock()
        self.server = PermissionServer(
            port=self.port,
            timeout=5,
            on_permission_request=self.callback,
        )
        self.server.start()
        yield
        self.server.stop()

    def test_unknown_session_allowed(self):
        """未注册的 session（CLI 直接调用）自动放行"""
        data = {
            "session_id": "unknown-session",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
        }
        resp = _post_json(self.port, "/permission-request", data)
        assert resp["decision"] == "allow"

    def test_allow_flow(self):
        """完整的允许流程：注册 session → 发请求 → 用户允许"""
        self.server.register_session("sess-abc", "user-1", "chat-1")

        # 在回调中自动允许（模拟用户点击）
        def auto_allow(**kwargs):
            request_id = kwargs["request_id"]
            # 在另一个线程中稍后 resolve
            threading.Timer(
                0.1, self.server.resolve_request, args=(request_id, "allow")
            ).start()

        self.callback.side_effect = auto_allow

        data = {
            "session_id": "sess-abc",
            "tool_name": "Bash",
            "tool_input": {"command": "echo hello"},
        }
        resp = _post_json(self.port, "/permission-request", data, timeout=10)
        assert resp["decision"] == "allow"

    def test_deny_flow(self):
        """完整的拒绝流程"""
        self.server.register_session("sess-def", "user-2", "chat-2")

        def auto_deny(**kwargs):
            request_id = kwargs["request_id"]
            threading.Timer(
                0.1, self.server.resolve_request, args=(request_id, "deny")
            ).start()

        self.callback.side_effect = auto_deny

        data = {
            "session_id": "sess-def",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /"},
        }
        resp = _post_json(self.port, "/permission-request", data, timeout=10)
        assert resp["decision"] == "deny"

    def test_timeout_auto_deny(self):
        """超时自动拒绝"""
        # 使用短超时的服务器
        self.server.stop()
        self.server = PermissionServer(
            port=self.port,
            timeout=1,  # 1 秒超时
            on_permission_request=self.callback,
        )
        self.server.start()
        self.server.register_session("sess-timeout", "user-3", "chat-3")

        # 回调不做任何操作（模拟用户未响应）
        self.callback.side_effect = lambda **kwargs: None

        data = {
            "session_id": "sess-timeout",
            "tool_name": "Bash",
            "tool_input": {"command": "test"},
        }
        resp = _post_json(self.port, "/permission-request", data, timeout=10)
        assert resp["decision"] == "deny"
        assert "未在" in resp.get("reason", "")

    def test_callback_receives_correct_params(self):
        """回调函数接收正确的参数"""
        self.server.register_session("sess-params", "user-p", "chat-p")

        def capture_and_resolve(**kwargs):
            self.captured_kwargs = kwargs
            threading.Timer(
                0.1, self.server.resolve_request, args=(kwargs["request_id"], "allow")
            ).start()

        self.callback.side_effect = capture_and_resolve

        data = {
            "session_id": "sess-params",
            "tool_name": "Edit",
            "tool_input": {"file_path": "/tmp/test.py"},
        }
        _post_json(self.port, "/permission-request", data, timeout=10)

        assert self.captured_kwargs["user_id"] == "user-p"
        assert self.captured_kwargs["chat_id"] == "chat-p"
        assert self.captured_kwargs["tool_name"] == "Edit"
        assert self.captured_kwargs["tool_input"]["file_path"] == "/tmp/test.py"
        assert "request_id" in self.captured_kwargs

    def test_invalid_path_returns_404(self):
        """访问无效路径返回 404"""
        try:
            _post_json(self.port, "/invalid", {})
            pytest.fail("应该抛出 HTTPError")
        except urllib.error.HTTPError as e:
            assert e.code == 404

    def test_empty_body_denied(self):
        """空请求体返回拒绝"""
        url = f"http://127.0.0.1:{self.port}/permission-request"
        req = urllib.request.Request(
            url,
            data=b"",
            headers={"Content-Type": "application/json", "Content-Length": "0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
        assert result["decision"] == "deny"


# ---- 权限确认卡片测试 ----


class TestPermissionCard:
    """权限确认卡片构建测试"""

    def test_bash_command_card(self):
        """Bash 命令显示为代码块"""
        card_json = ClaudeCodePlugin._build_permission_card(
            "req-1", "Bash", {"command": "rm -rf /tmp/test"}
        )
        card = json.loads(card_json)
        assert card["header"]["template"] == "orange"
        assert "权限确认" in card["header"]["title"]["content"]
        content = card["elements"][0]["content"]
        assert "Bash" in content
        assert "rm -rf /tmp/test" in content

    def test_edit_file_card(self):
        """Edit 工具显示文件路径"""
        card_json = ClaudeCodePlugin._build_permission_card(
            "req-2", "Edit", {"file_path": "/tmp/test.py"}
        )
        card = json.loads(card_json)
        content = card["elements"][0]["content"]
        assert "Edit" in content
        assert "/tmp/test.py" in content

    def test_write_file_card(self):
        """Write 工具显示文件路径"""
        card_json = ClaudeCodePlugin._build_permission_card(
            "req-3", "Write", {"file_path": "/tmp/new.py"}
        )
        card = json.loads(card_json)
        content = card["elements"][0]["content"]
        assert "Write" in content
        assert "/tmp/new.py" in content

    def test_generic_tool_card(self):
        """通用工具以 JSON 展示"""
        card_json = ClaudeCodePlugin._build_permission_card(
            "req-4", "WebFetch", {"url": "http://example.com"}
        )
        card = json.loads(card_json)
        content = card["elements"][0]["content"]
        assert "WebFetch" in content
        assert "example.com" in content

    def test_card_has_allow_deny_buttons(self):
        """卡片包含允许、拒绝和全部放行按钮"""
        card_json = ClaudeCodePlugin._build_permission_card(
            "req-5", "Bash", {"command": "ls"}
        )
        card = json.loads(card_json)
        actions = [e for e in card["elements"] if e.get("tag") == "action"]
        assert len(actions) == 1
        buttons = actions[0]["actions"]
        assert len(buttons) == 3

        allow_btn = buttons[0]
        assert allow_btn["value"]["action"] == "perm_allow"
        assert allow_btn["value"]["request_id"] == "req-5"

        deny_btn = buttons[1]
        assert deny_btn["value"]["action"] == "perm_deny"
        assert deny_btn["value"]["request_id"] == "req-5"

        bypass_btn = buttons[2]
        assert bypass_btn["value"]["action"] == "perm_bypass"
        assert bypass_btn["value"]["request_id"] == "req-5"

    def test_handled_card(self):
        """已处理卡片为灰色模板"""
        card_json = ClaudeCodePlugin._build_permission_handled_card("允许")
        card = json.loads(card_json)
        assert card["header"]["template"] == "grey"
        assert "已允许" in card["header"]["title"]["content"]


# ---- 插件权限集成测试 ----


class TestPluginPermissionIntegration:
    """插件与权限服务器的集成测试"""

    @pytest.fixture
    def plugin(self, mock_bot):
        """返回已注册 mock bot 的 ClaudeCodePlugin，配置为 default 权限模式"""
        p = ClaudeCodePlugin()
        p.on_register(mock_bot)
        p._config = {
            "claude_path": "/usr/bin/claude",
            "default_working_dir": "",
            "timeout": _DEFAULT_TIMEOUT,
            "max_output_chars": _DEFAULT_MAX_OUTPUT,
            "default_perm_mode": "interactive",
            "max_turns": _DEFAULT_MAX_TURNS,
            "run_as_user": "",
            "permission_server_port": _DEFAULT_PERM_PORT,
            "permission_timeout": _DEFAULT_PERM_TIMEOUT,
        }
        return p

    @pytest.fixture
    def bypass_plugin(self, mock_bot):
        """返回使用 bypassPermissions 模式的插件"""
        p = ClaudeCodePlugin()
        p.on_register(mock_bot)
        p._config = {
            "claude_path": "/usr/bin/claude",
            "default_working_dir": "",
            "timeout": _DEFAULT_TIMEOUT,
            "max_output_chars": _DEFAULT_MAX_OUTPUT,
            "default_perm_mode": "bypass",
            "max_turns": _DEFAULT_MAX_TURNS,
            "run_as_user": "",
            "permission_server_port": _DEFAULT_PERM_PORT,
            "permission_timeout": _DEFAULT_PERM_TIMEOUT,
        }
        return p

    def test_needs_permission_server_always_true(self, plugin):
        """权限服务器始终需要启动（默认启用交互确认）"""
        assert plugin._needs_permission_server() is True

    def test_needs_permission_server_bypass_mode_also_true(self, bypass_plugin):
        """bypassPermissions 配置下权限服务器同样需要启动"""
        assert bypass_plugin._needs_permission_server() is True

    def test_perm_allow_card_action(self, plugin):
        """权限允许按钮点击"""
        # 手动设置权限服务器 mock
        plugin._perm_server = MagicMock()
        plugin._perm_server.resolve_request.return_value = True

        result = plugin.handle_card_action(
            "u1", "c1", "msg1",
            {"action": "perm_allow", "plugin": "CC", "request_id": "req-123"}
        )

        plugin._perm_server.resolve_request.assert_called_once_with("req-123", "allow")

    def test_perm_deny_card_action(self, plugin):
        """权限拒绝按钮点击"""
        plugin._perm_server = MagicMock()
        plugin._perm_server.resolve_request.return_value = True

        result = plugin.handle_card_action(
            "u1", "c1", "msg1",
            {"action": "perm_deny", "plugin": "CC", "request_id": "req-456"}
        )

        plugin._perm_server.resolve_request.assert_called_once_with("req-456", "deny")

    def test_expired_request_card_action(self, plugin):
        """过期请求的按钮点击"""
        plugin._perm_server = MagicMock()
        plugin._perm_server.resolve_request.return_value = False

        result = plugin.handle_card_action(
            "u1", "c1", "msg1",
            {"action": "perm_allow", "plugin": "CC", "request_id": "expired"}
        )

        # resolve 返回 False，应该提示已过期
        plugin._perm_server.resolve_request.assert_called_once()

    def test_no_perm_server_card_action(self, plugin):
        """权限服务器未启动时的按钮点击"""
        plugin._perm_server = None

        result = plugin.handle_card_action(
            "u1", "c1", "msg1",
            {"action": "perm_allow", "plugin": "CC", "request_id": "req-789"}
        )
        # 不应崩溃
        assert result is not None

    def test_bypass_mode_auto_allows(self, plugin):
        """会话免确认模式下权限请求自动放行，不发飞书卡片"""
        plugin._perm_server = MagicMock()
        plugin.handle_message("u1", "c1", "CC")
        plugin._get_state("u1")["session_perm_mode"] = "bypass"

        plugin._on_permission_request(
            user_id="u1",
            chat_id="c1",
            request_id="req-auto",
            tool_name="Bash",
            tool_input={"command": "ls"},
        )

        # 自动放行，不发送飞书卡片
        plugin.bot.send_message.assert_not_called()
        plugin._perm_server.resolve_request.assert_called_once_with("req-auto", "allow")

    def test_interactive_mode_sends_card(self, plugin):
        """交互确认模式下权限请求发送飞书卡片"""
        plugin._perm_server = MagicMock()
        plugin.handle_message("u1", "c1", "CC")
        assert plugin._get_state("u1").get("session_perm_mode") == "interactive"

        plugin._on_permission_request(
            user_id="u1",
            chat_id="c1",
            request_id="req-card",
            tool_name="Bash",
            tool_input={"command": "rm -rf /tmp/test"},
        )

        # 应发送飞书卡片
        plugin.bot.send_message.assert_called_once()
        # 自动放行不应被调用
        plugin._perm_server.resolve_request.assert_not_called()

