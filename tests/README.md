# 测试框架使用指南

## 快速开始

```bash
# 安装测试依赖
pip install -r requirements-dev.txt

# 运行全部测试
pytest

# 运行指定模块
pytest tests/core/
pytest tests/plugins/test_rps_plugin.py

# 运行匹配名称的用例
pytest -k "menu"
```

## 目录结构

```
tests/
├── conftest.py                # 全局夹具：mock lark_oapi、StubPlugin、mock_bot
├── core/                      # 核心框架测试
│   ├── test_config.py         # 配置加载
│   ├── test_hub_bot.py        # 消息路由、插件注册
│   └── test_plugin.py         # 插件基类
└── plugins/                   # 插件测试（每个插件一个文件）
    ├── test_claude_chat_plugin.py   # Claude 对话插件
    ├── test_claude_code_plugin.py   # Claude Code 桥接插件
    ├── test_file_reader_plugin.py   # 文件阅读插件
    ├── test_permission_server.py    # 权限确认 HTTP 服务器
    └── test_rps_plugin.py           # 石头剪刀布插件
```

## 核心机制：lark_oapi Mock

项目依赖飞书 SDK (`lark_oapi`)，但测试不应依赖真实网络连接。`conftest.py` 在所有模块导入之前，通过 `patch.dict("sys.modules")` 将 `lark_oapi` 替换为 `MagicMock`，使得整个项目代码可以在无 SDK 环境下被导入和测试。

**你不需要关心这个细节**——只要在测试文件中正常 import 项目模块即可。

## 可用的 Fixture

`conftest.py` 提供了三个开箱即用的 fixture：

| Fixture | 说明 |
|---|---|
| `mock_bot` | 一个 `HubBot` 实例，`reply()`、`reply_card()`、`send_message()` 均为 `MagicMock`，可直接断言调用 |
| `stub_plugin` | 一个最小化的 `StubPlugin` 实例，会把收到的消息记录到 `received_messages` 列表 |
| `bot_with_plugin` | 返回 `(mock_bot, stub_plugin)` 元组，插件已注册到 bot 上，适合测试路由逻辑 |

此外，`StubPlugin` 类可以直接 import 使用，支持自定义 `name`、`keyword`、`description`：

```python
from tests.conftest import StubPlugin

p = StubPlugin(name="自定义", keyword="custom", description="用于特殊场景")
```

## 为新插件编写测试

以一个假设的「天气查询」插件为例：

### 1. 创建测试文件

```
tests/plugins/test_weather_plugin.py
```

### 2. 编写测试

```python
"""天气查询插件测试"""

import pytest
from unittest.mock import MagicMock, patch

from plugins.weather import WeatherPlugin


class TestWeatherPlugin:

    @pytest.fixture
    def weather(self):
        """创建插件实例并注入 mock bot"""
        plugin = WeatherPlugin()
        bot = MagicMock()
        plugin.on_register(bot)
        return plugin

    def test_properties(self, weather):
        """插件元信息正确"""
        assert weather.name
        assert weather.keyword
        assert weather.description

    def test_keyword_triggers_plugin(self, weather):
        """发送关键词触发插件"""
        weather.handle_message("u1", "c1", weather.keyword)
        weather.bot.reply.assert_called()  # 或 reply_card

    def test_query_city(self, weather):
        """查询城市天气返回结果"""
        weather.handle_message("u1", "c1", weather.keyword)
        weather.handle_message("u1", "c1", "北京")
        # 断言 bot 发送了包含天气信息的回复
        weather.bot.reply.assert_called()

    def test_invalid_city(self, weather):
        """查询不存在的城市给出提示"""
        weather.handle_message("u1", "c1", weather.keyword)
        weather.handle_message("u1", "c1", "不存在的地方")
        call_args = weather.bot.reply.call_args[0]
        assert "找不到" in call_args[1] or "无法" in call_args[1]
```

### 3. 测试外部 API 调用

如果插件调用了外部 HTTP 接口，用 `patch` 隔离：

```python
def test_api_failure_handled(self, weather):
    """外部 API 异常时不崩溃，返回友好提示"""
    with patch("plugins.weather.weather_plugin.fetch_weather", side_effect=Exception("超时")):
        weather.handle_message("u1", "c1", "北京")
        call_args = weather.bot.reply.call_args[0]
        assert "出错" in call_args[1] or "稍后" in call_args[1]
```

## 测试 HubBot 路由集成

如果需要验证插件在 HubBot 路由中的行为，使用 `mock_bot` fixture：

```python
def test_plugin_integrates_with_hub(self, mock_bot):
    """插件注册后能通过关键词激活"""
    plugin = WeatherPlugin()
    mock_bot.register(plugin)

    mock_bot.on_message("u1", "c1", plugin.keyword)
    assert mock_bot.active_plugin.get(("u1", "c1")) == plugin.keyword
```

## 编写规范

1. **测试文件命名**：`test_<模块名>.py`
2. **测试类命名**：`Test<功能分组>`，如 `TestMenuRouting`、`TestGameCard`
3. **测试方法命名**：`test_<行为描述>`，如 `test_keyword_starts_game`
4. **每个测试方法必须有中文 docstring**，简要说明测试意图
5. **一个测试只验证一件事**，避免在单个用例中塞入多个不相关的断言
6. **插件测试不要 import 其他插件**，与生产代码保持一致的隔离原则

## 常用断言技巧

```python
# 验证 bot 发送了消息
bot.reply.assert_called_once()
bot.reply.assert_called_with("chat1", "期望的文本")

# 验证 bot 发送了卡片
bot.reply_card.assert_called_once()

# 验证调用次数
assert bot.reply.call_count == 3

# 获取最后一次调用的参数
args, kwargs = bot.reply.call_args
chat_id, text = args

# 验证从未被调用
bot.reply.assert_not_called()

# 重置 mock 计数（在同一个测试中多次断言时）
bot.reply.reset_mock()
```
