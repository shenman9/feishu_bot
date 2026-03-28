"""
飞书机器人启动入口
"""

import logging
import signal
import sys

from config import load_config
from core.hub_bot import HubBot
from plugins.rps_game import RPSPlugin
from plugins.file_reader import FileReaderPlugin
from plugins.claude_chat import ClaudeChatPlugin
from plugins.paper_daily import PaperDailyPlugin
from plugins.claude_code.workspace_plugin import WorkspaceClaudeCodePlugin as ClaudeCodePlugin
from plugins.pomodoro import PomodoroPlugin


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    cfg = load_config()
    bot = HubBot(cfg["app_id"], cfg["app_secret"])

    # 注册插件（新增功能在此添加）
    bot.register_all([
        RPSPlugin(),
        FileReaderPlugin(),
        ClaudeChatPlugin(),
        PaperDailyPlugin(),
        ClaudeCodePlugin(),
        PomodoroPlugin(),
    ])

    # 注册 SIGTERM 处理器：将 SIGTERM 转为正常退出，确保 atexit 清理逻辑被触发
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    bot.start()


if __name__ == "__main__":
    main()
