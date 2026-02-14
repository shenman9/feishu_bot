"""
飞书机器人启动入口
"""

from config import load_config
from core.hub_bot import HubBot
from plugins.rps_game import RPSPlugin


def main():
    cfg = load_config()
    bot = HubBot(cfg["app_id"], cfg["app_secret"])

    # 注册插件（新增功能在此添加）
    bot.register_all([
        RPSPlugin(),
    ])

    bot.start()


if __name__ == "__main__":
    main()
