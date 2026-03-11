"""
Claude Code 独立机器人启动入口

在项目根目录执行：
    python -m plugins.claude_code

等效于在 Hub 机器人中只加载 CC 插件，但使用专属飞书应用凭证，
支持飞书 /命令 推荐框等独立机器人特性。

配置文件：
  默认读取项目根目录 config/ 目录
  若设置环境变量 CC_CONFIG_DIR，则从该目录读取 system.yaml 和 claude_code.yaml，
  数据目录通过 CC_DATA_DIR 环境变量指定（run_cc.sh 负责设置）。
"""

import logging
import os
import signal
import sys
from pathlib import Path

import yaml

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

    cc_config_dir = os.environ.get("CC_CONFIG_DIR")
    if cc_config_dir:
        # CC 专属机器人：从 CC_CONFIG_DIR 加载独立的飞书应用凭证
        system_path = Path(cc_config_dir) / "system.yaml"
        if not system_path.exists():
            raise FileNotFoundError(
                f"CC 系统配置文件不存在: {system_path}\n"
                "请复制 config/cc/system.yaml.example 为 config/cc/system.yaml 并填入实际值。"
            )
        cfg = yaml.safe_load(system_path.read_text(encoding="utf-8")) or {}
        if not cfg.get("app_id") or not cfg.get("app_secret"):
            raise ValueError(f"{system_path} 中 app_id 和 app_secret 不能为空")
    else:
        cfg = load_config()

    # 注册 SIGTERM 处理器：将 SIGTERM 转为正常退出，确保 atexit 清理逻辑被触发
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    bot = ClaudeCodeBot(cfg["app_id"], cfg["app_secret"])
    bot.start()


if __name__ == "__main__":
    main()
