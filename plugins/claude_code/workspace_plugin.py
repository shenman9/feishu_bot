"""
带工作区管理功能的 Claude Code 插件

通过继承 ClaudeCodePlugin 扩展，不修改上游父类任何代码。
新增工作区命令：/profile, /init, /bind, /unbind, /folders, /rmfolder, /run。
若 workspace 配置段未配置，所有新命令不生效，行为与原始 ClaudeCodePlugin 完全一致。
"""

import logging
import os
import shlex
import subprocess
from typing import Optional

from config import load_plugin_config
from plugins.claude_code.claude_code_plugin import ClaudeCodePlugin
from plugins.claude_code.constants import display_path
from plugins.claude_code.workspace import WorkspaceManager

logger = logging.getLogger(__name__)

# /run 命令输出截断阈值
_RUN_OUTPUT_MAX = 4000
# /run 命令超时（秒）
_RUN_TIMEOUT = 30


class WorkspaceClaudeCodePlugin(ClaudeCodePlugin):
    """带工作区管理功能的 Claude Code 插件（继承扩展，不改上游代码）"""

    # 工作区命令定义（独立于父类 _SPECIAL_COMMANDS，追加展示）
    _WORKSPACE_COMMANDS: list[dict] = [
        {"usage": "/profile [name] [email]", "brief": "配置/查看 Git 身份",  "detail": "查看或设置 Git 身份（/profile 查看，/profile 张三 email 设置）"},
        {"usage": "/init <repo> [描述]",     "brief": "创建工作区",          "detail": "从仓库创建新工作区（clone + 配置 fork remote + 自动绑定）"},
        {"usage": "/bind <名称|编号>",       "brief": "切换工作区",          "detail": "绑定指定工作区到当前会话（会重置会话）"},
        {"usage": "/unbind",                 "brief": "解绑工作区",          "detail": "解绑当前工作区，返回用户默认目录"},
        {"usage": "/folders",                "brief": "列出工作区",          "detail": "列出所有工作区文件夹及状态"},
        {"usage": "/rmfolder <名称|编号>",   "brief": "删除工作区",          "detail": "删除指定工作区文件夹（若当前绑定则先解绑）"},
        {"usage": "/run <命令>",             "brief": "执行 bash 命令",      "detail": "在当前工作目录下直接执行 bash 命令，不走 Claude Code"},
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._workspace_mgr: Optional[WorkspaceManager] = None

    # ---- 工作区管理器懒初始化 ----

    def _ensure_workspace_mgr(self) -> Optional[WorkspaceManager]:
        """若 workspace.base_dir 已配置则返回 manager，否则 None（功能未启用）"""
        if self._workspace_mgr is not None:
            return self._workspace_mgr

        cfg = self._load_plugin_config()
        ws_cfg = cfg.get("workspace", {})
        base_dir = ws_cfg.get("base_dir", "")

        if not base_dir:
            return None

        self._workspace_mgr = WorkspaceManager(
            base_dir=base_dir,
            repos=ws_cfg.get("repos", {}),
        )
        return self._workspace_mgr

    # ---- 覆写配置加载：追加 workspace 段 ----

    def _load_plugin_config(self) -> dict:
        """懒加载插件配置，追加 workspace 配置段"""
        cfg = super()._load_plugin_config()
        if "workspace" not in cfg:
            # 从同源 yaml 重新读取完整配置以获取 workspace 段
            if self._config_dir is not None:
                import yaml
                path = self._config_dir / "claude_code.yaml"
                cc = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
            else:
                cc = load_plugin_config("claude_code")
            ws = cc.get("workspace", {})
            cfg["workspace"] = {
                "base_dir": ws.get("base_dir", ""),
                "repos": ws.get("repos", {}),
            }
        return cfg

    # ---- 覆写 _get_state：默认 cwd 为用户工作区根 ----

    def _get_state(self, user_id: str, chat_id: str) -> dict:
        """获取用户状态，首次初始化时将 working_dir 设为用户工作区根"""
        key = self._state_key(user_id, chat_id)
        is_new = key not in self.user_states
        state = super()._get_state(user_id, chat_id)
        if is_new:
            ws_mgr = self._ensure_workspace_mgr()
            if ws_mgr:
                state["working_dir"] = ws_mgr.ensure_user_dir(user_id)
        return state

    # ---- 覆写 handle_message：拦截工作区命令 ----

    def handle_message(self, user_id: str, chat_id: str, text: str) -> None:
        """处理用户消息，优先匹配工作区命令"""
        state = self._get_state(user_id, chat_id)
        ws_mgr = self._ensure_workspace_mgr()

        # 工作区命令拦截（ws_mgr 为 None 时跳过，走父类）
        if ws_mgr:
            # /profile —— 查看当前配置
            if text == "/profile":
                self._handle_profile_view(user_id, chat_id, ws_mgr)
                return

            # /profile <name> <email> —— 设置 git 身份
            if text.startswith("/profile "):
                self._handle_profile_set(user_id, chat_id, text, ws_mgr)
                return

            # /init <repo> [描述] —— 创建工作区
            if text.startswith("/init "):
                self._handle_init(user_id, chat_id, text, ws_mgr)
                return

            # /bind <名称|编号> —— 切换工作区
            if text.startswith("/bind "):
                self._handle_bind(user_id, chat_id, text, ws_mgr)
                return

            # /unbind —— 解绑工作区
            if text == "/unbind":
                self._handle_unbind(user_id, chat_id, ws_mgr)
                return

            # /folders —— 列出工作区
            if text == "/folders":
                self._handle_folders(user_id, chat_id, ws_mgr)
                return

            # /rmfolder <名称|编号> —— 删除工作区
            if text.startswith("/rmfolder "):
                self._handle_rmfolder(user_id, chat_id, text, ws_mgr)
                return

            # /run <命令> —— 直接执行 bash
            if text.startswith("/run "):
                self._handle_run(user_id, chat_id, text)
                return

        # 没命中 → 走父类全部逻辑
        super().handle_message(user_id, chat_id, text)

    # ---- 工作区命令实现 ----

    def _handle_profile_view(self, user_id: str, chat_id: str, ws_mgr: WorkspaceManager) -> None:
        """查看当前 git 身份配置"""
        profile = ws_mgr.get_profile(user_id)
        if profile.get("git_name") and profile.get("git_email"):
            self.bot.reply(
                chat_id,
                f"**Git 身份配置**\n"
                f"姓名: {profile['git_name']}\n"
                f"邮箱: {profile['git_email']}",
            )
        else:
            self.bot.reply(
                chat_id,
                "尚未配置 Git 身份。\n"
                "请使用 `/profile <姓名> <邮箱>` 设置，例如:\n"
                "`/profile 张三 zhangsan@example.com`",
            )

    def _handle_profile_set(
        self, user_id: str, chat_id: str, text: str, ws_mgr: WorkspaceManager,
    ) -> None:
        """设置 git 身份"""
        raw = text[len("/profile "):].strip()
        try:
            args = shlex.split(raw)
        except ValueError:
            args = raw.split()

        if len(args) < 2:
            self.bot.reply(chat_id, "格式: `/profile <姓名> <邮箱>`\n例如: `/profile \"San Zhang\" zhangsan@example.com`")
            return

        # 最后一个参数为邮箱，其余拼接为姓名
        git_email = args[-1]
        git_name = " ".join(args[:-1])

        ws_mgr.set_profile(user_id, git_name, git_email)
        # 应用到已有工作区
        count = ws_mgr.apply_git_identity_all(user_id)

        reply_text = (
            f"**Git 身份已配置**\n"
            f"姓名: {git_name}\n"
            f"邮箱: {git_email}"
        )
        if count > 0:
            reply_text += f"\n已同步到 {count} 个工作区"
        self.bot.reply(chat_id, reply_text)

    def _handle_init(
        self, user_id: str, chat_id: str, text: str, ws_mgr: WorkspaceManager,
    ) -> None:
        """创建工作区"""
        # 前置检查：必须先配置 git 身份
        if not ws_mgr.has_profile(user_id):
            self.bot.reply(
                chat_id,
                "请先配置 Git 身份后再创建工作区。\n"
                "使用 `/profile <姓名> <邮箱>` 设置。",
            )
            return

        args = text[len("/init "):].strip().split(maxsplit=1)
        if not args:
            repos = ws_mgr.available_repos()
            repo_list = "\n".join(f"- `{r['key']}`" for r in repos) if repos else "无可用仓库"
            self.bot.reply(
                chat_id,
                f"格式: `/init <仓库> [描述]`\n可用仓库:\n{repo_list}",
            )
            return

        repo_key = args[0]
        description = args[1] if len(args) > 1 else ""

        try:
            self.bot.reply(chat_id, f"正在创建工作区，请稍候...")
            result = ws_mgr.init_folder(user_id, repo_key, description)
        except ValueError as e:
            self.bot.reply(chat_id, str(e))
            return
        except FileExistsError as e:
            self.bot.reply(chat_id, str(e))
            return
        except RuntimeError as e:
            self.bot.reply(chat_id, f"创建失败: {e}")
            return

        # 自动绑定到当前会话
        state = self._get_state(user_id, chat_id)
        state["working_dir"] = result["folder_path"]
        self._reset_session(user_id, chat_id)
        # 重置后需要重新设置 working_dir（_reset_session 不改 working_dir）
        state["working_dir"] = result["folder_path"]

        self.bot.reply(
            chat_id,
            f"**工作区已创建**\n"
            f"文件夹: {result['folder_name']}\n"
            f"仓库: {result['repo_key']}\n"
            f"已自动绑定到当前会话。\n\n"
            f"{self._format_status(user_id, chat_id)}",
        )
        self._send_perm_select_card_if_manual(chat_id, user_id)

    def _handle_bind(
        self, user_id: str, chat_id: str, text: str, ws_mgr: WorkspaceManager,
    ) -> None:
        """绑定工作区"""
        name_or_index = text[len("/bind "):].strip()
        if not name_or_index:
            self.bot.reply(chat_id, "格式: `/bind <名称|编号>`\n使用 `/folders` 查看可用工作区。")
            return

        folder_name = ws_mgr.resolve_folder(user_id, name_or_index)
        if not folder_name:
            self.bot.reply(
                chat_id,
                f"未找到工作区: {name_or_index}\n使用 `/folders` 查看可用工作区。",
            )
            return

        folder_path = ws_mgr.get_folder_path(user_id, folder_name)
        if not os.path.isdir(folder_path):
            self.bot.reply(chat_id, f"工作区目录不存在: {folder_name}")
            return

        state = self._get_state(user_id, chat_id)
        state["working_dir"] = folder_path
        self._reset_session(user_id, chat_id)
        state["working_dir"] = folder_path

        self.bot.reply(
            chat_id,
            f"已切换到工作区: {folder_name}\n\n"
            f"{self._format_status(user_id, chat_id)}",
        )
        self._send_perm_select_card_if_manual(chat_id, user_id)

    def _handle_unbind(self, user_id: str, chat_id: str, ws_mgr: WorkspaceManager) -> None:
        """解绑工作区，返回用户默认目录"""
        state = self._get_state(user_id, chat_id)
        user_dir = ws_mgr.ensure_user_dir(user_id)
        state["working_dir"] = user_dir
        self._reset_session(user_id, chat_id)
        state["working_dir"] = user_dir

        self.bot.reply(
            chat_id,
            f"已解绑工作区，返回默认目录。\n\n"
            f"{self._format_status(user_id, chat_id)}",
        )
        self._send_perm_select_card_if_manual(chat_id, user_id)

    def _handle_folders(self, user_id: str, chat_id: str, ws_mgr: WorkspaceManager) -> None:
        """列出用户所有工作区"""
        folders = ws_mgr.list_folders(user_id)
        if not folders:
            repos = ws_mgr.available_repos()
            repo_list = "\n".join(f"- `{r['key']}`" for r in repos) if repos else "无可用仓库"
            self.bot.reply(
                chat_id,
                f"暂无工作区。\n使用 `/init <仓库> [描述]` 创建。\n\n可用仓库:\n{repo_list}",
            )
            return

        state = self._get_state(user_id, chat_id)
        current_folder = ws_mgr.get_bound_folder_name(user_id, state["working_dir"])

        lines = ["**工作区列表**"]
        for i, folder in enumerate(folders, 1):
            marker = " ← 当前" if folder["name"] == current_folder else ""
            lines.append(
                f"[{i}] {folder['name']}  [{folder['repo']}]  ({folder['size']}){marker}"
            )

        self.bot.reply(chat_id, "\n".join(lines))

    def _handle_rmfolder(
        self, user_id: str, chat_id: str, text: str, ws_mgr: WorkspaceManager,
    ) -> None:
        """删除工作区"""
        name_or_index = text[len("/rmfolder "):].strip()
        if not name_or_index:
            self.bot.reply(chat_id, "格式: `/rmfolder <名称|编号>`\n使用 `/folders` 查看工作区。")
            return

        folder_name = ws_mgr.resolve_folder(user_id, name_or_index)
        if not folder_name:
            self.bot.reply(
                chat_id,
                f"未找到工作区: {name_or_index}\n使用 `/folders` 查看工作区。",
            )
            return

        # 若当前绑定此工作区则先解绑
        state = self._get_state(user_id, chat_id)
        current_folder = ws_mgr.get_bound_folder_name(user_id, state["working_dir"])
        if current_folder == folder_name:
            user_dir = ws_mgr.ensure_user_dir(user_id)
            state["working_dir"] = user_dir
            self._reset_session(user_id, chat_id)
            state["working_dir"] = user_dir

        ok = ws_mgr.delete_folder(user_id, folder_name)
        if ok:
            self.bot.reply(chat_id, f"已删除工作区: {folder_name}")
        else:
            self.bot.reply(chat_id, f"删除失败: {folder_name}")

    def _handle_run(self, user_id: str, chat_id: str, text: str) -> None:
        """在当前 working_dir 下执行 bash 命令，直接返回结果"""
        cmd_text = text[len("/run "):].strip()
        if not cmd_text:
            self.bot.reply(chat_id, "格式: `/run <命令>`\n例如: `/run git status`")
            return

        state = self._get_state(user_id, chat_id)
        cwd = state["working_dir"]
        env, preexec_fn = self._prepare_subprocess_env()

        try:
            result = subprocess.run(
                cmd_text, shell=True, cwd=cwd,
                capture_output=True, text=True, timeout=_RUN_TIMEOUT,
                env=env, preexec_fn=preexec_fn,
            )
            output = result.stdout
            if result.stderr:
                output += result.stderr
        except subprocess.TimeoutExpired:
            self.bot.reply(chat_id, f"命令执行超时（{_RUN_TIMEOUT}s）。")
            return
        except Exception as e:
            self.bot.reply(chat_id, f"命令执行失败: {e}")
            return

        if len(output) > _RUN_OUTPUT_MAX:
            output = output[:_RUN_OUTPUT_MAX] + "\n... (输出已截断)"

        if output.strip():
            self.bot.reply(chat_id, f"```\n{output}\n```")
        else:
            self.bot.reply(chat_id, "(无输出)")

    # ---- 覆写 _format_status：展示工作区信息 ----

    def _format_status(self, user_id: str, chat_id: str) -> str:
        """格式化状态文本，工作区模式下替换工作目录为工作区信息"""
        base = super()._format_status(user_id, chat_id)
        ws_mgr = self._ensure_workspace_mgr()
        if not ws_mgr:
            return base

        state = self._get_state(user_id, chat_id)
        folder_name = ws_mgr.get_bound_folder_name(user_id, state["working_dir"])
        if folder_name:
            # 从文件夹名提取 repo key
            repo_key = folder_name.split("-")[0] if "-" in folder_name else folder_name
            # 替换 "工作目录: ..." 行为 "工作区: folder_name (repo_key)"
            lines = base.split("\n")
            new_lines = []
            for line in lines:
                if line.startswith("工作目录:"):
                    new_lines.append(f"工作区: {folder_name} ({repo_key})")
                else:
                    new_lines.append(line)
            return "\n".join(new_lines)
        return base

    # ---- 覆写命令列表：追加工作区命令 ----

    @classmethod
    def _commands_brief(cls) -> str:
        """生成激活欢迎词中的简短指令列表，追加工作区命令"""
        base = super()._commands_brief()
        ws_lines = [
            f"- `{c['usage']}` {c['brief']}"
            for c in cls._WORKSPACE_COMMANDS
            if c.get("brief")
        ]
        return base + "\n" + "\n".join(ws_lines)

    @classmethod
    def _commands_detail(cls) -> str:
        """生成 /help 中的详细指令列表，追加工作区命令"""
        base = super()._commands_detail()
        ws_lines = [
            f"• `{c['usage']}` — {c['detail']}"
            for c in cls._WORKSPACE_COMMANDS
        ]
        return base + "\n" + "\n".join(ws_lines)
