"""
WorkspaceManager 单元测试

测试工作区管理核心逻辑（纯 Python 层，不依赖飞书 API）。
"""

import json
import os
import subprocess
from unittest.mock import patch, MagicMock

import pytest

from plugins.claude_code.workspace import WorkspaceManager


# ---- Fixtures ----


@pytest.fixture
def ws_dir(tmp_path):
    """返回临时工作区根目录"""
    return str(tmp_path / "workspaces")


@pytest.fixture
def repos():
    """返回测试仓库配置"""
    return {
        "simpler": {
            "url": "git@github.com:ChaoWao/simpler.git",
            "fork": "git@github.com:bot/simpler.git",
        },
        "pypto": {
            "url": "git@github.com:hw/pypto.git",
            "fork": "git@github.com:bot/pypto.git",
        },
    }


@pytest.fixture
def mgr(ws_dir, repos):
    """返回 WorkspaceManager 实例"""
    return WorkspaceManager(base_dir=ws_dir, repos=repos)


# ---- 用户目录 ----


class TestUserDir:
    """用户目录管理测试"""

    def test_ensure_user_dir_creates(self, mgr, ws_dir):
        """首次调用自动创建用户目录"""
        user_dir = mgr.ensure_user_dir("ou_user1")
        assert os.path.isdir(user_dir)
        assert user_dir == os.path.join(os.path.realpath(ws_dir), "ou_user1")

    def test_ensure_user_dir_idempotent(self, mgr):
        """多次调用不报错"""
        dir1 = mgr.ensure_user_dir("ou_user1")
        dir2 = mgr.ensure_user_dir("ou_user1")
        assert dir1 == dir2


# ---- 用户配置 ----


class TestProfile:
    """Git 身份配置测试"""

    def test_get_profile_empty(self, mgr):
        """用户未配置时返回空 dict"""
        assert mgr.get_profile("ou_new") == {}

    def test_has_profile_false(self, mgr):
        """用户未配置时返回 False"""
        assert mgr.has_profile("ou_new") is False

    def test_set_and_get_profile(self, mgr):
        """设置后可正确读取"""
        mgr.set_profile("ou_user1", "张三", "zhang@example.com")
        profile = mgr.get_profile("ou_user1")
        assert profile["git_name"] == "张三"
        assert profile["git_email"] == "zhang@example.com"

    def test_has_profile_true(self, mgr):
        """配置后返回 True"""
        mgr.set_profile("ou_user1", "张三", "zhang@example.com")
        assert mgr.has_profile("ou_user1") is True

    def test_profile_persistence(self, mgr, ws_dir, repos):
        """配置持久化到 profile.json"""
        mgr.set_profile("ou_user1", "张三", "zhang@example.com")
        # 重新创建 manager，验证读取
        mgr2 = WorkspaceManager(base_dir=ws_dir, repos=repos)
        profile = mgr2.get_profile("ou_user1")
        assert profile["git_name"] == "张三"

    def test_profile_overwrite(self, mgr):
        """覆盖旧配置"""
        mgr.set_profile("ou_user1", "张三", "zhang@example.com")
        mgr.set_profile("ou_user1", "李四", "li@example.com")
        profile = mgr.get_profile("ou_user1")
        assert profile["git_name"] == "李四"

    def test_get_profile_corrupted(self, mgr):
        """配置文件损坏时返回空 dict"""
        mgr.ensure_user_dir("ou_user1")
        path = mgr._profile_path("ou_user1")
        with open(path, "w") as f:
            f.write("not json{{{")
        assert mgr.get_profile("ou_user1") == {}


# ---- Git 身份应用 ----


class TestGitIdentity:
    """Git 身份应用测试"""

    def test_apply_git_identity(self, mgr, tmp_path):
        """应用 git 身份到仓库"""
        # 创建一个 git 仓库
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        subprocess.run(["git", "init", repo_path], capture_output=True)

        mgr.set_profile("ou_user1", "张三", "zhang@example.com")
        mgr.apply_git_identity("ou_user1", repo_path)

        # 验证 git config
        name = subprocess.run(
            ["git", "config", "--local", "user.name"],
            cwd=repo_path, capture_output=True, text=True,
        ).stdout.strip()
        email = subprocess.run(
            ["git", "config", "--local", "user.email"],
            cwd=repo_path, capture_output=True, text=True,
        ).stdout.strip()
        assert name == "张三"
        assert email == "zhang@example.com"

    def test_apply_git_identity_no_profile(self, mgr, tmp_path):
        """未配置 profile 时不执行任何操作"""
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        subprocess.run(["git", "init", repo_path], capture_output=True)
        # 不应抛异常
        mgr.apply_git_identity("ou_nouser", repo_path)

    def test_apply_git_identity_all(self, mgr):
        """应用到所有工作文件夹"""
        user_dir = mgr.ensure_user_dir("ou_user1")
        mgr.set_profile("ou_user1", "张三", "zhang@example.com")

        # 创建两个 git 仓库
        for name in ["simpler-test1", "pypto-test2"]:
            repo_path = os.path.join(user_dir, name)
            os.makedirs(repo_path)
            subprocess.run(["git", "init", repo_path], capture_output=True)

        count = mgr.apply_git_identity_all("ou_user1")
        assert count == 2


# ---- 文件夹名清理 ----


class TestSanitizeFolderName:
    """文件夹名清理测试"""

    def test_basic(self):
        """基本拼接"""
        assert WorkspaceManager.sanitize_folder_name("simpler", "fix-bug") == "simpler-fix-bug"

    def test_no_description(self):
        """无描述"""
        assert WorkspaceManager.sanitize_folder_name("simpler", "") == "simpler"

    def test_special_chars(self):
        """特殊字符替换"""
        result = WorkspaceManager.sanitize_folder_name("repo", "fix/this bug!")
        assert "/" not in result
        assert "!" not in result
        assert " " not in result

    def test_uppercase(self):
        """大写转小写"""
        assert WorkspaceManager.sanitize_folder_name("Repo", "FIX") == "repo-fix"

    def test_length_limit(self):
        """超长截断"""
        result = WorkspaceManager.sanitize_folder_name("r", "a" * 100)
        assert len(result) <= 40

    def test_path_traversal(self):
        """路径穿越防护"""
        result = WorkspaceManager.sanitize_folder_name("repo", "../../../etc")
        assert ".." not in result

    def test_chinese_chars(self):
        """中文字符保留"""
        result = WorkspaceManager.sanitize_folder_name("simpler", "修复登录")
        assert "修复登录" in result

    def test_empty_result_fallback(self):
        """全特殊字符时 fallback"""
        result = WorkspaceManager.sanitize_folder_name("", "!!!")
        assert result == "workspace"


# ---- 工作区操作 ----


class TestInitFolder:
    """工作区创建测试"""

    def test_unknown_repo(self, mgr):
        """未知仓库报错"""
        mgr.set_profile("ou_user1", "张三", "z@e.com")
        with pytest.raises(ValueError, match="未知仓库"):
            mgr.init_folder("ou_user1", "unknown", "test")

    def test_folder_exists(self, mgr):
        """文件夹已存在报错"""
        mgr.set_profile("ou_user1", "张三", "z@e.com")
        user_dir = mgr.ensure_user_dir("ou_user1")
        os.makedirs(os.path.join(user_dir, "simpler-test"))
        with pytest.raises(FileExistsError, match="已存在"):
            mgr.init_folder("ou_user1", "simpler", "test")

    @patch("plugins.claude_code.workspace.subprocess.run")
    def test_clone_failure(self, mock_run, mgr):
        """clone 失败报错"""
        mgr.set_profile("ou_user1", "张三", "z@e.com")
        mock_run.return_value = MagicMock(returncode=1, stderr="fatal: error")
        with pytest.raises(RuntimeError, match="git clone 失败"):
            mgr.init_folder("ou_user1", "simpler", "test")

    @patch("plugins.claude_code.workspace.subprocess.run")
    def test_successful_init(self, mock_run, mgr):
        """成功创建工作区"""
        mgr.set_profile("ou_user1", "张三", "z@e.com")
        user_dir = mgr.ensure_user_dir("ou_user1")

        def side_effect(cmd, **kwargs):
            # clone 命令：创建目录模拟 git clone
            if cmd[0] == "git" and cmd[1] == "clone":
                target = cmd[3]
                os.makedirs(os.path.join(target, ".git"), exist_ok=True)
                return MagicMock(returncode=0)
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect
        result = mgr.init_folder("ou_user1", "simpler", "fix-bug")
        assert result["folder_name"] == "simpler-fix-bug"
        assert result["repo_key"] == "simpler"
        assert os.path.isdir(result["folder_path"])

    @patch("plugins.claude_code.workspace.subprocess.run")
    def test_init_without_fork(self, mock_run, mgr):
        """无 fork 配置时跳过 remote 配置"""
        mgr.repos["nofork"] = {"url": "git@github.com:test/repo.git"}
        mgr.set_profile("ou_user1", "张三", "z@e.com")

        def side_effect(cmd, **kwargs):
            if cmd[0] == "git" and cmd[1] == "clone":
                target = cmd[3]
                os.makedirs(os.path.join(target, ".git"), exist_ok=True)
                return MagicMock(returncode=0)
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect
        result = mgr.init_folder("ou_user1", "nofork", "test")
        assert result["repo_key"] == "nofork"

        # 验证未调用 remote rename/add（clone + git config 调用，无 remote 操作）
        calls = [c for c in mock_run.call_args_list if c[0][0][1] == "remote"]
        assert len(calls) == 0


# ---- 列出 & 删除 ----


class TestListAndDelete:
    """列出和删除工作区测试"""

    def _make_folder(self, mgr, user_id, name):
        """创建模拟工作区文件夹"""
        user_dir = mgr.ensure_user_dir(user_id)
        folder_path = os.path.join(user_dir, name)
        os.makedirs(os.path.join(folder_path, ".git"), exist_ok=True)
        # 写一个文件以便测试 size
        with open(os.path.join(folder_path, "README.md"), "w") as f:
            f.write("test")
        return folder_path

    def test_list_empty(self, mgr):
        """无文件夹时返回空列表"""
        assert mgr.list_folders("ou_nouser") == []

    def test_list_folders(self, mgr):
        """正确列出工作文件夹"""
        self._make_folder(mgr, "ou_user1", "simpler-fix-bug")
        self._make_folder(mgr, "ou_user1", "pypto-add-cache")

        folders = mgr.list_folders("ou_user1")
        assert len(folders) == 2
        names = [f["name"] for f in folders]
        assert "pypto-add-cache" in names
        assert "simpler-fix-bug" in names

    def test_list_skips_non_git(self, mgr):
        """跳过非 git 目录"""
        user_dir = mgr.ensure_user_dir("ou_user1")
        os.makedirs(os.path.join(user_dir, "random-dir"))
        self._make_folder(mgr, "ou_user1", "simpler-test")

        folders = mgr.list_folders("ou_user1")
        assert len(folders) == 1

    def test_list_skips_files(self, mgr):
        """跳过文件（如 profile.json）"""
        mgr.set_profile("ou_user1", "张三", "z@e.com")
        self._make_folder(mgr, "ou_user1", "simpler-test")

        folders = mgr.list_folders("ou_user1")
        assert len(folders) == 1

    def test_delete_folder(self, mgr):
        """成功删除文件夹"""
        self._make_folder(mgr, "ou_user1", "simpler-fix-bug")
        assert mgr.delete_folder("ou_user1", "simpler-fix-bug") is True
        assert mgr.list_folders("ou_user1") == []

    def test_delete_nonexistent(self, mgr):
        """删除不存在的文件夹"""
        mgr.ensure_user_dir("ou_user1")
        assert mgr.delete_folder("ou_user1", "nonexistent") is False

    def test_delete_path_traversal(self, mgr):
        """路径穿越攻击被拒绝"""
        mgr.ensure_user_dir("ou_user1")
        assert mgr.delete_folder("ou_user1", "../../etc") is False


# ---- 解析文件夹 ----


class TestResolveFolder:
    """文件夹解析测试"""

    def _make_folder(self, mgr, user_id, name):
        user_dir = mgr.ensure_user_dir(user_id)
        folder_path = os.path.join(user_dir, name)
        os.makedirs(os.path.join(folder_path, ".git"), exist_ok=True)
        return folder_path

    def test_resolve_by_index(self, mgr):
        """按编号解析"""
        self._make_folder(mgr, "ou_user1", "pypto-cache")
        self._make_folder(mgr, "ou_user1", "simpler-bug")

        # 排序后 pypto-cache 是 [1]，simpler-bug 是 [2]
        result = mgr.resolve_folder("ou_user1", "1")
        assert result == "pypto-cache"
        result = mgr.resolve_folder("ou_user1", "2")
        assert result == "simpler-bug"

    def test_resolve_by_name(self, mgr):
        """按名称解析"""
        self._make_folder(mgr, "ou_user1", "simpler-fix-bug")
        result = mgr.resolve_folder("ou_user1", "simpler-fix-bug")
        assert result == "simpler-fix-bug"

    def test_resolve_not_found(self, mgr):
        """找不到返回 None"""
        assert mgr.resolve_folder("ou_user1", "nonexistent") is None

    def test_resolve_index_out_of_range(self, mgr):
        """编号越界返回 None"""
        self._make_folder(mgr, "ou_user1", "simpler-test")
        assert mgr.resolve_folder("ou_user1", "99") is None
        assert mgr.resolve_folder("ou_user1", "0") is None


# ---- 绑定检测 ----


class TestBoundFolder:
    """绑定文件夹检测测试"""

    def test_bound_exact(self, mgr):
        """working_dir 等于文件夹路径"""
        user_dir = mgr.ensure_user_dir("ou_user1")
        folder_path = os.path.join(user_dir, "simpler-test")
        os.makedirs(os.path.join(folder_path, ".git"), exist_ok=True)

        result = mgr.get_bound_folder_name("ou_user1", folder_path)
        assert result == "simpler-test"

    def test_bound_subdir(self, mgr):
        """working_dir 在文件夹子目录内"""
        user_dir = mgr.ensure_user_dir("ou_user1")
        folder_path = os.path.join(user_dir, "simpler-test")
        sub_path = os.path.join(folder_path, "src", "main")
        os.makedirs(os.path.join(folder_path, ".git"), exist_ok=True)
        os.makedirs(sub_path, exist_ok=True)

        result = mgr.get_bound_folder_name("ou_user1", sub_path)
        assert result == "simpler-test"

    def test_not_bound(self, mgr):
        """working_dir 不在任何工作区内"""
        mgr.ensure_user_dir("ou_user1")
        result = mgr.get_bound_folder_name("ou_user1", "/tmp/somewhere")
        assert result is None


# ---- 可用仓库 ----


class TestAvailableRepos:
    """可用仓库列表测试"""

    def test_available_repos(self, mgr):
        """返回正确的仓库列表"""
        repos = mgr.available_repos()
        assert len(repos) == 2
        keys = [r["key"] for r in repos]
        assert "simpler" in keys
        assert "pypto" in keys

    def test_empty_repos(self, ws_dir):
        """空仓库配置"""
        mgr = WorkspaceManager(base_dir=ws_dir, repos={})
        assert mgr.available_repos() == []
