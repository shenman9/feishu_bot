"""
飞书机器人启动入口
"""

import logging

from config import load_config
from core.hub_bot import HubBot
from plugins.rps_game import RPSPlugin
from plugins.file_reader import FileReaderPlugin
from plugins.claude_chat import ClaudeChatPlugin
from plugins.paper_daily import PaperDailyPlugin


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
    ])

    bot.start()


if __name__ == "__main__":
    main()
