# Claude Code 插件（CC）

## 简介

通过飞书远程调用本地 Claude Code CLI 的桥接插件。用户在飞书聊天中发送提示词，插件以子进程方式启动 `claude` 命令，将输出实时流式推送到飞书卡片。同时内置 HTTP 权限确认服务，对涉及文件修改、命令执行等敏感操作提供飞书卡片级别的交互确认。

## 触发方式

在飞书聊天中发送关键词 `CC` 即可激活插件。

## 功能特性

- **远程执行**：在飞书中直接向本地 Claude Code 发送任务，无需打开终端
- **会话持久化**：同一用户的多次请求共享同一 Claude Code 会话（UUID 标识），保留上下文
- **历史会话恢复**：历史会话自动记录到磁盘（Hub 模式：`data/claude_code/feishu_sessions.json`；CC 专属机器人：`data/cc_agent/feishu_sessions.json`），发送 `/session` 可查看并一键恢复任意历史会话，重启服务不丢失；恢复后自动展示该会话最近 3 轮对话预览（绿色卡片），帮助用户快速回忆上下文；支持**收藏**重要会话（置顶展示、永不过期）和**重命名**会话标题；未收藏的会话超过 7 天未活跃将自动清理
- **流式输出**：Claude Code 的输出实时推送到飞书卡片，节流更新（0.5 秒/50 字符）
- **过程可视化**：工具调用日志（含调用结果状态）与 Claude 文字回复按实际执行顺序交错展示，与原生 Claude Code 终端输出顺序一致；连续大量工具调用时自动折叠（保留最近 15 条）；卡片标题显示任务总用时（`执行中...已用时 Xs`），日志区域显示当前阶段用时（`💭 思考中...` 表示等待模型推理，`⏳ 正在处理...` 表示工具执行中），思考完成后标注思考耗时（`💭 思考完成 (用时 Xs)`）
- **权限确认**：危险操作（写文件、执行命令等）通过飞书卡片弹出确认，用户可逐一审批；若确认超时，任务完成卡片会追加警告提示（如 `⚠️ 本次任务中有 N 次权限确认超时（120s），操作已自动拒绝`），帮助用户定位失败原因
- **三态权限模式**：灵活的权限控制，适应不同工作场景
- **工作目录管理**：支持切换工作目录，切换后自动重置会话
- **用户问题转发**：Claude Code 调用 `AskUserQuestion` 时，问题会以飞书交互卡片形式呈现（逐题展示、预设选项按钮 + 自定义输入），用户回答后实时传回 Claude；即使处于 bypass 模式，该工具仍需用户实际作答
- **完成通知**：任务执行结束后自动发送飞书应用内加急通知，避免用户错过结果
- **模型选择**：支持在会话内切换 Claude 模型（Sonnet / Opus / Haiku 等），通过 `/model` 指令弹出选择卡片；模型设置为会话级别，重置会话后恢复默认

## 特殊指令

在激活插件后，以下指令可直接发送：

| 指令 | 说明 |
|------|------|
| `/new` | 重置当前会话（保留工作目录）|
| `/session` | 列出历史会话（初始 5 条，可分页加载更多），点击按钮可恢复任意一条；支持收藏（置顶展示、永不过期）和重命名会话标题；恢复后展示该会话最近 3 轮对话预览 |
| `/star` | 收藏/取消收藏当前会话（收藏后置顶展示且不会被自动清理）|
| `/rename <标题>` | 重命名当前会话标题，方便后续查找 |
| `/cancel` | 终止当前正在运行的任务 |
| `/status` | 查看当前会话状态（会话 ID、工作目录、权限模式）|
| `/permission` | 弹出权限模式选择卡片 |
| `/cd <路径>` | 切换工作目录并重置会话 |
| `/cd` | 重置工作目录为默认值并重置会话 |
| `/compact` | 压缩当前会话上下文（释放 token 空间），透传给 Claude Code 执行 |
| `/model` | 弹出模型选择卡片，切换当前会话使用的 Claude 模型 |
| `/help` | 显示帮助信息 |

> 未被上述指令匹配的 `/` 开头文本（如 `/compact`）会直接作为 prompt 发送给 Claude Code。其中 `/compact` 对应 Claude Code 内置的上下文压缩命令，执行耗时较长（可能数分钟），完成后显示压缩前的 token 数。

## 权限模式

通过 `/permission` 指令或点击确认卡片上的按钮可切换权限模式。

| 模式 | 标识 | 说明 |
|------|------|------|
| 交互确认 | `interactive` | 所有操作均需飞书卡片确认 |
| 自动放行编辑 | `accept_edits` | 工作目录内的文件写入/编辑自动放行，其余操作仍需确认 |
| 全部放行 | `bypass` | 所有操作自动放行，适合高度信任场景（谨慎使用）。例外：`AskUserQuestion` 始终需要用户作答 |
| 手动选择 | `manual_select` | 新会话创建后弹出权限选择卡片，由用户手动选择模式 |

默认模式由配置项 `claude_code.default_perm_mode` 控制，新会话启动时生效。`manual_select` 仅作为配置项值使用，表示每次新建会话时自动弹出选择卡片；运行时实际使用的始终是 `interactive`、`accept_edits`、`bypass` 三者之一。

## 权限确认流程

```
Claude Code 触发危险操作
    ↓
permission_hook.sh（Claude PreToolUse Hook）
    ↓
POST /permission-request → PermissionServer（localhost:9876）
    ↓
服务器向飞书用户发送确认卡片，阻塞等待
    ↓
用户点击卡片按钮
    ├─ [允许]                    → 放行本次请求
    ├─ [拒绝]                    → 拒绝本次请求
    ├─ [允许本次会话所有修改]    → 切换为 accept_edits 并放行（仅对工作目录内文件编辑显示）
    └─ [允许本次会话所有请求]    → 切换为 bypass 并放行
    ↓
HTTP 响应返回给 Hook 脚本 → Claude Code 继续/中止操作
```

### AskUserQuestion 处理

当 Claude Code 调用 `AskUserQuestion` 工具时，走独立的处理路径（不走权限确认卡片）：

```
Claude Code 调用 AskUserQuestion
    ↓
permission_hook.sh → PermissionServer → 插件识别为 AskUserQuestion
    ↓
构建交互式问题卡片（逐题展示模式）
    ├─ 已回答题目：灰色展示
    ├─ 当前题目：预设选项按钮 + 自定义输入框
    └─ 后续题目：仅标题预览
    ↓
用户在飞书中作答
    ├─ 点击预设选项按钮 → 记录答案，刷新卡片展示下一题
    └─ 输入框填写 + 点击"其他" → 同上
    ↓
所有问题回答完毕 → 通过 Hook "deny" + reason 将答案传回 Claude Code
    ↓
卡片变为灰色已回答状态
```

超时处理：每道题超时 300 秒（5 分钟），多道题按数量累加。bypass 模式下 `AskUserQuestion` 仍需用户实际作答。

**超时处理**：用户在 `permission_timeout`（默认 120s）内未点击按钮，服务器自动按"拒绝"处理。Hook 脚本区分两类失败：权限确认超时（curl exit 28）→ 拒绝操作；连接失败（服务器未运行）→ 降级放行。

只读工具（`Read`、`Glob`、`Grep` 等）在 Hook 脚本入口直接放行，不经过权限服务器。

## 文件结构

```
claude_code/
├── __init__.py              # 导出 ClaudeCodePlugin
├── claude_code_plugin.py    # 插件核心：指令路由、子进程管理、流式推送、卡片回调
├── constants.py             # 共享常量和纯工具函数（路径处理、模型列表等）
├── cards.py                 # 飞书卡片 JSON 构建（执行卡片、权限卡片、会话列表等）
├── stream_parser.py         # CLI 流式输出解析与工具调用日志渲染
├── session_store.py         # 会话持久化（JSON 文件读写、过期清理、JSONL 历史解析）
├── permission_manager.py    # 权限服务器生命周期管理（Hook 注册、请求回调分发）
├── permission_server.py     # HTTP 权限确认服务（阻塞等待用户飞书确认）
├── permission_hook.sh       # Claude Code PreToolUse Hook 脚本
├── standalone.py            # 独立机器人模式（跳过 HubBot 直连飞书）
└── __main__.py              # python -m plugins.claude_code 入口
```

## 模块职责

| 模块 | 职责 |
|------|------|
| `claude_code_plugin.py` | 插件核心：指令路由、子进程管理、输出流式推送、卡片回调处理 |
| `constants.py` | 共享常量（关键词、数据目录、默认模型列表）和纯工具函数（路径展示、工作目录解析）|
| `cards.py` | 所有飞书卡片 JSON 构建逻辑，纯函数无状态 |
| `stream_parser.py` | CLI stdout 流式解析（JSON 行 → 文本/工具日志段）和日志渲染 |
| `session_store.py` | 历史会话持久化存储、过期清理，以及 Claude Code JSONL 会话文件读取 |
| `permission_manager.py` | 权限确认服务器的启动、Hook 注册、权限请求回调分发（通过回调与主插件解耦）|
| `permission_server.py` | 内嵌 HTTP 服务器，接收 Hook 请求并阻塞等待用户飞书确认 |
| `permission_hook.sh` | 由 Claude Code 调用，将权限请求转发给 PermissionServer |
| `standalone.py` | 绕过 HubBot，以独立机器人身份运行 CC 插件 |
| `__main__.py` | 支持 `python -m plugins.claude_code` 直接启动 |

## 配置

插件支持两种运行模式，配置路径不同：

| 模式 | 配置文件 | 飞书凭证 | 数据目录 |
|------|---------|---------|---------|
| Hub 模式（默认）| `config/claude_code.yaml` | `config/system.yaml` | `data/claude_code/` |
| CC 专属机器人 | `config/cc/claude_code.yaml` | `config/cc/system.yaml` | `data/cc_agent/` |

CC 专属机器人通过 `run_cc.sh` 启动，脚本自动设置 `CC_CONFIG_DIR`（`config/cc/`）和 `CC_DATA_DIR`（`data/cc_agent/`）环境变量，插件据此加载独立的配置和数据目录，与 Hub 模式完全隔离。

在对应的 `claude_code.yaml` 中配置（参考 `config/claude_code.yaml.example` 或 `config/cc/claude_code.yaml.example`）：

```yaml
claude_path: "/usr/bin/claude"      # claude CLI 可执行文件路径
default_working_dir: ""             # 默认工作目录（空则使用当前目录）
timeout: 600                        # 单次任务超时时间（秒）
max_output_chars: 28000             # 飞书卡片最大字符数（超出则截断）
default_perm_mode: "interactive"    # 新会话默认权限模式
                                    #   interactive   / accept_edits / bypass / manual_select
max_turns: 50                       # Claude Code 最大对话轮数
run_as_user: ""                     # 子进程切换到指定系统用户运行（解决 root 限制）
permission_server_port: 9876        # 权限确认服务监听端口
permission_timeout: 120             # 用户确认超时时间（秒），超时自动拒绝
models:                             # 可选模型列表，通过 /model 指令选择（可选，有内置默认）
  - alias: "sonnet"                 #   alias: 传给 claude --model 的值
    label: "Sonnet"                 #   label: 飞书卡片显示名称
    desc: "速度与质量均衡"            #   desc: 卡片上的描述
default_model: ""                   # 新会话默认模型，留空使用 CLI 默认
```

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `claude_path` | `"claude"` | Claude CLI 路径，确保可执行 |
| `default_working_dir` | `""` | 默认工作目录，留空使用进程当前目录 |
| `timeout` | `600` | 任务执行超时（秒），超时后自动终止子进程 |
| `max_output_chars` | `28000` | 飞书卡片字符上限，超出则保留最新内容 |
| `default_perm_mode` | `"interactive"` | 新会话启动时的默认权限模式，可选值：`interactive` / `accept_edits` / `bypass` / `manual_select` |
| `max_turns` | `50` | Claude Code `--max-turns` 参数值 |
| `run_as_user` | `""` | 以指定系统用户运行子进程，适用于 Docker root 环境 |
| `permission_server_port` | `9876` | 权限服务 HTTP 监听端口，需确保未被占用 |
| `permission_timeout` | `120` | 等待用户确认的超时时间（秒）|
| `models` | 内置 Sonnet/Opus/Haiku | 可选模型列表，每项含 `alias`、`label`、`desc` |
| `default_model` | `""` | 新会话默认模型，留空使用 CLI 默认，填写应为 `models` 中的某个 `alias` |

## 会话状态

每个用户独立维护以下状态：

| 字段 | 说明 |
|------|------|
| `active` | 用户是否处于活跃会话 |
| `session_id` | Claude Code 会话 UUID（用于 `--resume`）|
| `session_started` | 会话是否已真正启动（首次提交后置 True）|
| `running` | 当前是否有任务正在运行 |
| `working_dir` | 当前工作目录路径 |
| `last_chat_id` | 最近一次交互的飞书 chat_id（用于权限卡片推送）|
| `session_perm_mode` | 当前会话的权限模式 |
| `session_model` | 当前会话使用的模型别名，空字符串表示 CLI 默认 |
| `perm_timeout_count` | 当前任务中权限确认超时次数，任务结束时用于卡片警告提示 |

## 注意事项

- 本插件依赖本地安装的 `claude` CLI，需提前安装并完成认证
- 权限服务器默认绑定 `localhost:9876`，仅供本机 Hook 脚本访问，不对外暴露
- `run_as_user` 仅在 Linux 下生效，需要主进程有足够权限切换用户
- 长时间运行的任务可通过 `/cancel` 中止，避免资源占用
- `bypass` 模式将跳过所有权限确认，请在充分信任的场景下使用
- **群聊唤醒模式**：群聊中默认需要 @机器人 才能触发响应。若需免 @使用，可在群内发送 `唤醒模式` 切换为「全部唤醒」，详见[项目 README](../../README.md#群聊唤醒模式)
