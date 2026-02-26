"""
Claude Code 独立机器人启动入口

在项目根目录执行：
    python -m plugins.claude_code

等效于在 Hub 机器人中只加载 CC 插件，但使用专属飞书应用凭证，
支持飞书 /命令 推荐框等独立机器人特性。

配置文件：项目根目录的 config.yaml（与 Hub 模式共用同一份）
  - app_id / app_secret：填写 CC 专属机器人的凭证
  - claude_code.*：CC 插件配置（与 Hub 模式相同）
"""

import logging
import sys
from pathlib import Path

# 确保项目根目录在模块搜索路径中（支持从任意位置启动）
_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import load_config
from plugins.claude_code.standalone import ClaudeCodeBot


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    cfg = load_config()
    bot = ClaudeCodeBot(cfg["app_id"], cfg["app_secret"])
    bot.start()


if __name__ == "__main__":
    main()
