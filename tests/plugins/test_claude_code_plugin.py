"""
Claude Code 桥接插件单元测试
"""

import io
import json
import subprocess
from unittest.mock import MagicMock, Mock, patch, PropertyMock

import pytest

from plugins.claude_code.claude_code_plugin import (
    ClaudeCodePlugin,
    _DEFAULT_TIMEOUT,
    _DEFAULT_MAX_TURNS,
)
from plugins.claude_code.constants import PLUGIN_KEYWORD
from plugins.claude_code.stream_parser import (
    DEFAULT_MAX_OUTPUT as _DEFAULT_MAX_OUTPUT,
    format_tool_call,
    parse_stream_line,
    render_log,
    assemble_card_text,
    _MAX_STREAK_DISPLAY,
    _TOOL_PARAM_MAX,
)
from plugins.claude_code.cards import build_execution_card
from plugins.claude_code.permission_manager import _DEFAULT_PERM_PORT, _DEFAULT_PERM_TIMEOUT


# ---- 辅助函数 ----


def _mock_popen(stdout_lines, returncode=0, stderr=""):
    """构造 subprocess.Popen 的 mock

    Args:
        stdout_lines: stdout 每行输出（不含换行符）
        returncode: 进程退出码
        stderr: stderr 输出内容
    """
    proc = Mock()
    # 使用 io.StringIO 模拟支持 readline() 的标准输出流
    # select.select 对 StringIO 会抛 TypeError，被生产代码的降级逻辑捕获后直接 readline()
    content = "\n".join(stdout_lines) + ("\n" if stdout_lines else "")
    proc.stdout = io.StringIO(content)
    proc.stderr = Mock()
    proc.stderr.read = Mock(return_value=stderr)
    proc.poll = Mock(return_value=returncode)
    proc.wait = Mock(return_value=returncode)
    proc.returncode = returncode
    proc.terminate = Mock()
    proc.kill = Mock()
    return proc


def _make_assistant_event(text):
    """构造 assistant 类型的 stream-json 行"""
    return json.dumps({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    })


def _make_tool_use_event(tool_name, tool_input, tool_use_id="toolu_001"):
    """构造包含 tool_use block 的 assistant stream-json 行"""
    return json.dumps({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_use_id, "name": tool_name, "input": tool_input}],
        },
    })


def _make_thinking_event():
    """构造包含 thinking block 的 assistant stream-json 行"""
    return json.dumps({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": "let me think..."}],
        },
    })


def _make_tool_result_event(tool_use_id="toolu_001", content="ok", is_error=False):
    """构造工具执行结果的 user stream-json 行"""
    return json.dumps({
        "type": "user",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": content,
                "is_error": is_error,
            }],
        },
    })


def _make_result_event(result_text="", cost=0.01, duration=2000, turns=1, session_id="test-session"):
    """构造 result 类型的 stream-json 行"""
    return json.dumps({
        "type": "result",
        "subtype": "success",
        "result": result_text,
        "cost_usd": cost,
        "duration_ms": duration,
        "num_turns": turns,
        "session_id": session_id,
    })


# ---- Fixtures ----


@pytest.fixture
def plugin(mock_bot):
    """返回已注册 mock bot 的 ClaudeCodePlugin，并注入测试配置"""
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
        "default_model": "",
    }
    # 跳过真实 HTTP 服务器启动，避免端口冲突和测试副作用
    perm_mgr = p._ensure_perm_manager()
    perm_mgr._started = True
    return p


# ---- 元信息测试 ----


class TestPluginProperties:
    """插件元信息测试"""

    def test_name(self, plugin):
        """插件名称正确"""
        assert plugin.name == "Claude Code"

    def test_keyword(self, plugin):
        """插件关键词正确"""
        assert plugin.keyword == PLUGIN_KEYWORD

    def test_description(self, plugin):
        """插件描述非空"""
        assert plugin.description


# ---- 激活与退出测试 ----


class TestActivation:
    """插件激活与退出测试"""

    def test_keyword_activates(self, plugin):
        """发送关键词激活插件"""
        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        assert plugin.is_user_active("u1") is True
        msg = plugin.bot.reply.call_args[0][1]
        assert "已激活" in msg

    def test_activation_shows_session_info(self, plugin):
        """激活时显示会话和目录信息"""
        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        msg = plugin.bot.reply.call_args[0][1]
        assert "会话" in msg
        assert "工作目录" in msg

    def test_deactivate_clears_state(self, plugin):
        """deactivate 清理用户全部状态"""
        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        plugin.deactivate_user("u1")
        assert "u1" not in plugin.user_states

    def test_deactivate_kills_running_process(self, plugin):
        """deactivate 终止运行中的进程"""
        proc = Mock()
        proc.poll = Mock(return_value=None)
        proc.terminate = Mock()
        proc.wait = Mock()
        plugin._running_processes["u1"] = proc

        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        plugin.deactivate_user("u1")

        proc.terminate.assert_called_once()

    def test_inactive_user_returns_false(self, plugin):
        """未激活用户 is_user_active 返回 False"""
        assert plugin.is_user_active("unknown") is False


# ---- 用户指令测试 ----


class TestUserCommands:
    """用户特殊指令测试"""

    def test_new_session(self, plugin):
        """「新会话」重置 session_id 和 session_started"""
        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        old_session = plugin._get_state("u1")["session_id"]
        # 模拟已完成过一次调用
        plugin._get_state("u1")["session_started"] = True

        plugin.handle_message("u1", "c1", "/new")
        state = plugin._get_state("u1")

        assert state["session_id"] != old_session
        assert state["session_started"] is False
        msg = plugin.bot.reply.call_args[0][1]
        assert "已重置" in msg

    def test_cancel_when_running(self, plugin):
        """运行中「取消」终止进程"""
        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        state = plugin._get_state("u1")
        state["running"] = True
        proc = Mock()
        proc.poll = Mock(return_value=None)
        proc.terminate = Mock()
        proc.wait = Mock()
        plugin._running_processes["u1"] = proc

        plugin.handle_message("u1", "c1", "/cancel")

        proc.terminate.assert_called_once()
        assert state["running"] is False
        msg = plugin.bot.reply.call_args[0][1]
        assert "已取消" in msg

    def test_cancel_when_idle(self, plugin):
        """空闲时「取消」给出友好提示"""
        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        plugin.handle_message("u1", "c1", "/cancel")

        msg = plugin.bot.reply.call_args[0][1]
        assert "没有运行中" in msg

    def test_status(self, plugin):
        """「状态」显示当前信息"""
        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        plugin.handle_message("u1", "c1", "/status")

        msg = plugin.bot.reply.call_args[0][1]
        assert "会话" in msg
        assert "状态" in msg

    def test_switch_dir_valid(self, plugin):
        """「切换目录」到有效目录"""
        with patch("plugins.claude_code.claude_code_plugin.os.path.isdir", return_value=True):
            plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
            plugin.handle_message("u1", "c1", "/cd /tmp")

        state = plugin._get_state("u1")
        assert state["working_dir"] == "/tmp"
        msg = plugin.bot.reply.call_args[0][1]
        assert "已切换" in msg

    def test_switch_dir_invalid(self, plugin):
        """「切换目录」到不存在的目录"""
        with patch("plugins.claude_code.claude_code_plugin.os.path.isdir", return_value=False):
            plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
            plugin.handle_message("u1", "c1", "/cd /nonexistent")

        msg = plugin.bot.reply.call_args[0][1]
        assert "不存在" in msg


# ---- 并发控制测试 ----


class TestConcurrency:
    """并发控制测试"""

    def test_reject_when_running(self, plugin):
        """运行中拒绝新任务"""
        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        state = plugin._get_state("u1")
        state["running"] = True

        plugin.bot.reply.reset_mock()
        plugin.handle_message("u1", "c1", "新的 prompt")

        msg = plugin.bot.reply.call_args[0][1]
        assert "运行中" in msg


# ---- stream-json 解析测试 ----


class TestParseStreamLine:
    """stream-json 解析测试"""

    def test_assistant_event(self):
        """正确提取 assistant 事件中的文本"""
        line = _make_assistant_event("Hello World!")
        text, log_actions, meta = parse_stream_line(line, False)
        assert text == "Hello World!"
        assert log_actions == []
        assert meta == ""

    def test_assistant_event_with_previous(self):
        """有前序文本时 assistant 事件直接返回文本，分隔符由主循环负责"""
        line = _make_assistant_event("第二段")
        text, log_actions, meta = parse_stream_line(line, True)
        assert text == "第二段"
        assert log_actions == []

    def test_result_event_meta(self):
        """result 事件提取费用/耗时信息"""
        line = _make_result_event(cost=0.05, duration=5000, turns=3)
        text, log_actions, meta = parse_stream_line(line, True)
        assert text == ""  # 有前序文本时不重复追加
        assert log_actions == []
        assert "$0.0500" in meta
        assert "5.0s" in meta
        assert "3" in meta

    def test_result_event_text_fallback(self):
        """无前序 assistant 文本时用 result 的文本"""
        line = _make_result_event(result_text="最终结果")
        text, log_actions, meta = parse_stream_line(line, False)
        assert text == "最终结果"
        assert log_actions == []

    def test_invalid_json(self):
        """无效 JSON 返回空"""
        text, log_actions, meta = parse_stream_line("not json", False)
        assert text == ""
        assert log_actions == []
        assert meta == ""

    def test_unknown_type(self):
        """未知事件类型返回空"""
        line = json.dumps({"type": "system", "message": "info"})
        text, log_actions, meta = parse_stream_line(line, False)
        assert text == ""
        assert log_actions == []
        assert meta == ""

    def test_tool_use_event(self):
        """assistant 事件中的 tool_use block 生成 add 日志动作"""
        line = _make_tool_use_event("Edit", {"file_path": "src/main.py"}, "toolu_001")
        text, log_actions, meta = parse_stream_line(line, False)
        assert text == ""
        assert len(log_actions) == 1
        action = log_actions[0]
        assert action["action"] == "add"
        assert action["tool_use_id"] == "toolu_001"
        assert "Edit" in action["line"]
        assert "src/main.py" in action["line"]

    def test_thinking_event(self):
        """assistant 事件中的 thinking block 生成思考日志动作"""
        line = _make_thinking_event()
        text, log_actions, meta = parse_stream_line(line, False)
        assert text == ""
        assert len(log_actions) == 1
        assert log_actions[0]["action"] == "add"
        assert "💭" in log_actions[0]["line"]
        assert log_actions[0]["tool_use_id"] is None

    def test_tool_result_success(self):
        """user 事件中工具成功结果生成 result 日志动作"""
        line = _make_tool_result_event("toolu_001", content="file content", is_error=False)
        text, log_actions, meta = parse_stream_line(line, False)
        assert text == ""
        assert len(log_actions) == 1
        action = log_actions[0]
        assert action["action"] == "result"
        assert action["tool_use_id"] == "toolu_001"
        assert action["is_error"] is False
        assert action["summary"] == ""

    def test_tool_result_error(self):
        """user 事件中工具失败结果包含错误摘要"""
        line = _make_tool_result_event("toolu_002", content="Permission denied: /etc/passwd", is_error=True)
        text, log_actions, meta = parse_stream_line(line, False)
        assert len(log_actions) == 1
        action = log_actions[0]
        assert action["action"] == "result"
        assert action["is_error"] is True
        assert "Permission denied" in action["summary"]

    def test_tool_result_array_content(self):
        """工具结果 content 为数组时正确提取文本"""
        line = json.dumps({
            "type": "user",
            "message": {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "toolu_003",
                    "content": [{"type": "text", "text": "Error: not found"}],
                    "is_error": True,
                }],
            },
        })
        text, log_actions, meta = parse_stream_line(line, False)
        assert log_actions[0]["is_error"] is True
        assert "Error: not found" in log_actions[0]["summary"]


# ---- 工具调用日志渲染测试 ----


class TestRenderLog:
    """工具调用日志渲染测试"""

    def test_empty_log(self):
        """无任何日志时返回空字符串"""
        result = render_log([], [], running=False)
        assert result == ""

    def test_running_with_log_shows_spinner(self):
        """执行中且有日志时显示⏳"""
        streak = [{"line": "📖 Read `file.py`", "tool_use_id": "t1"}]
        result = render_log([], streak, running=True)
        assert "⏳" in result
        assert "📖" in result

    def test_running_without_log_shows_spinner(self):
        """执行中且无日志时也显示⏳（初始思考阶段）"""
        result = render_log([], [], running=True)
        assert "⏳" in result

    def test_streak_within_limit(self):
        """连续工具调用未超上限时全部显示"""
        streak = [{"line": f"📖 Read `file{i}.py`", "tool_use_id": f"t{i}"} for i in range(5)]
        result = render_log([], streak, running=False)
        for i in range(5):
            assert f"file{i}.py" in result
        assert "省略" not in result

    def test_streak_exceeds_limit_folds(self):
        """连续工具调用超过上限时折叠显示最近 N 条"""
        total = _MAX_STREAK_DISPLAY + 5
        streak = [{"line": f"📖 Read `file{i}.py`", "tool_use_id": f"t{i}"} for i in range(total)]
        result = render_log([], streak, running=False)
        assert "省略" in result
        assert "5 次" in result
        # 最后 _MAX_STREAK_DISPLAY 条应显示
        for i in range(5, total):
            assert f"file{i}.py" in result
        # 前 5 条不应显示
        for i in range(5):
            assert f"file{i}.py" not in result

    def test_segments_separated_by_blank_line(self):
        """不同段之间有空行分隔"""
        segments = [
            {"type": "tools", "entries": ["📖 Read `a.py`"]},
        ]
        streak = [{"line": "📝 Edit `b.py`", "tool_use_id": "t2"}]
        result = render_log(segments, streak, running=False)
        # 段间有空行
        assert "\n\n" in result or "\n \n" in result or result.count("\n") >= 2

    def test_interleaved_tools_and_text_order(self):
        """工具调用与文字段按实际执行顺序交错渲染（非两段式）"""
        segments = [
            {"type": "tools", "entries": ["📖 Read `a.py` → ✅"]},
            {"type": "text", "content": "分析结果：文件已读取"},
            {"type": "tools", "entries": ["💻 Bash `git commit` → ✅"]},
            {"type": "text", "content": "提交完成"},
        ]
        result = render_log(segments, [], running=False)
        idx_tool1 = result.index("Read")
        idx_text1 = result.index("分析结果")
        idx_tool2 = result.index("Bash")
        idx_text2 = result.index("提交完成")
        # 必须严格按 工具1 → 文字1 → 工具2 → 文字2 顺序排列
        assert idx_tool1 < idx_text1 < idx_tool2 < idx_text2

    def test_text_segment_rendered_inline(self):
        """文字段内联渲染，不被折叠或放到末尾"""
        segments = [
            {"type": "tools", "entries": ["📖 Read `x.py` → ✅"]},
            {"type": "text", "content": "这是中间文字"},
            {"type": "tools", "entries": ["💻 Bash `ls` → ✅"]},
        ]
        result = render_log(segments, [], running=False)
        assert "这是中间文字" in result
        # 文字夹在两段工具调用之间
        assert result.index("Read") < result.index("这是中间文字") < result.index("Bash")


class TestFormatToolCall:
    """工具调用格式化测试"""

    def test_read_tool(self):
        """Read 工具显示文件路径和图标"""
        line = format_tool_call("Read", {"file_path": "src/main.py"})
        assert "📖" in line
        assert "src/main.py" in line

    def test_bash_tool(self):
        """Bash 工具显示命令"""
        line = format_tool_call("Bash", {"command": "python -m pytest"})
        assert "💻" in line
        assert "python -m pytest" in line

    def test_long_param_truncated(self):
        """超长参数被截断"""
        long_path = "a" * (_TOOL_PARAM_MAX + 20)
        line = format_tool_call("Read", {"file_path": long_path})
        assert "..." in line
        assert len(line) < len(long_path) + 20

    def test_unknown_tool(self):
        """未知工具使用默认图标"""
        line = format_tool_call("CustomTool", {"key": "value"})
        assert "🔧" in line
        assert "CustomTool" in line

    def test_tool_no_params(self):
        """无参数工具只显示图标和名称"""
        line = format_tool_call("Bash", {})
        assert "💻" in line
        assert "Bash" in line


class TestAssembleCardText:
    """两段式卡片内容组装测试"""

    def test_both_log_and_text(self):
        """日志和文字都有时用分隔线连接"""
        result = assemble_card_text("log content", "reply text")
        assert "log content" in result
        assert "reply text" in result
        assert "---" in result

    def test_only_log(self):
        """只有日志时返回日志"""
        result = assemble_card_text("log content", "")
        assert result == "log content"

    def test_only_text(self):
        """只有文字时返回文字"""
        result = assemble_card_text("", "reply text")
        assert result == "reply text"

    def test_both_empty(self):
        """都为空时返回空字符串"""
        result = assemble_card_text("", "")
        assert result == ""


# ---- 卡片构建测试 ----


class TestCardBuilding:
    """飞书卡片构建测试"""

    def test_running_card_has_cancel_button(self):
        """执行中卡片包含取消按钮"""
        card = json.loads(build_execution_card("测试", running=True))
        assert card["header"]["template"] == "turquoise"
        assert "执行中" in card["header"]["title"]["content"]
        # 检查有 action 元素
        actions = [e for e in card["elements"] if e.get("tag") == "action"]
        assert len(actions) == 1
        assert actions[0]["actions"][0]["value"]["action"] == "cancel"

    def test_completed_card_no_cancel(self):
        """完成卡片不包含取消按钮"""
        card = json.loads(build_execution_card("测试", running=False))
        assert card["header"]["template"] == "blue"
        assert "执行中" not in card["header"]["title"]["content"]
        actions = [e for e in card["elements"] if e.get("tag") == "action"]
        assert len(actions) == 0

    def test_card_contains_text(self):
        """卡片正文包含指定文本"""
        card = json.loads(build_execution_card("hello world"))
        assert card["elements"][0]["content"] == "hello world"


# ---- 卡片按钮处理测试 ----


class TestCardAction:
    """卡片按钮点击测试"""

    def test_cancel_action_when_running(self, plugin):
        """运行中点击取消"""
        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        state = plugin._get_state("u1")
        state["running"] = True
        proc = Mock()
        proc.poll = Mock(return_value=None)
        proc.terminate = Mock()
        proc.wait = Mock()
        plugin._running_processes["u1"] = proc

        plugin.handle_card_action(
            "u1", "c1", "msg1",
            {"action": "cancel", "plugin": PLUGIN_KEYWORD}
        )

        proc.terminate.assert_called_once()
        assert state["running"] is False

    def test_cancel_action_when_idle(self, plugin):
        """空闲时点击取消"""
        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        result = plugin.handle_card_action(
            "u1", "c1", "msg1",
            {"action": "cancel", "plugin": PLUGIN_KEYWORD}
        )
        # 不应崩溃
        assert result is not None


# ---- 子进程执行测试 ----


class TestSubprocessExecution:
    """子进程执行测试"""

    @patch("plugins.claude_code.claude_code_plugin.subprocess.Popen")
    def test_normal_execution(self, mock_popen_cls, plugin):
        """正常执行：发送 prompt 后流式更新卡片"""
        stdout_lines = [
            _make_assistant_event("Hello!") + "\n",
            _make_result_event(cost=0.01, duration=2000, turns=1) + "\n",
        ]
        mock_popen_cls.return_value = _mock_popen(stdout_lines)

        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        plugin.handle_message("u1", "c1", "say hello")

        # 等待后台线程完成
        thread = plugin._running_threads.get("u1")
        if thread:
            thread.join(timeout=10)

        # 占位卡片已发送
        plugin.bot.send_message_get_id.assert_called()
        # patch_message 被调用（最终更新）
        plugin.bot.patch_message.assert_called()
        # 最终卡片包含输出文本
        last_card = json.loads(plugin.bot.patch_message.call_args[0][1])
        assert "Hello!" in last_card["elements"][0]["content"]
        # 运行状态已恢复
        assert plugin._get_state("u1")["running"] is False

    @patch("plugins.claude_code.claude_code_plugin.subprocess.Popen")
    def test_command_includes_session_id(self, mock_popen_cls, plugin):
        """首次调用命令行参数包含 --session-id"""
        mock_popen_cls.return_value = _mock_popen([
            _make_result_event() + "\n",
        ])

        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        session_id = plugin._get_state("u1")["session_id"]
        plugin.handle_message("u1", "c1", "test prompt")

        thread = plugin._running_threads.get("u1")
        if thread:
            thread.join(timeout=10)

        cmd = mock_popen_cls.call_args[0][0]
        assert "--session-id" in cmd
        assert session_id in cmd
        assert "--resume" not in cmd

    @patch("plugins.claude_code.claude_code_plugin.subprocess.Popen")
    def test_session_started_after_success(self, mock_popen_cls, plugin):
        """成功执行后 session_started 标记为 True"""
        mock_popen_cls.return_value = _mock_popen([
            _make_assistant_event("Hello!") + "\n",
            _make_result_event() + "\n",
        ])

        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        assert plugin._get_state("u1")["session_started"] is False

        plugin.handle_message("u1", "c1", "test")

        thread = plugin._running_threads.get("u1")
        if thread:
            thread.join(timeout=10)

        assert plugin._get_state("u1")["session_started"] is True

    @patch("plugins.claude_code.claude_code_plugin.subprocess.Popen")
    def test_second_call_uses_resume(self, mock_popen_cls, plugin):
        """第二次调用使用 --resume 而非 --session-id"""
        mock_popen_cls.return_value = _mock_popen([
            _make_assistant_event("Hello!") + "\n",
            _make_result_event() + "\n",
        ])

        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        plugin.handle_message("u1", "c1", "first prompt")

        thread = plugin._running_threads.get("u1")
        if thread:
            thread.join(timeout=10)

        # 重置 mock 以捕获第二次调用
        mock_popen_cls.reset_mock()
        mock_popen_cls.return_value = _mock_popen([
            _make_assistant_event("World!") + "\n",
            _make_result_event() + "\n",
        ])

        session_id = plugin._get_state("u1")["session_id"]
        plugin.handle_message("u1", "c1", "second prompt")

        thread = plugin._running_threads.get("u1")
        if thread:
            thread.join(timeout=10)

        cmd = mock_popen_cls.call_args[0][0]
        assert "--resume" in cmd
        assert session_id in cmd
        assert "--session-id" not in cmd

    @patch("plugins.claude_code.claude_code_plugin.subprocess.Popen")
    def test_empty_output(self, mock_popen_cls, plugin):
        """无输出时显示友好提示"""
        mock_popen_cls.return_value = _mock_popen([], stderr="some error")

        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        plugin.handle_message("u1", "c1", "test")

        thread = plugin._running_threads.get("u1")
        if thread:
            thread.join(timeout=10)

        plugin.bot.patch_message.assert_called()
        last_card = json.loads(plugin.bot.patch_message.call_args[0][1])
        assert "未产生输出" in last_card["elements"][0]["content"]

    @patch("plugins.claude_code.claude_code_plugin.subprocess.Popen")
    def test_file_not_found(self, mock_popen_cls, plugin):
        """Claude CLI 不存在时友好提示"""
        mock_popen_cls.side_effect = FileNotFoundError("No such file")

        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        plugin.handle_message("u1", "c1", "test")

        thread = plugin._running_threads.get("u1")
        if thread:
            thread.join(timeout=10)

        plugin.bot.patch_message.assert_called()
        last_card = json.loads(plugin.bot.patch_message.call_args[0][1])
        assert "未安装" in last_card["elements"][0]["content"]

    @patch("plugins.claude_code.claude_code_plugin.subprocess.Popen")
    def test_output_truncation(self, mock_popen_cls, plugin):
        """超长输出截断"""
        plugin._config["max_output_chars"] = 100
        long_text = "x" * 200
        mock_popen_cls.return_value = _mock_popen([
            _make_assistant_event(long_text) + "\n",
        ])

        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        plugin.handle_message("u1", "c1", "test")

        thread = plugin._running_threads.get("u1")
        if thread:
            thread.join(timeout=10)

        plugin.bot.patch_message.assert_called()
        last_card = json.loads(plugin.bot.patch_message.call_args[0][1])
        content = last_card["elements"][0]["content"]
        assert "截断" in content

    @patch("plugins.claude_code.claude_code_plugin.subprocess.Popen")
    def test_working_dir_passed_to_popen(self, mock_popen_cls, plugin):
        """工作目录正确传递给 Popen"""
        mock_popen_cls.return_value = _mock_popen([
            _make_result_event() + "\n",
        ])

        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        state = plugin._get_state("u1")
        state["working_dir"] = "/tmp/test_project"
        plugin.handle_message("u1", "c1", "test")

        thread = plugin._running_threads.get("u1")
        if thread:
            thread.join(timeout=10)

        cwd = mock_popen_cls.call_args[1].get("cwd")
        assert cwd == "/tmp/test_project"

    @patch("plugins.claude_code.claude_code_plugin.subprocess.Popen")
    def test_env_removes_claudecode(self, mock_popen_cls, plugin):
        """环境变量中移除 CLAUDECODE 避免嵌套检测"""
        mock_popen_cls.return_value = _mock_popen([
            _make_result_event() + "\n",
        ])

        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        plugin.handle_message("u1", "c1", "test")

        thread = plugin._running_threads.get("u1")
        if thread:
            thread.join(timeout=10)

        env = mock_popen_cls.call_args[1].get("env", {})
        assert "CLAUDECODE" not in env

    @patch("plugins.claude_code.claude_code_plugin.subprocess.Popen")
    def test_tool_call_flow_shows_log(self, mock_popen_cls, plugin):
        """工具调用流程：卡片包含工具日志和文字回复两段"""
        stdout_lines = [
            _make_tool_use_event("Read", {"file_path": "src/main.py"}, "toolu_001") + "\n",
            _make_tool_result_event("toolu_001", content="file content", is_error=False) + "\n",
            _make_assistant_event("已读取文件。") + "\n",
            _make_result_event(cost=0.01, duration=1000, turns=1) + "\n",
        ]
        mock_popen_cls.return_value = _mock_popen(stdout_lines)

        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        plugin.handle_message("u1", "c1", "read file")

        thread = plugin._running_threads.get("u1")
        if thread:
            thread.join(timeout=10)

        plugin.bot.patch_message.assert_called()
        last_card = json.loads(plugin.bot.patch_message.call_args[0][1])
        content = last_card["elements"][0]["content"]
        # 日志部分含工具调用和成功标记
        assert "Read" in content
        assert "src/main.py" in content
        assert "✅" in content
        # 文字回复部分
        assert "已读取文件。" in content
        # 分隔线
        assert "---" in content

    @patch("plugins.claude_code.claude_code_plugin.subprocess.Popen")
    def test_tool_call_error_shows_failure(self, mock_popen_cls, plugin):
        """工具执行失败时显示❌和错误摘要"""
        stdout_lines = [
            _make_tool_use_event("Bash", {"command": "make build"}, "toolu_002") + "\n",
            _make_tool_result_event("toolu_002", content="Permission denied", is_error=True) + "\n",
            _make_assistant_event("构建失败。") + "\n",
            _make_result_event() + "\n",
        ]
        mock_popen_cls.return_value = _mock_popen(stdout_lines)

        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        plugin.handle_message("u1", "c1", "build")

        thread = plugin._running_threads.get("u1")
        if thread:
            thread.join(timeout=10)

        last_card = json.loads(plugin.bot.patch_message.call_args[0][1])
        content = last_card["elements"][0]["content"]
        assert "❌" in content
        assert "Permission denied" in content


# ---- 命令构建测试 ----


class TestBuildCommand:
    """CLI 命令构建测试"""

    def test_basic_command(self, plugin):
        """基本命令参数正确（新会话使用 --session-id）"""
        cmd = plugin._build_command("hello", "test-session-id")
        assert cmd[0] == "/usr/bin/claude"
        assert "-p" in cmd
        assert "hello" in cmd
        assert "--output-format" in cmd
        assert "stream-json" in cmd
        assert "--session-id" in cmd
        assert "test-session-id" in cmd
        assert "--resume" not in cmd

    def test_resume_command(self, plugin):
        """恢复已有会话使用 --resume 而非 --session-id"""
        cmd = plugin._build_command("hello", "test-session-id", resume=True)
        assert "--resume" in cmd
        assert "test-session-id" in cmd
        assert "--session-id" not in cmd

    def test_permission_mode(self, plugin):
        """包含权限模式参数"""
        cmd = plugin._build_command("test", "session-1")
        assert "--permission-mode" in cmd
        assert "bypassPermissions" in cmd

    def test_max_turns(self, plugin):
        """包含最大轮次参数"""
        cmd = plugin._build_command("test", "session-1")
        assert "--max-turns" in cmd


# ---- 多用户隔离测试 ----


class TestUserIsolation:
    """多用户隔离测试"""

    def test_independent_sessions(self, plugin):
        """不同用户有独立的 session_id"""
        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        plugin.handle_message("u2", "c2", PLUGIN_KEYWORD)

        s1 = plugin._get_state("u1")["session_id"]
        s2 = plugin._get_state("u2")["session_id"]
        assert s1 != s2

    def test_independent_working_dirs(self, plugin):
        """不同用户有独立的工作目录"""
        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        plugin.handle_message("u2", "c2", PLUGIN_KEYWORD)

        with patch("plugins.claude_code.claude_code_plugin.os.path.isdir", return_value=True):
            plugin.handle_message("u1", "c1", "/cd /project_a")
            plugin.handle_message("u2", "c2", "/cd /project_b")

        assert plugin._get_state("u1")["working_dir"] == "/project_a"
        assert plugin._get_state("u2")["working_dir"] == "/project_b"


# ---- 进程终止测试 ----


class TestProcessKill:
    """进程终止测试"""

    def test_kill_running_process(self, plugin):
        """终止运行中的进程"""
        proc = Mock()
        proc.poll = Mock(return_value=None)
        proc.terminate = Mock()
        proc.wait = Mock()
        plugin._running_processes["u1"] = proc

        plugin._kill_process("u1")

        proc.terminate.assert_called_once()
        assert "u1" not in plugin._running_processes

    def test_kill_stubborn_process(self, plugin):
        """terminate 超时后用 kill"""
        proc = Mock()
        proc.poll = Mock(return_value=None)
        proc.terminate = Mock()
        proc.wait = Mock(side_effect=subprocess.TimeoutExpired("cmd", 5))
        proc.kill = Mock()
        plugin._running_processes["u1"] = proc

        plugin._kill_process("u1")

        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()

    def test_kill_already_exited(self, plugin):
        """进程已退出时不调用 terminate"""
        proc = Mock()
        proc.poll = Mock(return_value=0)
        proc.terminate = Mock()
        plugin._running_processes["u1"] = proc

        plugin._kill_process("u1")

        proc.terminate.assert_not_called()

    def test_kill_nonexistent_user(self, plugin):
        """终止不存在用户的进程不报错"""
        plugin._kill_process("nonexistent")  # 不应抛异常


# ---- 用户切换测试 ----


class TestRunAsUser:
    """run_as_user 用户切换测试"""

    @patch("plugins.claude_code.claude_code_plugin.os.getuid", return_value=0)
    @patch("plugins.claude_code.claude_code_plugin.pwd.getpwnam")
    def test_prepare_env_with_run_as_user(self, mock_getpwnam, mock_getuid, plugin):
        """配置 run_as_user 时切换 uid/gid 并设置 HOME 为用户主目录"""
        mock_pw = Mock()
        mock_pw.pw_uid = 1000
        mock_pw.pw_gid = 1000
        mock_pw.pw_dir = "/home/testuser"
        mock_getpwnam.return_value = mock_pw

        plugin._config["run_as_user"] = "testuser"

        env, preexec_fn = plugin._prepare_subprocess_env()

        # HOME 设置为用户主目录，claude 读取 $HOME/.claude/ 下的鉴权和配置
        assert env.get("HOME") == "/home/testuser"
        assert "CLAUDECODE" not in env
        assert preexec_fn is not None

    def test_prepare_env_without_run_as_user(self, plugin):
        """未配置 run_as_user 时不设置 preexec_fn"""
        plugin._config["run_as_user"] = ""

        env, preexec_fn = plugin._prepare_subprocess_env()

        assert "CLAUDECODE" not in env
        assert preexec_fn is None

    @patch("plugins.claude_code.claude_code_plugin.os.getuid", return_value=1000)
    def test_prepare_env_non_root_ignores_run_as_user(self, mock_getuid, plugin):
        """非 root 环境下忽略 run_as_user"""
        plugin._config["run_as_user"] = "otheruser"

        env, preexec_fn = plugin._prepare_subprocess_env()

        assert preexec_fn is None

    @patch("plugins.claude_code.claude_code_plugin.os.getuid", return_value=0)
    @patch("plugins.claude_code.claude_code_plugin.pwd.getpwnam", side_effect=KeyError("no such user"))
    def test_prepare_env_invalid_user(self, mock_getpwnam, mock_getuid, plugin):
        """配置了不存在的用户时 preexec_fn 为 None"""
        plugin._config["run_as_user"] = "nonexistent"

        env, preexec_fn = plugin._prepare_subprocess_env()

        assert preexec_fn is None

    @patch("plugins.claude_code.claude_code_plugin.subprocess.Popen")
    @patch("plugins.claude_code.claude_code_plugin.os.getuid", return_value=0)
    @patch("plugins.claude_code.claude_code_plugin.pwd.getpwnam")
    def test_popen_receives_preexec_fn(self, mock_getpwnam, mock_getuid, mock_popen_cls, plugin):
        """Popen 调用时传入 preexec_fn"""
        mock_pw = Mock()
        mock_pw.pw_uid = 1000
        mock_pw.pw_gid = 1000
        mock_pw.pw_dir = "/home/testuser"
        mock_getpwnam.return_value = mock_pw

        plugin._config["run_as_user"] = "testuser"
        mock_popen_cls.return_value = _mock_popen([
            _make_result_event() + "\n",
        ])

        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        plugin.handle_message("u1", "c1", "test")

        thread = plugin._running_threads.get("u1")
        if thread:
            thread.join(timeout=10)

        assert mock_popen_cls.call_args[1].get("preexec_fn") is not None


# ---- 会话级免确认模式测试 ----


class TestPermissionToggle:
    """会话级权限确认模式切换测试"""

    def test_default_bypass_is_false(self, plugin):
        """新用户默认为交互确认模式"""
        state = plugin._get_state("u1")
        assert state.get("bypass_permission", False) is False

    def test_toggle_on(self, plugin):
        """卡片动作 set_perm_mode 可切换为 bypass 模式"""
        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        plugin.handle_card_action("u1", "c1", "m1", {
            "action": "set_perm_mode", "plugin": PLUGIN_KEYWORD, "mode": "bypass"
        })
        assert plugin._get_state("u1")["session_perm_mode"] == "bypass"

    def test_toggle_off(self, plugin):
        """卡片动作 set_perm_mode 可切换回 interactive 模式"""
        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        plugin.handle_card_action("u1", "c1", "m1", {
            "action": "set_perm_mode", "plugin": PLUGIN_KEYWORD, "mode": "bypass"
        })
        plugin.handle_card_action("u1", "c1", "m1", {
            "action": "set_perm_mode", "plugin": PLUGIN_KEYWORD, "mode": "interactive"
        })
        assert plugin._get_state("u1")["session_perm_mode"] == "interactive"

    def test_toggle_blocked_when_running(self, plugin):
        """任务运行中，/permission 指令被拒绝并提示"""
        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        plugin._get_state("u1")["running"] = True
        plugin.handle_message("u1", "c1", "/permission")
        msg = plugin.bot.reply.call_args[0][1]
        assert "运行中" in msg
        assert plugin._get_state("u1")["session_perm_mode"] == "interactive"

    def test_new_session_resets_bypass(self, plugin):
        """「/new」重置权限模式为默认（interactive）"""
        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        plugin.handle_card_action("u1", "c1", "m1", {
            "action": "set_perm_mode", "plugin": PLUGIN_KEYWORD, "mode": "bypass"
        })
        assert plugin._get_state("u1")["session_perm_mode"] == "bypass"
        plugin.handle_message("u1", "c1", "/new")
        assert plugin._get_state("u1")["session_perm_mode"] == "interactive"

    def test_deactivate_resets_bypass(self, plugin):
        """deactivate 后重新获取状态默认为交互确认"""
        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        plugin.handle_message("u1", "c1", "/bypass")
        plugin.deactivate_user("u1")
        assert plugin._get_state("u1").get("bypass_permission", False) is False

    def test_status_shows_interactive_mode(self, plugin):
        """「status」显示交互确认模式"""
        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        plugin.handle_message("u1", "c1", "/status")
        msg = plugin.bot.reply.call_args[0][1]
        assert "interactive" in msg

    def test_status_shows_bypass_mode(self, plugin):
        """切换 bypass 后，「/status」显示 bypass 模式"""
        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        plugin.handle_card_action("u1", "c1", "m1", {
            "action": "set_perm_mode", "plugin": PLUGIN_KEYWORD, "mode": "bypass"
        })
        plugin.handle_message("u1", "c1", "/status")
        msg = plugin.bot.reply.call_args[0][1]
        assert "bypass" in msg

    def test_activation_shows_permission_mode(self, plugin):
        """激活消息包含权限模式信息和 /permission 指令"""
        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        msg = plugin.bot.reply.call_args[0][1]
        assert "权限模式" in msg
        assert "/permission" in msg

    def test_multiple_users_independent(self, plugin):
        """多用户权限模式互不干扰"""
        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        plugin.handle_message("u2", "c2", PLUGIN_KEYWORD)
        plugin.handle_card_action("u1", "c1", "m1", {
            "action": "set_perm_mode", "plugin": PLUGIN_KEYWORD, "mode": "bypass"
        })
        assert plugin._get_state("u1")["session_perm_mode"] == "bypass"
        assert plugin._get_state("u2")["session_perm_mode"] == "interactive"
