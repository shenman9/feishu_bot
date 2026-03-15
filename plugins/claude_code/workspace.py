"""
多用户隔离工作区管理器

纯逻辑层，不依赖飞书 API。负责：
- 用户目录创建与隔离
- Git 身份配置持久化（profile.json）
- 仓库 clone + fork remote 配置
- 工作区文件夹的 CRUD 与索引解析
"""

import json
import logging
import os
import re
import shutil
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

# 文件夹名最大长度
_MAX_FOLDER_NAME_LEN = 40


class WorkspaceManager:
    """多用户隔离工作区管理器"""

    def __init__(self, base_dir: str, repos: dict):
        """
        Args:
            base_dir: 工作区根目录，如 "/data/wcwxyai/workspaces"
            repos: 仓库配置，如 {"simpler": {"url": "...", "fork": "..."}}
        """
        self.base_dir = os.path.realpath(base_dir)
        self.repos = repos

    # ---- 用户目录 ----

    def ensure_user_dir(self, user_id: str) -> str:
        """确保用户目录存在，返回路径。首次调用时自动 mkdir。"""
        user_dir = os.path.join(self.base_dir, user_id)
        os.makedirs(user_dir, exist_ok=True)
        return user_dir

    # ---- 用户配置 ----

    def _profile_path(self, user_id: str) -> str:
        """返回用户配置文件路径"""
        return os.path.join(self.base_dir, user_id, "profile.json")

    def get_profile(self, user_id: str) -> dict:
        """读取用户配置，返回 {"git_name": "...", "git_email": "..."} 或空 dict"""
        path = self._profile_path(user_id)
        if not os.path.isfile(path):
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("读取用户配置失败: %s, %s", path, e)
            return {}

    def set_profile(self, user_id: str, git_name: str, git_email: str) -> None:
        """保存用户配置到 <base_dir>/<user_id>/profile.json"""
        self.ensure_user_dir(user_id)
        path = self._profile_path(user_id)
        data = {"git_name": git_name, "git_email": git_email}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info("用户配置已保存: user=%s, name=%s, email=%s", user_id, git_name, git_email)

    def has_profile(self, user_id: str) -> bool:
        """检查用户是否已配置 git 身份"""
        profile = self.get_profile(user_id)
        return bool(profile.get("git_name") and profile.get("git_email"))

    def apply_git_identity(self, user_id: str, folder_path: str) -> None:
        """将用户 git 身份应用到指定文件夹（git config --local）"""
        profile = self.get_profile(user_id)
        if not profile.get("git_name") or not profile.get("git_email"):
            return
        try:
            subprocess.run(
                ["git", "config", "--local", "user.name", profile["git_name"]],
                cwd=folder_path, check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "config", "--local", "user.email", profile["git_email"]],
                cwd=folder_path, check=True, capture_output=True,
            )
            logger.info(
                "Git 身份已应用: user=%s, path=%s, name=%s",
                user_id, folder_path, profile["git_name"],
            )
        except subprocess.CalledProcessError as e:
            logger.warning("应用 git 身份失败: %s, %s", folder_path, e)

    def apply_git_identity_all(self, user_id: str) -> int:
        """将 git 身份应用到用户的所有工作文件夹，返回成功数"""
        folders = self.list_folders(user_id)
        count = 0
        for folder in folders:
            try:
                self.apply_git_identity(user_id, folder["path"])
                count += 1
            except Exception as e:
                logger.warning("应用 git 身份到 %s 失败: %s", folder["name"], e)
        return count

    # ---- 工作区操作 ----

    def init_folder(self, user_id: str, repo_key: str, description: str) -> dict:
        """创建新工作区：clone 仓库 + 配置 fork remote + 应用 git 身份。

        返回 {"folder_name": ..., "folder_path": ..., "repo_key": ...}

        步骤：
        1. 校验 repo_key 在配置中存在
        2. 生成文件夹名：<repo_key>-<description>（sanitize_folder_name）
        3. git clone <url> <user_dir>/<folder_name>
        4. 配置 fork remote：rename origin→upstream, add origin=fork, fetch
        5. 应用 git identity (git config --local)
        """
        if repo_key not in self.repos:
            available = ", ".join(self.repos.keys()) if self.repos else "无"
            raise ValueError(f"未知仓库: {repo_key}，可用仓库: {available}")

        repo_cfg = self.repos[repo_key]
        repo_url = repo_cfg["url"]
        fork_url = repo_cfg.get("fork", "")

        folder_name = self.sanitize_folder_name(repo_key, description)
        user_dir = self.ensure_user_dir(user_id)
        folder_path = os.path.join(user_dir, folder_name)

        if os.path.exists(folder_path):
            raise FileExistsError(f"工作区已存在: {folder_name}")

        # 1. git clone
        logger.info("开始 clone 仓库: %s → %s", repo_url, folder_path)
        result = subprocess.run(
            ["git", "clone", repo_url, folder_path],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git clone 失败: {result.stderr.strip()}")

        # 2. 配置 fork remote
        if fork_url:
            try:
                # origin → upstream
                subprocess.run(
                    ["git", "remote", "rename", "origin", "upstream"],
                    cwd=folder_path, check=True, capture_output=True,
                )
                # 添加 origin = fork
                subprocess.run(
                    ["git", "remote", "add", "origin", fork_url],
                    cwd=folder_path, check=True, capture_output=True,
                )
                # fetch origin
                subprocess.run(
                    ["git", "fetch", "origin"],
                    cwd=folder_path, capture_output=True, timeout=120,
                )
                logger.info("Fork remote 已配置: upstream=%s, origin=%s", repo_url, fork_url)
            except subprocess.CalledProcessError as e:
                logger.warning("配置 fork remote 失败: %s", e)

        # 3. 应用 git 身份
        self.apply_git_identity(user_id, folder_path)

        return {
            "folder_name": folder_name,
            "folder_path": folder_path,
            "repo_key": repo_key,
        }

    def list_folders(self, user_id: str) -> list[dict]:
        """列出用户所有工作文件夹。

        返回 [{"name": ..., "repo": ..., "size": ..., "path": ...}]
        """
        user_dir = os.path.join(self.base_dir, user_id)
        if not os.path.isdir(user_dir):
            return []

        folders = []
        for entry in sorted(os.listdir(user_dir)):
            entry_path = os.path.join(user_dir, entry)
            # 跳过非目录和配置文件
            if not os.path.isdir(entry_path):
                continue
            # 检查是否为 git 仓库
            if not os.path.isdir(os.path.join(entry_path, ".git")):
                continue

            # 解析仓库别名（文件夹名第一段）
            repo_key = entry.split("-")[0] if "-" in entry else entry
            # 估算大小
            size = self._dir_size_display(entry_path)

            folders.append({
                "name": entry,
                "repo": repo_key,
                "size": size,
                "path": entry_path,
            })
        return folders

    def delete_folder(self, user_id: str, folder_name: str) -> bool:
        """删除工作文件夹。安全检查：realpath 必须在用户目录下。"""
        user_dir = os.path.realpath(os.path.join(self.base_dir, user_id))
        folder_path = os.path.realpath(os.path.join(user_dir, folder_name))

        # 安全校验：路径必须在用户目录下
        if not folder_path.startswith(user_dir + os.sep):
            logger.warning("路径越界，拒绝删除: %s", folder_path)
            return False

        if not os.path.isdir(folder_path):
            return False

        shutil.rmtree(folder_path)
        logger.info("工作区已删除: user=%s, folder=%s", user_id, folder_name)
        return True

    def resolve_folder(self, user_id: str, name_or_index: str) -> Optional[str]:
        """解析文件夹名称或编号索引（"1" → 第一个文件夹名），返回文件夹名或 None"""
        folders = self.list_folders(user_id)
        if not folders:
            return None

        # 尝试按编号解析
        try:
            idx = int(name_or_index)
            if 1 <= idx <= len(folders):
                return folders[idx - 1]["name"]
        except ValueError:
            pass

        # 按名称精确匹配
        for folder in folders:
            if folder["name"] == name_or_index:
                return folder["name"]

        return None

    def get_folder_path(self, user_id: str, folder_name: str) -> str:
        """返回文件夹绝对路径"""
        return os.path.join(self.base_dir, user_id, folder_name)

    def get_bound_folder_name(self, user_id: str, working_dir: str) -> Optional[str]:
        """若 working_dir 在用户某工作区文件夹内，返回文件夹名，否则 None"""
        user_dir = os.path.realpath(os.path.join(self.base_dir, user_id))
        real_working = os.path.realpath(working_dir)

        for folder in self.list_folders(user_id):
            folder_real = os.path.realpath(folder["path"])
            # working_dir 等于或在文件夹内
            if real_working == folder_real or real_working.startswith(folder_real + os.sep):
                return folder["name"]
        return None

    def available_repos(self) -> list[dict]:
        """返回可用仓库列表 [{"key": "simpler", "url": "..."}]"""
        return [
            {"key": key, "url": cfg["url"]}
            for key, cfg in self.repos.items()
        ]

    @staticmethod
    def sanitize_folder_name(repo_key: str, description: str) -> str:
        """生成合法文件夹名：小写、连字符、去特殊字符、≤40字符"""
        # 合并 repo_key 和 description
        raw = f"{repo_key}-{description}" if description else repo_key
        # 转小写
        raw = raw.lower()
        # 替换非字母数字为连字符
        raw = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", raw)
        # 去首尾连字符
        raw = raw.strip("-")
        # 截断长度
        if len(raw) > _MAX_FOLDER_NAME_LEN:
            raw = raw[:_MAX_FOLDER_NAME_LEN].rstrip("-")
        return raw or "workspace"

    @staticmethod
    def _dir_size_display(path: str) -> str:
        """估算目录大小，返回人类可读格式"""
        try:
            total = 0
            for dirpath, _dirnames, filenames in os.walk(path):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    try:
                        total += os.path.getsize(fp)
                    except OSError:
                        pass
            if total < 1024:
                return f"{total}B"
            elif total < 1024 * 1024:
                return f"{total // 1024}K"
            elif total < 1024 * 1024 * 1024:
                return f"{total // (1024 * 1024)}M"
            else:
                return f"{total // (1024 * 1024 * 1024)}G"
        except OSError:
            return "?"
