"""
论文日报插件

从 ArXiv 获取最新论文，通过 LLM 筛选与关注主题相关的论文，
生成中文摘要，以飞书卡片形式推送结果。支持用户订阅每日定时推送。
"""

import json
import logging
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import schedule

from config import load_plugin_config
from core.plugin import Plugin
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)

from .config import AppConfig
from .fetcher import fetch_papers
from .llm_client import LLMClient
from .notifier import build_feishu_card_content, select_per_topic, count_per_topic
from .processor import process_papers
from .reporter import generate_reports

logger = logging.getLogger(__name__)

PLUGIN_KEYWORD = "论文日报"

_PLUGIN_DIR = Path(__file__).parent
_SUBSCRIBERS_PATH = _PLUGIN_DIR / "subscribers.json"

_BEIJING_TZ = timezone(timedelta(hours=8))


class PaperDailyPlugin(Plugin):
    """论文日报插件"""

    def __init__(self):
        super().__init__()
        self.user_states: dict[str, dict] = {}
        self._config: Optional[dict] = None
        self._running_tasks: dict[str, threading.Thread] = {}
        self._scheduler_started = False

    # ---- 元信息 ----

    @property
    def name(self) -> str:
        return "论文日报"

    @property
    def keyword(self) -> str:
        return PLUGIN_KEYWORD

    @property
    def description(self) -> str:
        return "获取 ArXiv 最新论文，AI 筛选并生成中文摘要推送"

    # ---- 生命周期 ----

    def on_register(self, bot) -> None:
        """注册时启动定时推送调度器"""
        super().on_register(bot)
        self._start_scheduler()

    # ---- 配置 ----

    def _load_plugin_config(self) -> dict:
        """懒加载插件配置，从 config/paper_daily.yaml 读取"""
        if self._config is None:
            pd = load_plugin_config("paper_daily")
            self._config = {
                "topics": pd.get("topics", []),
                "categories": pd.get("categories", ["cs.CL", "cs.AI", "cs.LG"]),
                "max_papers": pd.get("max_papers", 50),
                "llm_base_url": pd.get("llm_base_url", ""),
                "llm_api_key": pd.get("llm_api_key", ""),
                "llm_model": pd.get("llm_model", "claude-opus-4-6"),
                "schedule_time": pd.get("schedule_time", "10:00"),
            }
        return self._config

    def _build_app_config(self) -> AppConfig:
        """从插件配置构造 AppConfig 对象，供内部模块使用"""
        cfg = self._load_plugin_config()
        return AppConfig(
            topics=cfg["topics"],
            categories=cfg["categories"],
            max_papers=cfg["max_papers"],
            feishu_webhook_url="",
            llm_base_url=cfg["llm_base_url"],
            llm_api_key=cfg["llm_api_key"],
            llm_model=cfg["llm_model"],
        )

    # ---- 用户状态 ----

    def _get_state(self, user_id: str) -> dict:
        if user_id not in self.user_states:
            self.user_states[user_id] = {"active": False, "running": False}
        return self.user_states[user_id]

    def is_user_active(self, user_id: str) -> bool:
        return self._get_state(user_id).get("active", False)

    def deactivate_user(self, user_id: str) -> None:
        self.user_states.pop(user_id, None)

    # ---- 订阅管理 ----

    def _load_subscribers(self) -> dict[str, str]:
        """加载订阅者列表，返回 {user_id: chat_id}"""
        if not _SUBSCRIBERS_PATH.exists():
            return {}
        try:
            with open(_SUBSCRIBERS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_subscribers(self, subscribers: dict[str, str]) -> None:
        """保存订阅者列表"""
        try:
            with open(_SUBSCRIBERS_PATH, "w", encoding="utf-8") as f:
                json.dump(subscribers, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.error(f"保存订阅者列表失败: {e}")

    def _subscribe(self, user_id: str, chat_id: str) -> None:
        """添加订阅"""
        subscribers = self._load_subscribers()
        subscribers[user_id] = chat_id
        self._save_subscribers(subscribers)

    def _unsubscribe(self, user_id: str) -> None:
        """取消订阅"""
        subscribers = self._load_subscribers()
        subscribers.pop(user_id, None)
        self._save_subscribers(subscribers)

    def _is_subscribed(self, user_id: str) -> bool:
        """检查是否已订阅"""
        return user_id in self._load_subscribers()

    # ---- 定时推送 ----

    def _start_scheduler(self) -> None:
        """启动定时推送调度器（守护线程）"""
        if self._scheduler_started:
            return
        cfg = self._load_plugin_config()
        schedule_time = cfg.get("schedule_time", "10:00")

        schedule.every().day.at(schedule_time).do(self._scheduled_push)
        logger.info(f"论文日报: 已注册每日定时推送，时间 {schedule_time}")

        t = threading.Thread(target=self._run_scheduler, daemon=True)
        t.start()
        self._scheduler_started = True

    def _run_scheduler(self) -> None:
        """调度器循环"""
        while True:
            schedule.run_pending()
            time.sleep(30)

    def _scheduled_push(self) -> None:
        """定时推送：执行流水线，向所有订阅者发送结果"""
        subscribers = self._load_subscribers()
        if not subscribers:
            logger.info("论文日报: 无订阅者，跳过定时推送")
            return

        logger.info(f"论文日报: 开始定时推送，共 {len(subscribers)} 个订阅者")

        try:
            app_config = self._build_app_config()
            papers = fetch_papers(app_config)
            if not papers:
                logger.info("论文日报: 定时推送 - 今日论文可能还未更新，跳过")
                return

            papers.sort(key=lambda p: p.published, reverse=True)
            if len(papers) > app_config.max_papers:
                papers = papers[:app_config.max_papers]

            llm = LLMClient(app_config)
            try:
                relevant = process_papers(papers, app_config, llm)
            finally:
                llm.close()

            if not relevant:
                logger.info("论文日报: 定时推送 - 筛选后无相关论文")
                return

            today = datetime.now(timezone.utc)
            generate_reports(
                relevant, today, topics=app_config.topics,
                template_dir=str(_PLUGIN_DIR / "templates"),
                output_base=str(_PLUGIN_DIR / "reports"),
            )

            selected = select_per_topic(relevant, app_config.topics)
            remaining = len(relevant) - len(selected)
            topic_counts = count_per_topic(relevant, app_config.topics)
            card = build_feishu_card_content(
                selected, today,
                total=len(relevant),
                remaining=remaining,
                topic_counts=topic_counts,
            )

            for user_id, chat_id in subscribers.items():
                try:
                    self.bot.reply_card(chat_id, card)
                    logger.info(f"论文日报: 定时推送成功 user={user_id}")
                except Exception as e:
                    logger.error(f"论文日报: 定时推送失败 user={user_id}: {e}")

        except Exception as e:
            logger.error(f"论文日报: 定时推送流水线失败: {e}", exc_info=True)

    # ---- 卡片构建 ----

    def _build_welcome_card(self, user_id: str) -> dict:
        """构造欢迎/激活卡片"""
        cfg = self._load_plugin_config()
        topics_str = "、".join(cfg["topics"]) if cfg["topics"] else "未配置"
        cats_str = ", ".join(cfg["categories"])
        schedule_time = cfg.get("schedule_time", "10:00")
        subscribed = self._is_subscribed(user_id)
        sub_status = "已订阅" if subscribed else "未订阅"

        body = (
            f"**关注主题:** {topics_str}\n"
            f"**ArXiv 分类:** {cats_str}\n"
            f"**定时推送:** 每日 {schedule_time}（{sub_status}）\n\n"
            f"点击按钮或发送指令操作："
        )

        # 订阅按钮根据状态切换
        if subscribed:
            sub_btn = {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "取消订阅"},
                "type": "default",
                "value": {"action": "unsubscribe", "plugin": PLUGIN_KEYWORD},
            }
        else:
            sub_btn = {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "订阅每日推送"},
                "type": "default",
                "value": {"action": "subscribe", "plugin": PLUGIN_KEYWORD},
            }

        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "论文日报"},
                "template": "blue",
            },
            "elements": [
                {"tag": "markdown", "content": body},
                {"tag": "hr"},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "获取今日日报"},
                            "type": "primary",
                            "value": {"action": "fetch", "plugin": PLUGIN_KEYWORD},
                        },
                        sub_btn,
                    ],
                },
            ],
        }

    @staticmethod
    def _build_progress_card(body_md: str) -> dict:
        """构造进度卡片"""
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "论文日报 - 处理中"},
                "template": "blue",
            },
            "elements": [
                {"tag": "markdown", "content": body_md},
            ],
        }

    def _patch_progress(self, message_id: str, text: str) -> None:
        """更新进度卡片内容"""
        card = self._build_progress_card(text)
        try:
            self.bot.patch_message(message_id, json.dumps(card))
        except Exception as e:
            logger.warning(f"进度消息更新失败: {e}")

    # ---- 获取流水线 ----

    def _start_fetch(self, user_id: str, chat_id: str) -> None:
        """启动获取流程"""
        state = self._get_state(user_id)
        if state.get("running"):
            self.bot.reply(chat_id, "正在获取中，请稍候...")
            return

        state["running"] = True

        # 发送占位卡片
        placeholder = self._build_progress_card("正在连接 ArXiv，请稍候...")
        message_id = self.bot.send_message_get_id(
            chat_id, "interactive", json.dumps(placeholder)
        )

        t = threading.Thread(
            target=self._run_pipeline,
            args=(user_id, chat_id, message_id),
            daemon=True,
        )
        t.start()
        self._running_tasks[user_id] = t

    def _run_pipeline(
        self, user_id: str, chat_id: str, message_id: Optional[str]
    ) -> None:
        """在后台线程执行完整流水线"""
        state = self._get_state(user_id)

        try:
            app_config = self._build_app_config()

            # 1. 获取论文
            if message_id:
                self._patch_progress(message_id, "正在从 ArXiv 获取论文...")
            papers = fetch_papers(app_config)
            if not papers:
                self._send_result(
                    chat_id, message_id,
                    "今日论文可能还未更新，请稍后再试。"
                )
                return

            papers.sort(key=lambda p: p.published, reverse=True)
            if len(papers) > app_config.max_papers:
                papers = papers[:app_config.max_papers]

            if message_id:
                self._patch_progress(
                    message_id,
                    f"获取到 **{len(papers)}** 篇论文，正在 AI 筛选中...\n\n"
                    f"（此过程需要一些时间，请耐心等待）"
                )

            # 2. LLM 筛选 + 摘要
            llm = LLMClient(app_config)
            try:
                relevant = process_papers(
                    papers, app_config, llm,
                    progress_callback=lambda msg: (
                        self._patch_progress(message_id, msg) if message_id else None
                    ),
                )
            finally:
                llm.close()

            if not relevant:
                self._send_result(
                    chat_id, message_id, "筛选完成，今日无相关论文。"
                )
                return

            # 3. 生成报告
            today = datetime.now(timezone.utc)
            generate_reports(
                relevant, today, topics=app_config.topics,
                template_dir=str(_PLUGIN_DIR / "templates"),
                output_base=str(_PLUGIN_DIR / "reports"),
            )

            # 4. 构造结果卡片
            selected = select_per_topic(relevant, app_config.topics)
            remaining = len(relevant) - len(selected)
            topic_counts = count_per_topic(relevant, app_config.topics)
            result_card = build_feishu_card_content(
                selected, today,
                total=len(relevant),
                remaining=remaining,
                topic_counts=topic_counts,
            )

            if message_id:
                self.bot.patch_message(message_id, json.dumps(result_card))
            else:
                self.bot.reply_card(chat_id, result_card)

        except Exception as e:
            logger.error(f"论文日报流水线失败: {e}", exc_info=True)
            self._send_result(
                chat_id, message_id,
                f"日报生成失败，请稍后重试。\n\n错误信息: {e}"
            )
        finally:
            state["running"] = False
            self._running_tasks.pop(user_id, None)

    def _send_result(
        self, chat_id: str, message_id: Optional[str], text: str
    ) -> None:
        """发送结果文本（更新占位卡片或直接发送）"""
        if message_id:
            self._patch_progress(message_id, text)
        else:
            self.bot.reply(chat_id, text)

    # ---- Plugin 接口 ----

    def handle_message(self, user_id: str, chat_id: str, text: str) -> None:
        state = self._get_state(user_id)

        # 关键词激活
        if text == self.keyword:
            state["active"] = True
            card = self._build_welcome_card(user_id)
            self.bot.reply_card(chat_id, card)
            return

        # 获取日报
        if text in ("获取日报", "获取论文", "日报"):
            self._start_fetch(user_id, chat_id)
            return

        # 订阅
        if text in ("订阅", "订阅推送", "订阅每日推送"):
            self._subscribe(user_id, chat_id)
            self.bot.reply(chat_id, "订阅成功！将在每日定时为你推送论文日报。")
            return

        # 取消订阅
        if text in ("取消订阅",):
            self._unsubscribe(user_id)
            self.bot.reply(chat_id, "已取消订阅。")
            return

        # 未识别指令
        self.bot.reply(
            chat_id,
            "可用指令：\n"
            "- 发送「获取日报」获取今日论文\n"
            "- 发送「订阅」订阅每日推送\n"
            "- 发送「取消订阅」取消订阅\n"
            "- 发送「退出」返回主菜单"
        )

    def handle_card_action(
        self, user_id: str, chat_id: str, message_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        action = action_value.get("action", "")

        if action == "fetch":
            self._start_fetch(user_id, chat_id)
        elif action == "subscribe":
            self._subscribe(user_id, chat_id)
            return self.bot.make_card_response(
                toast="订阅成功！将在每日定时为你推送论文日报。"
            )
        elif action == "unsubscribe":
            self._unsubscribe(user_id)
            return self.bot.make_card_response(toast="已取消订阅。")

        return P2CardActionTriggerResponse()
