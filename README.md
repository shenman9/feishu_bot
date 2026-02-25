# Feishu Bot

基于飞书开放平台的插件化机器人框架。通过统一入口管理多个独立功能，用户发送关键词即可切换不同功能。

## 项目结构

```
feishu_bot/
├── main.py                  # 启动入口
├── config.py                # 配置加载
├── config.yaml              # 实际配置（不提交）
├── config.yaml.example      # 配置模板
├── pyproject.toml           # pytest 配置
├── requirements-dev.txt     # 测试依赖
├── core/                    # 核心框架
│   ├── feishu_bot.py        # 机器人基类（WebSocket 连接、消息收发）
│   ├── hub_bot.py           # 统一入口（插件注册、消息路由、功能菜单）
│   └── plugin.py            # 插件抽象基类
├── plugins/                 # 功能插件（每个插件一个独立目录）
│   ├── README.md            # 插件开发指南
│   ├── rps_game/            # 石头剪刀布（示例插件）
│   ├── file_reader/         # 文件阅读（上传 txt 文件读取内容）
│   ├── claude_chat/         # Claude 对话（多轮智能对话，流式响应）
│   ├── paper_daily/         # 论文日报（ArXiv 论文 AI 筛选与每日推送）
│   └── claude_code/         # Claude Code 桥接（调用本地 CLI，支持飞书交互式权限确认）
│       ├── claude_code_plugin.py   # 插件主体
│       ├── permission_server.py    # 权限确认 HTTP 服务器（IPC 桥梁）
│       └── permission_hook.sh      # PreToolUse Hook 脚本（Claude Code 调用）
└── tests/                   # 测试套件
    ├── README.md            # 测试开发指南
    ├── conftest.py          # 共享夹具（mock bot、StubPlugin 等）
    ├── core/                # 核心框架测试
    └── plugins/             # 插件测试
```

## 快速开始

### 1. 安装依赖

```bash
pip install lark-oapi pyyaml httpx schedule jinja2
pip install -r requirements-dev.txt  # 测试依赖
```

### 2. 配置

```bash
cp config.yaml.example config.yaml
```

编辑 `config.yaml`，填入飞书应用的凭证及各插件配置（详见 `config.yaml.example` 中的注释）：

```yaml
app_id: "your_app_id"
app_secret: "your_app_secret"
```

> 应用需在[飞书开放平台](https://open.feishu.cn)创建，并开启「机器人」能力和 WebSocket 长连接模式。

### 3. 启动

```bash
python main.py
```

## 使用方式

### 发送消息触发

| 用户发送 | 机器人行为 |
|---------|-----------|
| `菜单` / `帮助` | 展示功能菜单卡片 |
| 插件关键词 | 激活对应插件 |
| `退出` / `返回` | 退出当前插件，回到主菜单 |

### 已有插件

| 关键词 | 插件 | 说明 |
|--------|------|------|
| `石头剪刀布` | RPSPlugin | 猜拳小游戏 |
| `文件阅读` | FileReaderPlugin | 上传 txt 文件读取内容 |
| `Claude` | ClaudeChatPlugin | 多轮智能对话，流式响应实时更新卡片 |
| `论文日报` | PaperDailyPlugin | ArXiv 论文 AI 筛选与中文摘要，支持订阅每日定时推送 |
| `CC` | ClaudeCodePlugin | 调用本地 Claude Code CLI，支持飞书交互式权限确认（允许/拒绝危险操作） |

### 底部菜单栏触发

机器人支持飞书底部自定义菜单栏，用户点击菜单项即可直接激活对应插件，无需手动输入关键词。菜单的 `event_key` 与插件注册关键词一致时自动匹配激活。

> 菜单栏需在[飞书开放平台](https://open.feishu.cn) → 应用后台 → 机器人 → 自定义菜单中配置，`event_key` 设置为对应插件的关键词。

## 添加新插件

1. 在 `plugins/` 下新建目录，如 `plugins/my_feature/`
2. 实现 `Plugin` 子类（接口详见 [plugins/README.md](plugins/README.md)）
3. 在 `main.py` 中注册：

```python
from plugins.my_feature import MyPlugin

bot.register_all([
    RPSPlugin(),
    MyPlugin(),   # 新增
])
```

## 架构说明

```
用户消息     → WebSocket → FeishuBot(基类) → HubBot(路由)
                                               ├─ 关键词匹配 → 激活插件
                                               ├─ 活跃插件   → 转发消息
                                               └─ 无上下文   → 展示菜单

菜单栏点击   → WebSocket → FeishuBot(基类) → HubBot(菜单路由)
                                               ├─ event_key 匹配插件 → 激活插件
                                               └─ 无匹配             → 展示菜单
```

- `FeishuBot`：封装飞书 WebSocket 连接和消息收发，子类实现 `on_message`
- `HubBot`：继承 FeishuBot，负责插件注册和消息路由
- `Plugin`：插件抽象基类，定义统一接口，插件之间代码完全独立

## 测试

测试框架基于 pytest，通过 mock 隔离 `lark_oapi` 依赖，无需飞书连接即可运行。

```bash
# 运行全部测试
pytest

# 运行指定模块
pytest tests/core/
pytest tests/plugins/test_rps_plugin.py

# 按名称匹配
pytest -k "menu"
```

`tests/conftest.py` 提供了三个共享 fixture：

| Fixture | 说明 |
|---------|------|
| `mock_bot` | `reply()` / `reply_card()` 被 mock 的 HubBot 实例 |
| `stub_plugin` | 最小化插件实现，记录收到的消息到 `received_messages` |
| `bot_with_plugin` | `(mock_bot, stub_plugin)` 元组，插件已注册 |

新增插件时，在 `tests/plugins/` 下创建对应的 `test_<plugin_name>.py` 即可。详细指南见 [tests/README.md](tests/README.md)。
