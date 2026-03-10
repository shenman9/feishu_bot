# 插件开发指南

本目录下每个子目录是一个独立插件。开发新插件只需关注本文件描述的接口契约，不需要了解外层代码。

## 快速开始

1. 在 `plugins/` 下新建目录，如 `plugins/my_feature/`
2. 创建 `my_plugin.py`，继承 `Plugin` 基类
3. 在 `__init__.py` 中导出你的插件类
4. 在根目录 `main.py` 中注册（一行代码）

## Plugin 基类接口

```python
from core.plugin import Plugin

class MyPlugin(Plugin):

    # ---- 必须实现 ----

    @property
    def name(self) -> str:
        """显示名称，出现在功能菜单中"""
        return "我的功能"

    @property
    def keyword(self) -> str:
        """触发关键词，用户发送此文本激活插件"""
        return "我的功能"

    @property
    def description(self) -> str:
        """一句话描述，出现在功能菜单中"""
        return "这是一个示例功能"

    def handle_message(self, user_id: str, chat_id: str, text: str) -> None:
        """
        处理文本消息。
        - 当用户发送 keyword 时，text == keyword（首次激活）
        - 之后用户发送的所有文本都会路由到这里，直到插件不再活跃
        """
        self.bot.reply(chat_id, f"你说了: {text}")

    # ---- 可选覆写 ----

    def handle_card_action(self, user_id, chat_id, message_id, action_value) -> P2CardActionTriggerResponse:
        """处理卡片按钮点击。不需要卡片交互的插件可以不实现。"""
        ...

    def handle_file_message(self, user_id, chat_id, message_id, file_key, file_name) -> None:
        """处理文件消息（用户上传文件时调用）。不需要文件处理的插件可以不实现。"""
        ...

    def is_user_active(self, user_id: str, chat_id: str = "") -> bool:
        """返回 True 表示用户仍在本插件会话中，后续消息继续路由到本插件。
        返回 False（默认）则每次消息处理完后自动退出插件。
        chat_id 用于跨群聊隔离——同一用户在不同群聊中可独立维护会话状态。"""
        return False

    def deactivate_user(self, user_id: str, chat_id: str = "") -> None:
        """用户退出插件时调用，用于清理会话状态。
        chat_id 用于跨群聊隔离，与 is_user_active 中的 chat_id 语义一致。"""
        pass
```

## self.bot 可用方法

插件通过 `self.bot` 调用消息发送能力：

| 方法 | 说明 |
|------|------|
| `self.bot.reply(chat_id, text)` | 发送文本消息 |
| `self.bot.reply_card(chat_id, card_dict)` | 发送交互卡片 |
| `self.bot.send_message(chat_id, msg_type, content)` | 发送任意类型消息 |
| `self.bot.send_message_get_id(chat_id, msg_type, content)` | 发送消息并返回 message_id |
| `self.bot.patch_message(message_id, content)` | 更新已发送的卡片内容（用于进度更新、流式响应等） |
| `self.bot.make_card_response(card=None, toast=None)` | 构造卡片按钮点击的响应（更新卡片/弹 toast） |
| `self.bot.download_file(message_id, file_key)` | 下载消息中的文件，返回二进制内容（失败时抛 RuntimeError） |

## 生命周期钩子

```python
def on_register(self, bot) -> None:
    """插件被注册到 HubBot 时调用。可用于初始化定时任务、后台线程等。
    务必先调用 super().on_register(bot) 以正确绑定 self.bot。"""
    super().on_register(bot)
    # 自定义初始化逻辑
```

## 卡片按钮路由

如果插件使用交互卡片，按钮的 `value` 中务必包含 `"plugin": "<你的keyword>"`，确保点击事件能正确路由：

```python
{
    "tag": "button",
    "text": {"tag": "plain_text", "content": "点我"},
    "value": {"action": "do_something", "plugin": "我的功能"}
}
```

## 用户交互流程

```
用户发送 keyword → HubBot 激活插件 → handle_message(text=keyword)
用户继续发消息 → HubBot 检查 is_user_active(user_id, chat_id) → True → handle_message(text)
用户发送"退出" → HubBot 调用 deactivate_user(user_id, chat_id) → 回到主菜单

注意：同一用户在不同群聊中的插件激活状态互相独立（按 (user_id, chat_id) 隔离）。
```

## 示例

| 插件 | 目录 | 特性参考 |
|------|------|---------|
| 石头剪刀布 | `rps_game/` | 基础交互、用户状态管理 |
| 文件阅读 | `file_reader/` | 文件上传处理 |
| Claude 对话 | `claude_chat/` | 流式响应、`patch_message` 实时更新卡片 |
| 论文日报 | `paper_daily/` | 后台线程、定时推送（`on_register` + `schedule`）、订阅管理、进度卡片 |
| Claude Code | `claude_code/` | subprocess 调用 CLI、`PreToolUse` Hook、飞书权限确认卡片交互 |
