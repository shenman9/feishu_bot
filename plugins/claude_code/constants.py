"""
Claude Code 插件共享常量与工具函数

所有子模块共用的常量和纯函数集中于此，避免循环导入。
"""

import datetime
import os
import pathlib

# 插件关键词（激活、卡片路由等多处引用）
PLUGIN_KEYWORD = "CC"

# 北京时区（UTC+8）
BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8))

# 插件目录路径
PLUGIN_DIR = pathlib.Path(__file__).parent           # plugins/claude_code/
PROJECT_ROOT = PLUGIN_DIR.parent.parent              # hub_agent/
CC_DATA_DIR = PROJECT_ROOT / "data" / "claude_code"  # 运行时数据目录

# /model 默认可选模型列表（配置文件未指定时使用）
DEFAULT_MODELS: list[dict] = [
    {"alias": "sonnet", "label": "Sonnet", "desc": "速度与质量均衡，适合日常开发任务"},
    {"alias": "opus", "label": "Opus", "desc": "最强推理能力，适合复杂架构和疑难问题"},
    {"alias": "haiku", "label": "Haiku", "desc": "最快响应速度，适合简单问答和快速任务"},
]

# 切换会话后展示的历史对话轮数
HISTORY_PREVIEW_ROUNDS = 3


def display_path(path: str, base: str = "") -> str:
    """格式化路径用于展示：若相对路径更短则优先使用

    Args:
        path: 原始路径（若非绝对路径则原样返回）
        base: 参考基准目录（通常为工作目录），用于计算相对路径

    Returns:
        原路径、相对于 base 的路径、以及 ~ 缩写中最短的一个
    """
    if not path or not os.path.isabs(path):
        return path
    candidates = [path]
    # 尝试相对于 base 的路径
    if base:
        try:
            rel = os.path.relpath(path, base)
            candidates.append(rel)
        except ValueError:
            pass
    # 尝试 ~ 缩写
    home = os.path.expanduser("~")
    if path == home:
        candidates.append("~")
    elif path.startswith(home + os.sep):
        candidates.append("~" + path[len(home):])
    return min(candidates, key=len)


def resolve_working_dir(raw: str = "") -> str:
    """将 working_dir 配置值解析为有效绝对路径

    使用 realpath 归一化，解析符号链接和 .. 等相对路径组件。
    空字符串表示"使用默认目录"，回落到进程当前工作目录 os.getcwd()。
    这样 state["working_dir"] 始终持有真实绝对路径，避免下游散落多处 fallback。
    """
    return os.path.realpath(raw) if raw else os.path.realpath(os.getcwd())
