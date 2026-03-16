"""
石头剪刀布游戏插件
"""

import random

from core.plugin import Plugin
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)

CHOICES = ["石头", "剪刀", "布"]
EMOJI = {"石头": "✊", "剪刀": "✌️", "布": "✋"}

OUTCOMES = {
    "石头": {"石头": "平局", "剪刀": "你输了", "布": "你赢了"},
    "剪刀": {"石头": "你赢了", "剪刀": "平局", "布": "你输了"},
    "布": {"石头": "你输了", "剪刀": "你赢了", "布": "平局"},
}
RESULT_EMOJI = {"你赢了": "🎉", "你输了": "😅", "平局": "🤝"}
RESULT_COLOR = {"你赢了": "green", "你输了": "red", "平局": "yellow"}

PLUGIN_KEYWORD = "石头剪刀布"


def _game_card(title: str, body_md: str, show_buttons: bool = True) -> dict:
    """构造游戏卡片，按钮 value 中嵌入 plugin 字段用于路由"""
    elements = [{"tag": "markdown", "content": body_md}]
    if show_buttons:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "action",
            "actions": [
                *({"tag": "button",
                   "text": {"tag": "plain_text", "content": f"{EMOJI[c]} {c}"},
                   "type": "primary",
                   "value": {"choice": c, "plugin": PLUGIN_KEYWORD}}
                  for c in CHOICES),
                {"tag": "button",
                 "text": {"tag": "plain_text", "content": "🚪 结束"},
                 "type": "danger",
                 "value": {"choice": "结束", "plugin": PLUGIN_KEYWORD}},
            ],
        })
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"},
        "elements": elements,
    }


class RPSPlugin(Plugin):
    """石头剪刀布游戏插件"""

    @property
    def name(self) -> str:
        return "石头剪刀布"

    @property
    def keyword(self) -> str:
        return PLUGIN_KEYWORD

    @property
    def description(self) -> str:
        return "和机器人猜拳，看谁更厉害"

    def __init__(self):
        super().__init__()
        self.user_states: dict = {}

    def _get_state(self, user_id: str) -> dict:
        if user_id not in self.user_states:
            self.user_states[user_id] = {"playing": False, "bot_choice": None}
        return self.user_states[user_id]

    def _start_round(self, state: dict) -> str:
        state["bot_choice"] = random.choice(CHOICES)
        return state["bot_choice"]

    def _play(self, chat_id: str, state: dict, choice: str) -> None:
        if choice == "结束":
            state["playing"] = False
            state["bot_choice"] = None
            card = _game_card(
                "✊✌️✋ 石头剪刀布",
                "游戏结束！感谢参与，下次再来玩吧～ 👋",
                show_buttons=False,
            )
            card["header"]["template"] = "grey"
            self.bot.reply_card(chat_id, card)
            return

        if choice in CHOICES:
            bot_choice = state["bot_choice"]
            result = OUTCOMES[bot_choice][choice]
            emoji = RESULT_EMOJI[result]
            color = RESULT_COLOR[result]
            self._start_round(state)
            body = (
                f"你出了 **{EMOJI[choice]} {choice}**　vs　"
                f"我出了 **{EMOJI[bot_choice]} {bot_choice}**\n\n"
                f"结果：**<font color='{color}'>{result} {emoji}</font>**"
                "\n\n---\n\n下一轮我又选好了，**你出啥？**"
            )
            self.bot.reply_card(chat_id, _game_card("✊✌️✋ 石头剪刀布", body))
            return

        self.bot.reply(
            chat_id, "请输入「石头」「剪刀」或「布」进行游戏，或输入「结束」退出游戏。"
        )

    # ---- Plugin 接口实现 ----

    def handle_message(self, user_id: str, chat_id: str, text: str,
                       message_id: str = "") -> None:
        state = self._get_state(user_id)

        if text == PLUGIN_KEYWORD:
            state["playing"] = True
            self._start_round(state)
            card = _game_card(
                "✊✌️✋ 石头剪刀布",
                "**欢迎来到石头剪刀布！**\n\n"
                "规则：我先随机选一个，你点按钮或直接输入文字出招，"
                "我来判定胜负。\n\n我已经选好了，**你出啥？**",
            )
            self.bot.reply_card(chat_id, card)
            return

        if state["playing"]:
            self._play(chat_id, state, text)
            return

        self.bot.reply(chat_id, "发送「石头剪刀布」开始游戏。")

    def handle_card_action(
        self, user_id: str, chat_id: str, message_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        state = self._get_state(user_id)
        choice = action_value.get("choice", "")

        if not state["playing"]:
            return self.bot.make_card_response(
                toast="当前没有进行中的游戏，发送「石头剪刀布」开始"
            )

        self._play(chat_id, state, choice)
        return P2CardActionTriggerResponse()

    def is_user_active(self, user_id: str, chat_id: str = "") -> bool:
        return self._get_state(user_id)["playing"]

    def deactivate_user(self, user_id: str, chat_id: str = "") -> None:
        self.user_states.pop(user_id, None)
