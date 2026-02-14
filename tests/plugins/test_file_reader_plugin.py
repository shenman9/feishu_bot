"""
文件阅读插件单元测试
"""

import pytest
from unittest.mock import MagicMock

from plugins.file_reader.file_reader_plugin import (
    FileReaderPlugin,
    MAX_FILE_SIZE,
    PLUGIN_KEYWORD,
)


class TestFileReaderPlugin:
    """FileReaderPlugin 核心逻辑测试"""

    @pytest.fixture
    def plugin(self):
        p = FileReaderPlugin()
        bot = MagicMock()
        p.on_register(bot)
        return p

    def test_properties(self, plugin):
        """插件元信息正确"""
        assert plugin.name == "文件阅读"
        assert plugin.keyword == PLUGIN_KEYWORD
        assert plugin.description

    def test_keyword_activates(self, plugin):
        """发送关键词激活插件"""
        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        assert plugin.is_user_active("u1") is True
        plugin.bot.reply.assert_called_once()
        # 无历史文件时的欢迎提示
        msg = plugin.bot.reply.call_args[0][1]
        assert "已启动" in msg

    def test_keyword_shows_file_list(self, plugin):
        """有历史文件时激活显示文件列表"""
        state = plugin._get_state("u1")
        state["files"] = [{"name": "a.txt", "content": "aaa"}]
        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        msg = plugin.bot.reply.call_args[0][1]
        assert "a.txt" in msg
        assert "1 个文件" in msg

    def test_upload_txt_returns_content(self, plugin):
        """上传 txt 文件返回内容"""
        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        plugin.bot.reply.reset_mock()
        plugin.bot.download_file.return_value = "你好世界".encode("utf-8")

        plugin.handle_file_message("u1", "c1", "msg1", "fk1", "hello.txt")

        plugin.bot.reply.assert_called_once()
        msg = plugin.bot.reply.call_args[0][1]
        assert "你好世界" in msg
        assert "hello.txt" in msg
        # 文件已存储
        assert len(plugin._get_state("u1")["files"]) == 1

    def test_reject_non_txt(self, plugin):
        """拒绝非 txt 文件"""
        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        plugin.bot.reply.reset_mock()

        plugin.handle_file_message("u1", "c1", "msg1", "fk1", "photo.png")

        msg = plugin.bot.reply.call_args[0][1]
        assert "仅支持 .txt" in msg
        assert len(plugin._get_state("u1")["files"]) == 0

    def test_reject_oversized_file(self, plugin):
        """拒绝超大文件"""
        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        plugin.bot.reply.reset_mock()
        plugin.bot.download_file.return_value = b"x" * (MAX_FILE_SIZE + 1)

        plugin.handle_file_message("u1", "c1", "msg1", "fk1", "big.txt")

        msg = plugin.bot.reply.call_args[0][1]
        assert "文件过大" in msg

    def test_number_shows_history(self, plugin):
        """发送数字查看历史文件"""
        state = plugin._get_state("u1")
        state["active"] = True
        state["files"] = [
            {"name": "a.txt", "content": "内容A"},
            {"name": "b.txt", "content": "内容B"},
            {"name": "c.txt", "content": "内容C"},
        ]

        plugin.handle_message("u1", "c1", "2")
        msg = plugin.bot.reply.call_args[0][1]
        assert "内容B" in msg
        assert "b.txt" in msg

    def test_invalid_number(self, plugin):
        """无效编号提示"""
        state = plugin._get_state("u1")
        state["active"] = True
        state["files"] = [{"name": "a.txt", "content": "aaa"}]

        plugin.handle_message("u1", "c1", "5")
        msg = plugin.bot.reply.call_args[0][1]
        assert "编号无效" in msg

    def test_download_failure(self, plugin):
        """下载失败时友好提示"""
        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        plugin.bot.reply.reset_mock()
        plugin.bot.download_file.side_effect = RuntimeError("网络错误")

        plugin.handle_file_message("u1", "c1", "msg1", "fk1", "err.txt")

        msg = plugin.bot.reply.call_args[0][1]
        assert "下载失败" in msg

    def test_deactivate_clears_state(self, plugin):
        """deactivate 清理用户状态"""
        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        plugin.deactivate_user("u1")
        assert "u1" not in plugin.user_states

    def test_gbk_encoding(self, plugin):
        """GBK 编码文件正常解码"""
        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        plugin.bot.reply.reset_mock()
        plugin.bot.download_file.return_value = "中文内容".encode("gbk")

        plugin.handle_file_message("u1", "c1", "msg1", "fk1", "gbk.txt")

        msg = plugin.bot.reply.call_args[0][1]
        assert "中文内容" in msg

    def test_multiple_uploads_and_query(self, plugin):
        """多次上传后按编号查看"""
        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        plugin.bot.download_file.return_value = "第一份".encode("utf-8")
        plugin.handle_file_message("u1", "c1", "m1", "fk1", "first.txt")
        plugin.bot.download_file.return_value = "第二份".encode("utf-8")
        plugin.handle_file_message("u1", "c1", "m2", "fk2", "second.txt")
        plugin.bot.download_file.return_value = "第三份".encode("utf-8")
        plugin.handle_file_message("u1", "c1", "m3", "fk3", "third.txt")

        plugin.bot.reply.reset_mock()
        plugin.handle_message("u1", "c1", "2")
        msg = plugin.bot.reply.call_args[0][1]
        assert "第二份" in msg
        assert "second.txt" in msg

    def test_unknown_text_shows_hint(self, plugin):
        """未知文本输入提示用户"""
        plugin.handle_message("u1", "c1", PLUGIN_KEYWORD)
        plugin.bot.reply.reset_mock()

        plugin.handle_message("u1", "c1", "随便说点什么")
        msg = plugin.bot.reply.call_args[0][1]
        assert "上传" in msg
