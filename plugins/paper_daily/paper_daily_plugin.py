"""
论文日报插件

从 ArXiv 获取最新论文，通过 Gemini LLM 筛选并推荐与研究兴趣相关的论文，
生成中文摘要，以飞书卡片形式推送结果。支持用户订阅每日定时推送。

使用 Gemini /v1beta/ API 端点（无客户端检测限制）。
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
from .processor import process_papers

logger = logging.getLogger(__name__)

PLUGIN_KEYWORD = "论文日报"
_PLUGIN_DIR = Path(__file__).parent
_SUBSCRIBERS_PATH = _PLUGIN_DIR / "subscribers.json"
_SETTINGS_PATH = _PLUGIN_DIR / "settings.json"
_BEIJING_TZ = timezone(timedelta(hours=8))


class PaperDailyPlugin(Plugin):
    """论文日报插件：ArXiv 论文获取 + Gemini 筛选 + 飞书卡片推送。"""

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
        return "获取 ArXiv 最新论文，Gemini AI 筛选并生成中文摘要推送"

    # ---- 生命周期 ----

    def on_register(self, bot) -> None:
        super().on_register(bot)
        self._start_scheduler()

    # ---- 配置 ----

    def _load_plugin_config(self) -> dict:
        """懒加载插件配置（config/paper_daily.yaml）。"""
        if self._config is None:
            raw = load_plugin_config("paper_daily")
            self._config = {
                "research_interest": raw.get("research_interest", ""),
                "categories": raw.get("categories", ["cs.CL", "cs.AI", "cs.LG"]),
                "max_papers": raw.get("max_papers", 200),
                "llm_base_url": raw.get("llm_base_url", "https://api.lingyaai.cn"),
                "llm_api_key": raw.get("llm_api_key", ""),
                "llm_model": raw.get("llm_model", "gemini-3.1-pro-preview-thinking"),
                "schedule_time": raw.get("schedule_time", "09:00"),
            }
        return self._config

    def _build_app_config(self) -> AppConfig:
        cfg = self._load_plugin_config()
        return AppConfig(
            research_interest=self._get_research_interest(),
            categories=cfg["categories"],
            max_papers=cfg["max_papers"],
            llm_base_url=cfg["llm_base_url"],
            llm_api_key=cfg["llm_api_key"],
            llm_model=cfg["llm_model"],
            schedule_time=cfg["schedule_time"],
        )

    # ---- 用户状态 ----

    def _get_state(self, user_id: str) -> dict:
        if user_id not in self.user_states:
            self.user_states[user_id] = {"active": False, "running": False}
        return self.user_states[user_id]

    def is_user_active(self, user_id: str, chat_id: str = "") -> bool:
        return self._get_state(user_id).get("active", False)

    def deactivate_user(self, user_id: str, chat_id: str = "") -> None:
        self.user_states.pop(user_id, None)

    # ---- 订阅管理 ----

    def _load_subscribers(self) -> dict[str, str]:
        """加载订阅者列表，返回 {user_id: chat_id}。"""
        if not _SUBSCRIBERS_PATH.exists():
            return {}
        try:
            return json.loads(_SUBSCRIBERS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_subscribers(self, subscribers: dict[str, str]) -> None:
        try:
            _SUBSCRIBERS_PATH.write_text(
                json.dumps(subscribers, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as e:
            logger.error(f"保存订阅者列表失败: {e}")

    def _subscribe(self, user_id: str, chat_id: str) -> None:
        subs = self._load_subscribers()
        subs[user_id] = chat_id
        self._save_subscribers(subs)
        logger.info(f"用户订阅: user={user_id} chat={chat_id}")

    def _unsubscribe(self, user_id: str) -> None:
        subs = self._load_subscribers()
        if user_id in subs:
            subs.pop(user_id)
            self._save_subscribers(subs)
            logger.info(f"用户取消订阅: user={user_id}")

    def _is_subscribed(self, user_id: str) -> bool:
        return user_id in self._load_subscribers()

    # ---- 设置持久化 ----

    @staticmethod
    def _load_settings() -> dict:
        """加载用户设置（settings.json），不存在时返回空字典。"""
        if not _SETTINGS_PATH.exists():
            return {}
        try:
            return json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    @staticmethod
    def _save_settings(settings: dict) -> None:
        try:
            _SETTINGS_PATH.write_text(
                json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as e:
            logger.error(f"保存设置失败: {e}")

    def _get_research_interest(self) -> str:
        """获取当前研究兴趣：优先读 settings.json，回退到 YAML 配置。"""
        settings = self._load_settings()
        if "research_interest" in settings:
            return settings["research_interest"]
        return self._load_plugin_config()["research_interest"]

    # ---- 定时推送 ----

    def _start_scheduler(self) -> None:
        """启动定时推送调度器（守护线程）。"""
        if self._scheduler_started:
            return
        cfg = self._load_plugin_config()
        schedule_time = cfg.get("schedule_time", "09:00")

        # 将北京时间转换为本地时间（假设服务器运行在 UTC）
        # 北京时间 09:00 = UTC 01:00
        try:
            hour, minute = map(int, schedule_time.split(":"))
            beijing_dt = datetime.now(_BEIJING_TZ).replace(hour=hour, minute=minute, second=0, microsecond=0)
            utc_dt = beijing_dt.astimezone(timezone.utc)
            local_time = f"{utc_dt.hour:02d}:{utc_dt.minute:02d}"
            schedule.every().day.at(local_time).do(self._scheduled_push)
            logger.info(f"论文日报: 定时推送已注册，北京时间 {schedule_time} (本地时间 {local_time})")
        except (ValueError, AttributeError) as e:
            logger.error(f"定时推送配置错误: schedule_time={schedule_time}, error={e}")
            return

        t = threading.Thread(target=self._run_scheduler, daemon=True, name="paper-daily-scheduler")
        t.start()
        self._scheduler_started = True

    def _run_scheduler(self) -> None:
        while True:
            schedule.run_pending()
            logger.debug("论文日报: 定时调度器心跳（每30s）")
            time.sleep(30)

    def _scheduled_push(self) -> None:
        """定时推送：对所有订阅者执行完整流水线并推送结果。

        周末（周六/周日）ArXiv 不发布新论文，跳过推送避免重复。
        """
        # 周末跳过：ArXiv 周末无新论文，周一会统一推送
        weekday = datetime.now(_BEIJING_TZ).weekday()  # 0=周一, 5=周六, 6=周日
        if weekday in (5, 6):
            logger.info(f"论文日报定时推送: 周末跳过（weekday={weekday}）")
            return

        subscribers = self._load_subscribers()
        if not subscribers:
            logger.info("论文日报定时推送: 无订阅者，跳过")
            return

        logger.info(f"论文日报定时推送开始: {len(subscribers)} 个订阅者")
        try:
            cfg = self._build_app_config()
            papers = fetch_papers(cfg)
            if not papers:
                logger.info("论文日报定时推送: 今日论文尚未更新，跳过")
                return

            papers = papers[:cfg.max_papers]
            relevant = process_papers(papers, cfg)

            if not relevant:
                logger.info("论文日报定时推送: 筛选后无推荐论文")
                return

            today = datetime.now(_BEIJING_TZ)
            card = self._build_result_card(relevant, today, len(papers))

            for user_id, chat_id in subscribers.items():
                try:
                    self.bot.reply_card(chat_id, card)
                    logger.info(f"论文日报定时推送成功: user={user_id}")
                except Exception as e:
                    logger.error(f"论文日报定时推送失败: user={user_id} error={e}")

        except Exception as e:
            logger.error(f"论文日报定时推送流水线异常: {e}", exc_info=True)

    # ---- 流水线 ----

    def _start_fetch(self, user_id: str, chat_id: str) -> None:
        """启动后台获取流程（幂等，同一用户不重复启动）。"""
        state = self._get_state(user_id)
        if state.get("running"):
            self.bot.reply(chat_id, "正在获取中，请稍候...")
            return

        state["running"] = True
        placeholder = self._build_progress_card("正在连接 ArXiv，请稍候...")
        message_id = self.bot.send_message_get_id(
            chat_id, "interactive", json.dumps(placeholder)
        )
        t = threading.Thread(
            target=self._run_pipeline,
            args=(user_id, chat_id, message_id),
            daemon=True,
            name=f"paper-daily-{user_id[:8]}",
        )
        t.start()
        self._running_tasks[user_id] = t

    def _run_pipeline(self, user_id: str, chat_id: str, message_id: Optional[str]) -> None:
        """后台线程：完整执行获取→筛选→推送流水线。"""
        state = self._get_state(user_id)
        try:
            cfg = self._build_app_config()

            # 1. 获取论文
            self._patch_progress(message_id, "正在从 ArXiv 获取论文...")
            papers = fetch_papers(cfg)
            if not papers:
                self._patch_progress(message_id, "今日论文尚未更新，请稍后再试。")
                return

            total_fetched = min(len(papers), cfg.max_papers)
            papers = papers[:cfg.max_papers]
            logger.info(f"流水线: 共获取 {len(papers)} 篇（限制 {cfg.max_papers}）")

            self._patch_progress(
                message_id,
                f"已获取 **{total_fetched}** 篇论文，正在 Gemini AI 筛选中...\n\n"
                f"（每 10 篇一批，请耐心等待）"
            )

            # 2. 筛选 + 摘要
            relevant = process_papers(
                papers, cfg,
                progress_callback=lambda msg: self._patch_progress(message_id, msg),
            )

            # 3. 展示结果
            if not relevant:
                self._patch_progress(
                    message_id,
                    f"筛选完成，今日 {total_fetched} 篇论文中**无推荐论文**。"
                )
                return

            today = datetime.now(_BEIJING_TZ)
            result_card = self._build_result_card(relevant, today, total_fetched)
            if message_id:
                self.bot.patch_message(message_id, json.dumps(result_card))
            else:
                self.bot.reply_card(chat_id, result_card)

        except Exception as e:
            logger.error(f"论文日报流水线异常: user={user_id} error={e}", exc_info=True)
            self._patch_progress(message_id, f"日报生成失败，请稍后重试。\n\n错误：{e}")
        finally:
            state["running"] = False
            self._running_tasks.pop(user_id, None)

    # ---- 卡片构建 ----

    def _build_welcome_card(self, user_id: str) -> dict:
        """欢迎/菜单卡片：显示配置信息和操作按钮。"""
        cfg = self._load_plugin_config()
        # 截取研究兴趣的前 80 字做展示
        interest = self._get_research_interest().strip().replace("\n", " ")
        interest_short = interest[:80] + "..." if len(interest) > 80 else interest
        cats_str = ", ".join(cfg["categories"])
        schedule_time = cfg.get("schedule_time", "09:00")
        subscribed = self._is_subscribed(user_id)

        body = (
            f"**研究兴趣：** {interest_short}\n"
            f"**ArXiv 分类：** {cats_str}\n"
            f"**定时推送：** 每日 {schedule_time}（{'已订阅 ✓' if subscribed else '未订阅'}）\n\n"
            f"选择操作："
        )

        sub_btn = {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "取消订阅" if subscribed else "订阅每日推送"},
            "type": "default",
            "value": {"action": "unsubscribe" if subscribed else "subscribe", "plugin": PLUGIN_KEYWORD},
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
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "修改研究兴趣"},
                            "type": "default",
                            "value": {"action": "edit_interest", "plugin": PLUGIN_KEYWORD},
                        },
                    ],
                },
            ],
        }

    def _build_edit_interest_card(self) -> dict:
        """编辑研究兴趣的表单卡片：输入框预填当前值 + 保存按钮。"""
        current = self._get_research_interest()
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "修改研究兴趣"},
                "template": "blue",
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        "用自然语言描述你的研究兴趣，LLM 会据此判断论文相关性。\n"
                        "可包含：关注的方向、具体技术、不感兴趣的方向等。"
                    ),
                },
                {"tag": "hr"},
                {
                    "tag": "form",
                    "name": "edit_interest_form",
                    "elements": [
                        {
                            "tag": "input",
                            "name": "research_interest",
                            "input_type": "multiline_text",
                            "width": "fill",
                            "placeholder": {
                                "tag": "plain_text",
                                "content": "描述你的研究兴趣…",
                            },
                            "default_value": current,
                            "rows": 6,
                            "max_length": 1000,
                        },
                        {
                            "tag": "button",
                            "name": "submit_interest",
                            "text": {"tag": "plain_text", "content": "保存"},
                            "type": "primary",
                            "form_action_type": "submit",
                            "value": {
                                "action": "save_interest",
                                "plugin": PLUGIN_KEYWORD,
                            },
                        },
                    ],
                },
            ],
        }

    @staticmethod
    def _build_progress_card(body_md: str) -> dict:
        """进度/状态卡片。"""
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "论文日报 - 处理中"},
                "template": "blue",
            },
            "elements": [{"tag": "markdown", "content": body_md}],
        }

    def _patch_progress(self, message_id: Optional[str], text: str) -> None:
        """更新已发送的进度卡片内容。"""
        if not message_id:
            return
        try:
            self.bot.patch_message(message_id, json.dumps(self._build_progress_card(text)))
        except Exception as e:
            logger.warning(f"进度卡片更新失败: {e}")

    def _build_result_card(
        self, relevant: list, today: datetime, total_fetched: int
    ) -> dict:
        """结果卡片：按 score 倒序平铺展示推荐论文，每篇附带标签。"""
        date_str = today.strftime("%Y-%m-%d")

        elements = []

        # 标题摘要行
        elements.append({
            "tag": "markdown",
            "content": (
                f"**日期：** {date_str}　"
                f"**共筛选：** {total_fetched} 篇　"
                f"**推荐：** {len(relevant)} 篇"
            ),
        })
        elements.append({"tag": "hr"})

        # 按 score 倒序平铺展示，最多 20 篇
        display_papers = relevant[:20]
        for paper in display_papers:
            elements.extend(self._paper_elements(paper))
            elements.append({"tag": "hr"})

        if len(relevant) > 20:
            elements.append({
                "tag": "markdown",
                "content": f"还有 {len(relevant) - 20} 篇推荐论文未展示。",
            })

        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"论文日报 {date_str}"},
                "template": "green",
            },
            "elements": elements,
        }

    @staticmethod
    def _paper_elements(paper) -> list[dict]:
        """生成单篇论文的飞书卡片元素列表。"""
        authors_str = "、".join(paper.authors[:3])
        if len(paper.authors) > 3:
            authors_str += f" 等{len(paper.authors)}人"

        tags_str = ""
        if paper.tags:
            tags_str = f"\n标签：{' | '.join(paper.tags)}"

        content = (
            f"**[{paper.title}]({paper.entry_url})**\n"
            f"{authors_str}\n"
            f"推荐度：{'★' * paper.relevance_score}{'☆' * (5 - paper.relevance_score)}"
            f"　{paper.relevance_reason}"
            f"{tags_str}\n\n"
            f"{paper.summary_zh or '（无摘要）'}"
        )
        return [{"tag": "markdown", "content": content}]

    # ---- Plugin 接口 ----

    def handle_message(self, user_id: str, chat_id: str, text: str) -> None:
        state = self._get_state(user_id)

        if text == self.keyword:
            state["active"] = True
            self.bot.reply_card(chat_id, self._build_welcome_card(user_id))
            return

        if text in ("获取日报", "获取论文", "日报"):
            self._start_fetch(user_id, chat_id)
            return

        if text in ("订阅", "订阅推送", "订阅每日推送"):
            self._subscribe(user_id, chat_id)
            self.bot.reply(chat_id, "订阅成功！每日定时为你推送论文日报。")
            return

        if text in ("取消订阅",):
            self._unsubscribe(user_id)
            self.bot.reply(chat_id, "已取消订阅。")
            return

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

        # 表单提交时 action 可能为空，从 _form_value 判断
        if not action and "_form_value" in action_value:
            form_value = action_value["_form_value"]
            if "research_interest" in form_value:
                return self._handle_save_interest(
                    chat_id, form_value.get("research_interest", "")
                )

        if action == "fetch":
            self._start_fetch(user_id, chat_id)
        elif action == "subscribe":
            self._subscribe(user_id, chat_id)
            return self.bot.make_card_response(toast="订阅成功！将在每日定时为你推送论文日报。")
        elif action == "unsubscribe":
            self._unsubscribe(user_id)
            return self.bot.make_card_response(toast="已取消订阅。")
        elif action == "edit_interest":
            self.bot.reply_card(chat_id, self._build_edit_interest_card())
        elif action == "save_interest":
            return self._handle_save_interest(
                chat_id, action_value.get("_form_value", {}).get("research_interest", "")
            )

        return P2CardActionTriggerResponse()

    def _handle_save_interest(self, chat_id: str, new_interest: str) -> P2CardActionTriggerResponse:
        """保存用户修改的研究兴趣。"""
        new_interest = new_interest.strip()
        if not new_interest:
            return self.bot.make_card_response(toast="研究兴趣不能为空", toast_type="error")

        settings = self._load_settings()
        settings["research_interest"] = new_interest
        self._save_settings(settings)
        logger.info(f"研究兴趣已更新: {new_interest[:50]}...")
        return self.bot.make_card_response(toast="研究兴趣已保存，下次获取日报时生效。")
