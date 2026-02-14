# Feishu Bot

基于飞书开放平台的插件化机器人框架。通过统一入口管理多个独立功能，用户发送关键词即可切换不同功能。

## 项目结构

```
feishu_bot/
├── main.py                  # 启动入口
├── config.py                # 配置加载
├── config.yaml              # 实际配置（不提交）
├── config.yaml.example      # 配置模板
├── core/                    # 核心框架
│   ├── feishu_bot.py        # 机器人基类（WebSocket 连接、消息收发）
│   ├── hub_bot.py           # 统一入口（插件注册、消息路由、功能菜单）
│   └── plugin.py            # 插件抽象基类
└── plugins/                 # 功能插件（每个插件一个独立目录）
    ├── README.md            # 插件开发指南
    └── rps_game/            # 石头剪刀布（示例插件）
        └── rps_plugin.py
```

## 快速开始

### 1. 安装依赖

```bash
pip install lark-oapi pyyaml
```

### 2. 配置

```bash
cp config.yaml.example config.yaml
```

编辑 `config.yaml`，填入飞书应用的凭证：

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

| 用户发送 | 机器人行为 |
|---------|-----------|
| `菜单` / `帮助` | 展示功能菜单卡片 |
| 插件关键词（如 `石头剪刀布`） | 激活对应插件 |
| `退出` / `返回` | 退出当前插件，回到主菜单 |

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
用户消息 → WebSocket → FeishuBot(基类) → HubBot(路由)
                                            ├─ 关键词匹配 → 激活插件
                                            ├─ 活跃插件   → 转发消息
                                            └─ 无上下文   → 展示菜单
```

- `FeishuBot`：封装飞书 WebSocket 连接和消息收发，子类实现 `on_message`
- `HubBot`：继承 FeishuBot，负责插件注册和消息路由
- `Plugin`：插件抽象基类，定义统一接口，插件之间代码完全独立
