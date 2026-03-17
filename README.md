# Hub Agent

基于飞书开放平台的插件化机器人框架。通过统一入口管理多个独立功能，用户发送关键词即可切换不同功能。

## 项目结构

```
hub_agent/
├── main.py                  # 启动入口
├── config.py                # 配置加载
├── run.sh                   # 服务管理脚本（启动/停止/重启）
├── config/                  # 配置文件目录
│   ├── system.yaml.example  # 系统配置模板（飞书凭证等）
│   ├── claude_code.yaml.example  # CC 插件配置模板
│   └── ...                  # 其他插件配置模板（*.yaml 不提交）
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
│       ├── workspace.py            # 工作区管理逻辑层
│       ├── workspace_plugin.py     # 工作区插件子类（继承扩展）
│       ├── permission_server.py    # 权限确认 HTTP 服务器（IPC 桥梁）
│       ├── permission_hook.sh      # PreToolUse Hook 脚本（Claude Code 调用）
│       ├── standalone.py           # 独立运行模式（跳过 HubBot，直连飞书）
│       └── __main__.py             # python -m plugins.claude_code 入口
└── tests/                   # 测试套件
    ├── README.md            # 测试开发指南
    ├── conftest.py          # 共享夹具（mock bot、StubPlugin 等）
    ├── core/                # 核心框架测试
    └── plugins/             # 插件测试
```

## 快速开始

### 1. 安装依赖

```bash
# 全量 Hub Agent
pip install -r requirements.txt

# 仅独立 CC Agent（最小依赖）
pip install lark-oapi pyyaml

pip install -r requirements-dev.txt  # 可选：测试依赖
```

### 2. 配置

> 应用需在[飞书开放平台](https://open.feishu.cn)创建，并开启「机器人」能力和 WebSocket 长连接模式。详见下方「飞书应用配置」章节。

#### 仅独立 CC Agent

```bash
cp config/cc/system.yaml.example config/cc/system.yaml
cp config/cc/claude_code.yaml.example config/cc/claude_code.yaml
```

编辑 `config/cc/system.yaml`，填入飞书应用的凭证：

```yaml
app_id: "your_app_id"
app_secret: "your_app_secret"
```

按需编辑 `config/cc/claude_code.yaml`，设置 `claude_path`、工作目录、权限模式等。

#### 全量 Hub Agent

```bash
cp config/system.yaml.example config/system.yaml
cp config/claude_code.yaml.example config/claude_code.yaml
# 按需复制其他插件配置
```

编辑 `config/system.yaml`，填入飞书应用的凭证：

```yaml
app_id: "your_app_id"
app_secret: "your_app_secret"
```

各插件配置项详见对应 `config/*.yaml.example` 中的注释。

> CC Agent 与 Hub Agent 使用独立的配置和数据目录（`config/cc/`、`data/cc_agent/`），可以同时运行互不干扰，但需要分别绑定不同的飞书应用。

### 3. 独立 CC Agent 启动

如果你**只需要 Claude Code 机器人**，无需启动完整的 Hub Agent，完成上述配置后直接启动：

**前置条件**：已安装 [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) 并完成认证（`claude --version` 可正常执行）

```bash
./run_cc.sh start
```

常用管理命令：

```bash
./run_cc.sh status    # 查看运行状态
./run_cc.sh log       # 查看最近日志
./run_cc.sh restart   # 重启服务
./run_cc.sh stop      # 停止服务
```

> **关于权限确认 Hook**：首次启动时，CC Agent 会自动将 `permission_hook.sh` 注册到 `~/.claude/settings.json` 的 `PreToolUse` hook 中，无需手动配置。如果你已有自定义 hook，不会被覆盖（仅追加）。该 Hook 使 Claude Code 在执行文件修改、命令执行等敏感操作前，通过飞书卡片向你请求确认。

### 4. 启动 Hub Agent

如果你需要完整的插件集成功能（多插件路由、菜单管理等），启动 Hub Agent：

```bash
./run.sh start
```

#### 飞书应用配置

1. 前往 [飞书开放平台](https://open.feishu.cn) 创建应用
2. 在「应用能力」中添加「机器人」
3. 在「权限管理」中开通以下权限：

   | 权限名称 | 权限标识 | 用途 |
   |---------|---------|------|
   | 获取用户基本信息 | `contact:user.base:readonly` | 获取用户身份信息 |
   | 获取用户 user ID | `contact:user.employee_id:readonly` | 获取用户 user ID |
   | 获取用户组信息 | `contact:group:readonly` | 获取用户组信息 |
   | 获取群组中所有消息 | `im:message.group_msg` | 获取群组中所有消息 |
   | 接收群聊中@机器人消息 | `im:message.group_at_msg:readonly` | 读取群聊中 @机器人的消息内容 |
   | 读取消息 | `im:message` | 获取与发送消息内容 |
   | 读取单聊消息 | `im:message.p2p_msg:readonly` | 读取私聊消息内容 |
   | 以应用身份发消息 | `im:message:send_as_bot` | 发送文本和卡片消息 |
   | 更新应用发送的消息 | `im:message:update` | 流式更新卡片内容 |
   | 获取与上传图片或文件资源 | `im:resource` | 下载用户上传的文件（文件阅读插件） |
   | 发送应用内加急 | `im:message.urgent` | 任务完成后发送加急通知提醒用户 |
   

   > - 如果仅使用独立 CC Agent 且不需要文件阅读功能，可不开通 `im:resource`

   <details>
   <summary>一键导入权限 JSON（点击展开）</summary>

   在飞书开放平台「权限管理」页面点击「批量开通」，粘贴以下 JSON 即可一键导入所有权限：

   ```json
   {
     "scopes": {
       "tenant": [
         "contact:user.base:readonly",
         "contact:user.employee_id:readonly",
         "contact:group:readonly",
         "im:message.group_msg",
         "im:message.group_at_msg:readonly",
         "im:message",
         "im:message.p2p_msg:readonly",
         "im:message:send_as_bot",
         "im:message:update",
         "im:message.urgent",
         "im:resource"
       ]
     }
   }
   ```

   </details>

4. 在「事件与回调」中启用 **WebSocket 长连接模式**（非 HTTP 回调），并订阅以下事件：
   - `im.message.receive_v1`（事件配置：接收消息）
   - `card.action.trigger`（回调配置：卡片交互回调，权限确认/会话列表等按钮点击依赖此事件）
5. 发布应用，将 `App ID` 和 `App Secret` 填入对应的 `system.yaml`

完成后，在飞书中搜索你的机器人名称，发送任意消息即可开始使用。功能详情参见 [Claude Code 插件文档](plugins/claude_code/README.md)。

## 使用方式

### 发送消息触发

| 用户发送 | 机器人行为 |
|---------|-----------|
| `菜单` / `帮助` | 展示功能菜单卡片 |
| 插件关键词 | 激活对应插件 |
| `退出` / `返回` | 退出当前插件，回到主菜单 |
| 合并转发消息 | 解析子消息内容（还原发送者、时间戳），作为文本传递给活跃插件 |

### 已有插件

| 关键词 | 插件 | 说明 |
|--------|------|------|
| `石头剪刀布` | RPSPlugin | 猜拳小游戏 |
| `文件阅读` | FileReaderPlugin | 上传 txt 文件读取内容 |
| `Claude` | ClaudeChatPlugin | 多轮智能对话，流式响应实时更新卡片 |
| `论文日报` | PaperDailyPlugin | ArXiv 论文 AI 筛选与中文摘要，支持订阅每日定时推送 |
| `CC` | ClaudeCodePlugin | 调用本地 Claude Code CLI，支持飞书交互式权限确认（允许/拒绝/bypass 危险操作），发送 `/help` 查看完整指令列表 |

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

- `FeishuBot`：封装飞书 WebSocket 连接和消息收发，支持合并转发消息解析（自动提取子消息文本、还原发送者与时间戳），子类实现 `on_message`
- `HubBot`：继承 FeishuBot，负责插件注册和消息路由
- `Plugin`：插件抽象基类，定义统一接口，插件之间代码完全独立

## 群聊唤醒模式

默认情况下，群聊中只有 @机器人 的消息才会被处理。通过唤醒模式，可以让群内所有消息直接触发机器人响应，无需每次 @。

**设置方法**：在群聊中发送文本 `唤醒模式`，机器人会弹出选择卡片：

| 模式 | 说明 |
|------|------|
| 全部唤醒 | 群内所有消息都直接发给机器人，无需 @ |
| 仅@唤醒 | 只有 @机器人 的消息才会触发响应（默认）|

> 注意：唤醒模式按群聊独立设置，群 A 开启不影响群 B。该设置仅保存在内存中，服务重启后需重新设置。

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
