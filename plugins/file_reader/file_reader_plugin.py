"""
文件阅读插件
用户上传 txt 文件后读取内容并返回，支持通过数字查看历史上传的文件。
"""

from core.plugin import Plugin

# 单个文件最大大小限制
MAX_FILE_SIZE = 100 * 1024  # 100KB
ALLOWED_SUFFIX = ".txt"
PLUGIN_KEYWORD = "文件阅读"


class FileReaderPlugin(Plugin):
    """文件阅读插件"""

    def __init__(self):
        super().__init__()
        # user_id -> {"active": bool, "files": [{"name": str, "content": str}]}
        self.user_states: dict[str, dict] = {}

    @property
    def name(self) -> str:
        return "文件阅读"

    @property
    def keyword(self) -> str:
        return PLUGIN_KEYWORD

    @property
    def description(self) -> str:
        return "上传 txt 文件，机器人读取内容并返回"

    def _get_state(self, user_id: str) -> dict:
        if user_id not in self.user_states:
            self.user_states[user_id] = {"active": False, "files": []}
        return self.user_states[user_id]

    # ---- Plugin 接口实现 ----

    def handle_message(self, user_id: str, chat_id: str, text: str) -> None:
        state = self._get_state(user_id)

        # 关键词激活
        if text == self.keyword:
            state["active"] = True
            files = state["files"]
            if files:
                file_list = "\n".join(
                    f"  {i + 1}. {f['name']}" for i, f in enumerate(files)
                )
                self.bot.reply(
                    chat_id,
                    f"文件阅读功能已启动。\n"
                    f"已有 {len(files)} 个文件：\n{file_list}\n\n"
                    f"发送数字查看对应文件，或直接上传新的 txt 文件。",
                )
            else:
                self.bot.reply(
                    chat_id,
                    "文件阅读功能已启动。请上传 txt 文件，我会读取内容并返回。",
                )
            return

        # 数字查看历史文件
        if text.isdigit():
            idx = int(text) - 1
            files = state["files"]
            if 0 <= idx < len(files):
                f = files[idx]
                self.bot.reply(chat_id, f"📄 {f['name']}\n\n{f['content']}")
            else:
                self.bot.reply(
                    chat_id, f"编号无效，当前共 {len(files)} 个文件。"
                )
            return

        self.bot.reply(
            chat_id, "请上传 txt 文件，或发送数字查看已上传的文件内容。"
        )

    def handle_file_message(
        self, user_id: str, chat_id: str, message_id: str,
        file_key: str, file_name: str
    ) -> None:
        state = self._get_state(user_id)

        # 校验文件类型
        if not file_name.lower().endswith(ALLOWED_SUFFIX):
            self.bot.reply(chat_id, f"仅支持 .txt 文件，收到的是: {file_name}")
            return

        # 下载文件
        try:
            raw = self.bot.download_file(message_id, file_key)
        except Exception as e:
            self.bot.reply(chat_id, f"文件下载失败: {e}")
            return

        # 大小检查
        if len(raw) > MAX_FILE_SIZE:
            self.bot.reply(
                chat_id,
                f"文件过大（{len(raw) // 1024}KB），最大支持 {MAX_FILE_SIZE // 1024}KB。",
            )
            return

        # 解码文本内容
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                content = raw.decode("gbk")
            except UnicodeDecodeError:
                self.bot.reply(chat_id, "文件编码无法识别，仅支持 UTF-8 或 GBK。")
                return

        # 存储并返回内容
        state["files"].append({"name": file_name, "content": content})
        idx = len(state["files"])
        self.bot.reply(chat_id, f"📄 文件 #{idx}: {file_name}\n\n{content}")

    def is_user_active(self, user_id: str) -> bool:
        return self._get_state(user_id)["active"]

    def deactivate_user(self, user_id: str) -> None:
        self.user_states.pop(user_id, None)
