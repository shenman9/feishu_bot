"""
WorkspaceClaudeCodePlugin 集成测试

测试工作区命令路由、父类命令透传、状态格式化等。
"""

import os
import subprocess
from unittest.mock import MagicMock, patch, call

import pytest

from plugins.claude_code.workspace_plugin import WorkspaceClaudeCodePlugin
from plugins.claude_code.claude_code_plugin import _DEFAULT_TIMEOUT
from plugins.claude_code.constants import PLUGIN_KEYWORD
from plugins.claude_code.stream_parser import DEFAULT_MAX_OUTPUT as _DEFAULT_MAX_OUTPUT
from plugins.claude_code.permission_manager import _DEFAULT_PERM_PORT, _DEFAULT_PERM_TIMEOUT


# ---- Fixtures ----


@pytest.fixture
def ws_base(tmp_path):
    """返回临时工作区根目录路径"""
    d = tmp_path / "workspaces"
    d.mkdir()
    return str(d)


@pytest.fixture
def ws_plugin(mock_bot, ws_base):
    """返回已注册 mock bot 且启用工作区的 WorkspaceClaudeCodePlugin"""
    p = WorkspaceClaudeCodePlugin()
    p.on_register(mock_bot)
    p._config = {
        "claude_path": "/usr/bin/claude",
        "default_working_dir": "",
        "timeout": _DEFAULT_TIMEOUT,
        "max_output_chars": _DEFAULT_MAX_OUTPUT,
        "default_perm_mode": "interactive",
        "max_turns": 50,
        "run_as_user": "",
        "permission_server_port": _DEFAULT_PERM_PORT,
        "permission_timeout": _DEFAULT_PERM_TIMEOUT,
        "default_model": "",
        "models": [],
        "workspace": {
            "base_dir": ws_base,
            "repos": {
                "simpler": {
                    "url": "git@github.com:ChaoWao/simpler.git",
                    "fork": "git@github.com:bot/simpler.git",
                },
                "pypto": {
                    "url": "git@github.com:hw/pypto.git",
                    "fork": "git@github.com:bot/pypto.git",
                },
            },
        },
    }
    # 跳过真实 HTTP 服务器启动
    perm_mgr = p._ensure_perm_manager()
    perm_mgr._started = True
    return p


@pytest.fixture
def ws_plugin_disabled(mock_bot):
    """返回工作区功能未启用的 WorkspaceClaudeCodePlugin"""
    p = WorkspaceClaudeCodePlugin()
    p.on_register(mock_bot)
    p._config = {
        "claude_path": "/usr/bin/claude",
        "default_working_dir": "",
        "timeout": _DEFAULT_TIMEOUT,
        "max_output_chars": _DEFAULT_MAX_OUTPUT,
        "default_perm_mode": "interactive",
        "max_turns": 50,
        "run_as_user": "",
        "permission_server_port": _DEFAULT_PERM_PORT,
        "permission_timeout": _DEFAULT_PERM_TIMEOUT,
        "default_model": "",
        "models": [],
        "workspace": {"base_dir": "", "repos": {}},
    }
    perm_mgr = p._ensure_perm_manager()
    perm_mgr._started = True
    return p


def _activate(plugin, user_id="ou_user1", chat_id="chat_001"):
    """激活插件"""
    plugin.handle_message(user_id, chat_id, PLUGIN_KEYWORD)


def _make_workspace_folder(ws_base, user_id, name):
    """手动创建工作区文件夹"""
    folder_path = os.path.join(ws_base, user_id, name)
    os.makedirs(os.path.join(folder_path, ".git"), exist_ok=True)
    return folder_path


# ---- 激活与默认目录 ----


class TestActivation:
    """激活与默认目录测试"""

    def test_activation_sets_user_dir(self, ws_plugin, ws_base, mock_bot):
        """激活后 working_dir 自动设为用户目录"""
        _activate(ws_plugin)
        state = ws_plugin._get_state("ou_user1", "chat_001")
        expected = os.path.join(os.path.realpath(ws_base), "ou_user1")
        assert state["working_dir"] == expected
        assert os.path.isdir(expected)

    def test_activation_disabled_workspace(self, ws_plugin_disabled, mock_bot):
        """工作区未启用时，working_dir 为默认"""
        _activate(ws_plugin_disabled)
        state = ws_plugin_disabled._get_state("ou_user1", "chat_001")
        # 应该是进程当前目录
        assert os.path.isabs(state["working_dir"])


# ---- /profile 命令 ----


class TestProfileCommand:
    """Profile 命令测试"""

    def test_view_empty(self, ws_plugin, mock_bot):
        """未配置时提示设置"""
        _activate(ws_plugin)
        mock_bot.reply.reset_mock()
        ws_plugin.handle_message("ou_user1", "chat_001", "/profile")
        reply_text = mock_bot.reply.call_args[0][1]
        assert "尚未配置" in reply_text

    def test_set_profile(self, ws_plugin, mock_bot):
        """设置 git 身份"""
        _activate(ws_plugin)
        mock_bot.reply.reset_mock()
        ws_plugin.handle_message("ou_user1", "chat_001", "/profile 张三 zhang@example.com")
        reply_text = mock_bot.reply.call_args[0][1]
        assert "张三" in reply_text
        assert "zhang@example.com" in reply_text

    def test_set_profile_missing_args(self, ws_plugin, mock_bot):
        """参数不足时提示格式"""
        _activate(ws_plugin)
        mock_bot.reply.reset_mock()
        ws_plugin.handle_message("ou_user1", "chat_001", "/profile onlyname")
        reply_text = mock_bot.reply.call_args[0][1]
        assert "格式" in reply_text

    def test_set_profile_quoted_name(self, ws_plugin, mock_bot):
        """带引号的名字正确解析"""
        _activate(ws_plugin)
        mock_bot.reply.reset_mock()
        ws_plugin.handle_message("ou_user1", "chat_001", '/profile "San Zhang" zhangsan@example.com')
        reply_text = mock_bot.reply.call_args[0][1]
        assert "San Zhang" in reply_text
        assert "zhangsan@example.com" in reply_text

    def test_set_profile_unquoted_multiword_name(self, ws_plugin, mock_bot):
        """不带引号的多词名字，最后一个参数为邮箱"""
        _activate(ws_plugin)
        mock_bot.reply.reset_mock()
        ws_plugin.handle_message("ou_user1", "chat_001", "/profile San Zhang zhangsan@example.com")
        reply_text = mock_bot.reply.call_args[0][1]
        assert "San Zhang" in reply_text
        assert "zhangsan@example.com" in reply_text

    def test_view_after_set(self, ws_plugin, mock_bot):
        """设置后查看"""
        _activate(ws_plugin)
        ws_plugin.handle_message("ou_user1", "chat_001", "/profile 张三 zhang@example.com")
        mock_bot.reply.reset_mock()
        ws_plugin.handle_message("ou_user1", "chat_001", "/profile")
        reply_text = mock_bot.reply.call_args[0][1]
        assert "张三" in reply_text


# ---- /init 命令 ----


class TestInitCommand:
    """Init 命令测试"""

    def test_init_no_profile(self, ws_plugin, mock_bot):
        """未配置 profile 时拒绝"""
        _activate(ws_plugin)
        mock_bot.reply.reset_mock()
        ws_plugin.handle_message("ou_user1", "chat_001", "/init simpler test")
        reply_text = mock_bot.reply.call_args[0][1]
        assert "Git 身份" in reply_text

    def test_init_unknown_repo(self, ws_plugin, mock_bot):
        """未知仓库"""
        _activate(ws_plugin)
        ws_plugin.handle_message("ou_user1", "chat_001", "/profile 张三 z@e.com")
        mock_bot.reply.reset_mock()
        ws_plugin.handle_message("ou_user1", "chat_001", "/init unknown test")
        reply_text = mock_bot.reply.call_args[0][1]
        assert "未知仓库" in reply_text

    @patch("plugins.claude_code.workspace.subprocess.run")
    def test_init_success(self, mock_run, ws_plugin, ws_base, mock_bot):
        """成功创建并自动绑定"""
        _activate(ws_plugin)
        ws_plugin.handle_message("ou_user1", "chat_001", "/profile 张三 z@e.com")

        def side_effect(cmd, **kwargs):
            if cmd[0] == "git" and cmd[1] == "clone":
                target = cmd[3]
                os.makedirs(os.path.join(target, ".git"), exist_ok=True)
                return MagicMock(returncode=0)
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect
        mock_bot.reply.reset_mock()
        ws_plugin.handle_message("ou_user1", "chat_001", "/init simpler fix-bug")

        # 最后一次 reply 应包含工作区信息
        reply_text = mock_bot.reply.call_args[0][1]
        assert "simpler-fix-bug" in reply_text
        assert "已自动绑定" in reply_text

        # 验证 working_dir 已切换
        state = ws_plugin._get_state("ou_user1", "chat_001")
        assert "simpler-fix-bug" in state["working_dir"]


# ---- /bind 命令 ----


class TestBindCommand:
    """Bind 命令测试"""

    def test_bind_by_name(self, ws_plugin, ws_base, mock_bot):
        """按名称绑定"""
        _activate(ws_plugin)
        folder_path = _make_workspace_folder(ws_base, "ou_user1", "simpler-fix-bug")
        mock_bot.reply.reset_mock()
        ws_plugin.handle_message("ou_user1", "chat_001", "/bind simpler-fix-bug")
        reply_text = mock_bot.reply.call_args[0][1]
        assert "simpler-fix-bug" in reply_text

        state = ws_plugin._get_state("ou_user1", "chat_001")
        assert state["working_dir"] == folder_path

    def test_bind_by_index(self, ws_plugin, ws_base, mock_bot):
        """按编号绑定"""
        _activate(ws_plugin)
        _make_workspace_folder(ws_base, "ou_user1", "simpler-fix-bug")
        mock_bot.reply.reset_mock()
        ws_plugin.handle_message("ou_user1", "chat_001", "/bind 1")
        reply_text = mock_bot.reply.call_args[0][1]
        assert "simpler-fix-bug" in reply_text

    def test_bind_not_found(self, ws_plugin, mock_bot):
        """绑定不存在的工作区"""
        _activate(ws_plugin)
        mock_bot.reply.reset_mock()
        ws_plugin.handle_message("ou_user1", "chat_001", "/bind nonexistent")
        reply_text = mock_bot.reply.call_args[0][1]
        assert "未找到" in reply_text

    def test_bind_resets_session(self, ws_plugin, ws_base, mock_bot):
        """绑定会重置会话"""
        _activate(ws_plugin)
        state = ws_plugin._get_state("ou_user1", "chat_001")
        old_session = state["session_id"]

        _make_workspace_folder(ws_base, "ou_user1", "simpler-fix-bug")
        ws_plugin.handle_message("ou_user1", "chat_001", "/bind simpler-fix-bug")

        assert state["session_id"] != old_session


# ---- /unbind 命令 ----


class TestUnbindCommand:
    """Unbind 命令测试"""

    def test_unbind(self, ws_plugin, ws_base, mock_bot):
        """解绑返回用户根目录"""
        _activate(ws_plugin)
        _make_workspace_folder(ws_base, "ou_user1", "simpler-fix-bug")
        ws_plugin.handle_message("ou_user1", "chat_001", "/bind simpler-fix-bug")
        mock_bot.reply.reset_mock()

        ws_plugin.handle_message("ou_user1", "chat_001", "/unbind")
        reply_text = mock_bot.reply.call_args[0][1]
        assert "解绑" in reply_text

        state = ws_plugin._get_state("ou_user1", "chat_001")
        expected_root = os.path.join(os.path.realpath(ws_base), "ou_user1")
        assert state["working_dir"] == expected_root


# ---- /folders 命令 ----


class TestFoldersCommand:
    """Folders 命令测试"""

    def test_empty_folders(self, ws_plugin, mock_bot):
        """空列表提示创建"""
        _activate(ws_plugin)
        mock_bot.reply.reset_mock()
        ws_plugin.handle_message("ou_user1", "chat_001", "/folders")
        reply_text = mock_bot.reply.call_args[0][1]
        assert "暂无" in reply_text

    def test_list_folders(self, ws_plugin, ws_base, mock_bot):
        """正确列出工作区"""
        _activate(ws_plugin)
        _make_workspace_folder(ws_base, "ou_user1", "simpler-fix-bug")
        _make_workspace_folder(ws_base, "ou_user1", "pypto-cache")
        mock_bot.reply.reset_mock()
        ws_plugin.handle_message("ou_user1", "chat_001", "/folders")
        reply_text = mock_bot.reply.call_args[0][1]
        assert "simpler-fix-bug" in reply_text
        assert "pypto-cache" in reply_text

    def test_folders_marks_current(self, ws_plugin, ws_base, mock_bot):
        """标记当前绑定的工作区"""
        _activate(ws_plugin)
        _make_workspace_folder(ws_base, "ou_user1", "simpler-fix-bug")
        ws_plugin.handle_message("ou_user1", "chat_001", "/bind simpler-fix-bug")
        mock_bot.reply.reset_mock()
        ws_plugin.handle_message("ou_user1", "chat_001", "/folders")
        reply_text = mock_bot.reply.call_args[0][1]
        assert "当前" in reply_text


# ---- /rmfolder 命令 ----


class TestRmfolderCommand:
    """Rmfolder 命令测试"""

    def test_delete_folder(self, ws_plugin, ws_base, mock_bot):
        """成功删除"""
        _activate(ws_plugin)
        _make_workspace_folder(ws_base, "ou_user1", "simpler-fix-bug")
        mock_bot.reply.reset_mock()
        ws_plugin.handle_message("ou_user1", "chat_001", "/rmfolder simpler-fix-bug")
        reply_text = mock_bot.reply.call_args[0][1]
        assert "已删除" in reply_text

    def test_delete_current_unbinds(self, ws_plugin, ws_base, mock_bot):
        """删除当前绑定的工作区会先解绑"""
        _activate(ws_plugin)
        _make_workspace_folder(ws_base, "ou_user1", "simpler-fix-bug")
        ws_plugin.handle_message("ou_user1", "chat_001", "/bind simpler-fix-bug")
        ws_plugin.handle_message("ou_user1", "chat_001", "/rmfolder simpler-fix-bug")

        state = ws_plugin._get_state("ou_user1", "chat_001")
        expected_root = os.path.join(os.path.realpath(ws_base), "ou_user1")
        assert state["working_dir"] == expected_root

    def test_delete_not_found(self, ws_plugin, mock_bot):
        """删除不存在的工作区"""
        _activate(ws_plugin)
        mock_bot.reply.reset_mock()
        ws_plugin.handle_message("ou_user1", "chat_001", "/rmfolder nonexistent")
        reply_text = mock_bot.reply.call_args[0][1]
        assert "未找到" in reply_text


# ---- /run 命令 ----


class TestRunCommand:
    """Run 命令测试"""

    @patch("plugins.claude_code.workspace_plugin.subprocess.run")
    def test_run_basic(self, mock_run, ws_plugin, mock_bot):
        """基本命令执行"""
        _activate(ws_plugin)
        mock_run.return_value = MagicMock(stdout="hello world\n", stderr="", returncode=0)
        mock_bot.reply.reset_mock()
        ws_plugin.handle_message("ou_user1", "chat_001", "/run echo hello world")
        reply_text = mock_bot.reply.call_args[0][1]
        assert "hello world" in reply_text

    @patch("plugins.claude_code.workspace_plugin.subprocess.run")
    def test_run_empty_output(self, mock_run, ws_plugin, mock_bot):
        """无输出"""
        _activate(ws_plugin)
        mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        mock_bot.reply.reset_mock()
        ws_plugin.handle_message("ou_user1", "chat_001", "/run true")
        reply_text = mock_bot.reply.call_args[0][1]
        assert "无输出" in reply_text

    @patch("plugins.claude_code.workspace_plugin.subprocess.run")
    def test_run_timeout(self, mock_run, ws_plugin, mock_bot):
        """命令超时"""
        _activate(ws_plugin)
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="sleep", timeout=30)
        mock_bot.reply.reset_mock()
        ws_plugin.handle_message("ou_user1", "chat_001", "/run sleep 100")
        reply_text = mock_bot.reply.call_args[0][1]
        assert "超时" in reply_text

    @patch("plugins.claude_code.workspace_plugin.subprocess.run")
    def test_run_truncation(self, mock_run, ws_plugin, mock_bot):
        """长输出截断"""
        _activate(ws_plugin)
        long_output = "x" * 5000
        mock_run.return_value = MagicMock(stdout=long_output, stderr="", returncode=0)
        mock_bot.reply.reset_mock()
        ws_plugin.handle_message("ou_user1", "chat_001", "/run cat bigfile")
        reply_text = mock_bot.reply.call_args[0][1]
        assert "截断" in reply_text

    @patch("plugins.claude_code.workspace_plugin.subprocess.run")
    def test_run_uses_working_dir(self, mock_run, ws_plugin, ws_base, mock_bot):
        """命令在当前 working_dir 下执行"""
        _activate(ws_plugin)
        mock_run.return_value = MagicMock(stdout="ok\n", stderr="", returncode=0)
        ws_plugin.handle_message("ou_user1", "chat_001", "/run pwd")

        call_kwargs = mock_run.call_args[1]
        expected_dir = os.path.join(os.path.realpath(ws_base), "ou_user1")
        assert call_kwargs["cwd"] == expected_dir

    def test_run_empty_command(self, ws_plugin, mock_bot):
        """空命令提示格式"""
        _activate(ws_plugin)
        mock_bot.reply.reset_mock()
        ws_plugin.handle_message("ou_user1", "chat_001", "/run ")
        reply_text = mock_bot.reply.call_args[0][1]
        assert "格式" in reply_text


# ---- 父类命令透传 ----


class TestParentCommands:
    """父类命令不受工作区影响"""

    def test_new_command(self, ws_plugin, mock_bot):
        """/new 命令透传"""
        _activate(ws_plugin)
        mock_bot.reply.reset_mock()
        ws_plugin.handle_message("ou_user1", "chat_001", "/new")
        reply_text = mock_bot.reply.call_args[0][1]
        assert "会话已重置" in reply_text

    def test_status_command(self, ws_plugin, mock_bot):
        """/status 命令透传"""
        _activate(ws_plugin)
        mock_bot.reply.reset_mock()
        ws_plugin.handle_message("ou_user1", "chat_001", "/status")
        reply_text = mock_bot.reply.call_args[0][1]
        assert "会话:" in reply_text

    def test_cancel_command(self, ws_plugin, mock_bot):
        """/cancel 命令透传"""
        _activate(ws_plugin)
        mock_bot.reply.reset_mock()
        ws_plugin.handle_message("ou_user1", "chat_001", "/cancel")
        reply_text = mock_bot.reply.call_args[0][1]
        assert "没有运行中" in reply_text

    def test_help_command(self, ws_plugin, mock_bot):
        """/help 包含工作区命令"""
        _activate(ws_plugin)
        mock_bot.reply.reset_mock()
        ws_plugin.handle_message("ou_user1", "chat_001", "/help")
        reply_text = mock_bot.reply.call_args[0][1]
        # 工作区命令应出现在帮助中
        assert "/profile" in reply_text
        assert "/init" in reply_text
        assert "/run" in reply_text


# ---- 状态格式化 ----


class TestFormatStatus:
    """状态格式化测试"""

    def test_status_shows_workspace(self, ws_plugin, ws_base, mock_bot):
        """绑定工作区后状态显示工作区名"""
        _activate(ws_plugin)
        _make_workspace_folder(ws_base, "ou_user1", "simpler-fix-bug")
        ws_plugin.handle_message("ou_user1", "chat_001", "/bind simpler-fix-bug")
        mock_bot.reply.reset_mock()
        ws_plugin.handle_message("ou_user1", "chat_001", "/status")
        reply_text = mock_bot.reply.call_args[0][1]
        assert "工作区:" in reply_text
        assert "simpler-fix-bug" in reply_text

    def test_status_shows_dir_when_unbound(self, ws_plugin, mock_bot):
        """未绑定工作区时显示工作目录"""
        _activate(ws_plugin)
        mock_bot.reply.reset_mock()
        ws_plugin.handle_message("ou_user1", "chat_001", "/status")
        reply_text = mock_bot.reply.call_args[0][1]
        assert "工作目录:" in reply_text


# ---- 工作区禁用时降级 ----


class TestDisabledWorkspace:
    """工作区未启用时的降级行为"""

    def test_workspace_commands_fallthrough(self, ws_plugin_disabled, mock_bot):
        """工作区命令走父类逻辑（作为普通 prompt）"""
        _activate(ws_plugin_disabled)
        state = ws_plugin_disabled._get_state("ou_user1", "chat_001")
        # /profile 不会被拦截，走父类 handle_message（会被并发控制等拦截）
        mock_bot.reply.reset_mock()
        # /folders 作为普通文本会被当作 prompt 发给 Claude Code
        # 由于 running 为 False，应进入正常执行流
        # 这里只验证不抛异常且调用了 bot 某些方法
        ws_plugin_disabled.handle_message("ou_user1", "chat_001", "/status")
        assert mock_bot.reply.called

    def test_parent_commands_work(self, ws_plugin_disabled, mock_bot):
        """父类命令正常工作"""
        _activate(ws_plugin_disabled)
        mock_bot.reply.reset_mock()
        ws_plugin_disabled.handle_message("ou_user1", "chat_001", "/new")
        reply_text = mock_bot.reply.call_args[0][1]
        assert "会话已重置" in reply_text


# ---- 命令列表 ----


class TestCommandLists:
    """命令列表覆盖测试"""

    def test_brief_includes_workspace(self):
        """简短列表包含工作区命令"""
        brief = WorkspaceClaudeCodePlugin._commands_brief()
        assert "/profile" in brief
        assert "/init" in brief
        assert "/run" in brief

    def test_detail_includes_workspace(self):
        """详细列表包含工作区命令"""
        detail = WorkspaceClaudeCodePlugin._commands_detail()
        assert "/profile" in detail
        assert "/init" in detail
        assert "/folders" in detail
        assert "/rmfolder" in detail
