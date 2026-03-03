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

配置按职责拆分在 `config/` 目录下：

| 文件 | 说明 |
|------|------|
| `config/system.yaml` | 全局配置（app_id, app_secret） |
| `config/claude_chat.yaml` | Claude 对话插件配置 |
| `config/claude_code.yaml` | Claude Code 桥接插件配置 |
| `config/paper_daily.yaml` | 论文日报插件配置 |

* **config/\*.yaml**: 你可以读取和修改这些文件以进行本地调试。
* **Git 约束**:
    * 严禁将包含真实密钥的 `config/*.yaml` 提交到 git 历史中。
    * 始终确保 `.gitignore` 包含 `config/*.yaml`（已排除 `.example` 文件）。
    * 提交配置变更时，仅修改 `config/*.yaml.example`。

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
* `config/system.yaml` 文件是否存在
* `main.py` 文件是否存在
* 权限服务器端口是否已被占用（从 `config/claude_code.yaml` 读取，缺省 9876），占用时显示占用进程 PID 与命令名

## CC 插件权限服务器机制

CC 插件（ClaudeCodePlugin）启动时会在独立端口启动一个 HTTP 权限确认服务器，并将端口号写入 `data/claude_code/.feishu_perm_port`（即 `self._data_dir`，可在构造时注入自定义路径以支持多实例隔离）。同时通过环境变量 `FEISHU_CC_DATA_DIR` 将数据目录传递给子进程，`permission_hook.sh` 优先读取该变量，回落到默认路径 `<项目根>/data/claude_code`。

**降级机制**：若权限服务器启动失败（如端口被占用），插件会立即删除端口文件。hook 脚本检测不到端口文件时直接 `exit 0` 自动放行，避免因 curl 超时（默认 180s）卡住每次工具调用。

**并发安全**：`_ensure_permission_server()` 内部使用双重检查锁（`_perm_server_lock`），防止多线程同时尝试绑定端口产生 `Address already in use` 错误。

### 权限模式（session_perm_mode）

每个用户会话维护独立的权限模式，共三种，通过 `/permission` 指令弹出卡片切换：

| 模式 | 说明 |
|------|------|
| `interactive` | 所有操作均通过飞书卡片确认（默认） |
| `accept_edits` | 工作目录内的 Write/Edit/NotebookEdit 自动放行，其余仍需确认 |
| `bypass` | 所有操作自动放行，无需任何确认 |

新会话的默认模式由 `config/claude_code.yaml` 中的 `default_perm_mode` 决定，缺省为 `interactive`。

只读工具（`Read`、`Glob`、`Grep`）由 `permission_hook.sh` 在 hook 入口直接放行，不进入权限服务器流程，与原始 Claude Code 默认行为一致。

### 权限确认卡片按钮

需要用户确认时，飞书卡片上的按钮组合取决于当前请求的类型：

| 按钮 | action | 显示条件 |
|------|--------|---------|
| 「允许」 | `perm_allow` | 始终显示 |
| 「拒绝」 | `perm_deny` | 始终显示 |
| 「允许本次会话所有修改」 | `perm_accept_edits` | 仅当请求属于 accept_edits 范围：工具为 Write/Edit/NotebookEdit **且**目标文件在工作目录内 |
| 「允许本次会话所有请求」 | `perm_bypass` | 始终显示（bypass 模式下不会出现卡片，此处始终可见） |

「允许本次会话所有修改」不在 accept_edits 范围内的请求（如 `bash git status`，或工作目录外的文件写入）上隐藏，是因为切换到 accept_edits 模式对这类请求无效，显示该按钮会产生语义误导。
