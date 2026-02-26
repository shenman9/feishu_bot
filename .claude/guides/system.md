# 系统架构指南

> **Role & Context**: 你是 Claude，正在协助开发一个基于 Python 3.10+ 和飞书开放平台 (Lark Open Platform) 的插件化机器人系统。

## 项目概览

本项目是一个模块化的飞书机器人框架。核心架构模式为 **微内核 + 插件** 体系。

* **FeishuBot (Core)**: 处理 WebSocket 连接、鉴权、基础消息收发。
* **HubBot (Core)**: 继承自 FeishuBot，作为系统的**中央处理器**。负责加载插件、路由消息、处理异常。
* **Plugins**: 独立的功能模块。

## 核心架构原则 (关键)

1.  **严格的插件隔离**:
    * 插件 **严禁** `import` 其他插件的模块（例如 `from plugins.other import x` 是禁止的）。
    * 插件只能依赖 `core` 目录下的基类和接口。
    * 插件目录应被视为可以随时被移除或替换的独立单元。
2.  **通信机制**:
    * 插件与外界的交互必须通过 `HubBot` 提供的统一接口（Context/API）进行。
    * 如果插件 A 需要触发插件 B 的逻辑，必须向 Hub 发送指令，由 Hub 调度。
3.  **容错性**:
    * **主进程永不崩溃**。单个插件的未捕获异常必须被 HubBot 捕获，并记录错误日志，同时向用户回复友好的错误提示（如："该功能暂时遇到问题"）。

## 技术栈

* **语言**: Python 3.10+
* **并发模式**: 主线程处理飞书 WebSocket 事件（同步调用插件）；阻塞型 I/O（子进程调用、流式 HTTP 请求等）通过 `threading.Thread` 在后台线程中执行，避免阻塞主线程。
* **核心依赖**: `lark-oapi`（飞书 SDK）、`pyyaml`（配置解析）、`httpx`（HTTP 客户端）、`schedule`（定时任务）、`jinja2`（模板引擎）
* **依赖管理**: `pip`（运行依赖直接安装；测试依赖通过 `requirements-dev.txt`）。
* **测试框架**: `pytest`。

## 敏感信息与配置管理

* **config.yaml**: 你可以读取和修改此文件以进行本地调试。
* **Git 约束**:
    * 严禁将包含真实密钥（App ID, Secret）的 `config.yaml` 提交到 git 历史中。
    * 始终确保 `.gitignore` 包含 `config.yaml`。
    * 提交配置变更时，仅修改 `config.yaml.example`。

## 常用命令

* **安装运行依赖**: `pip install lark-oapi pyyaml httpx schedule jinja2 anthropic`
* **安装测试依赖**: `pip install -r requirements-dev.txt`
* **运行测试**: `pytest`

### 服务管理（必须通过 run.sh，禁止直接执行 python main.py）

| 命令 | 说明 |
|------|------|
| `./run.sh start` | 启动机器人（后台守护进程） |
| `./run.sh stop` | 停止机器人 |
| `./run.sh restart` | 重启机器人 |
| `./run.sh status` | 查看运行状态 |

**启动前预检**：`start` / `restart` 会自动执行环境预检，任一项失败则终止启动并给出提示：

* Python 解释器可用性
* `config.yaml` 文件是否存在
* `main.py` 文件是否存在
* 权限服务器端口是否已被占用（从 `config.yaml` 读取，缺省 9876），占用时显示占用进程 PID 与命令名

## CC 插件权限服务器机制

CC 插件（ClaudeCodePlugin）启动时会在独立端口启动一个 HTTP 权限确认服务器，并将端口号写入 `~/.claude/.feishu_perm_port`，供 `permission_hook.sh` 读取。

**降级机制**：若权限服务器启动失败（如端口被占用），插件会立即删除端口文件。hook 脚本检测不到端口文件时直接 `exit 0` 自动放行，避免因 curl 超时（默认 180s）卡住每次工具调用。

**并发安全**：`_ensure_permission_server()` 内部使用双重检查锁（`_perm_server_lock`），防止多线程同时尝试绑定端口产生 `Address already in use` 错误。
