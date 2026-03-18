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
- **工作区管理**（可选）：多用户隔离工作区，支持从配置仓库 clone/fork、绑定/解绑工作区、直接执行 bash 命令。通过继承扩展实现（`WorkspaceClaudeCodePlugin`），未配置 `workspace` 时自动降级为基础功能
- **用户问题转发**：Claude Code 调用 `AskUserQuestion` 时，问题会以飞书交互卡片形式呈现（逐题展示、预设选项按钮 + 自定义输入），同时发送应用内加急通知提醒用户及时作答，用户回答后实时传回 Claude；即使处于 bypass 模式，该工具仍需用户实际作答
- **计划审批**：Claude Code 进入 Plan Mode 后，`EnterPlanMode` 经权限服务器标记状态并自动放行，期间追踪实际写入的计划文件路径；调用 `ExitPlanMode` 时优先从追踪到的文件读取计划内容，以飞书卡片完整展示，同时发送应用内加急通知提醒用户及时审批，用户可点击「批准计划」「拒绝计划」或输入修改意见后「拒绝并反馈」；即使处于 bypass 模式，计划仍需用户审批
- **完成通知**：任务执行结束后自动发送飞书应用内加急通知，避免用户错过结果
- **消息队列**：任务执行期间用户发送的新指令自动入队（最多 10 条），上一轮完成后按序自动执行，无需手动重发；支持查看队列内容、移除指定条目、清空队列
- **引用回复**：执行卡片以引用回复方式关联用户的原始指令消息，便于在多人群聊或连续对话中快速追溯每张执行卡片对应的指令来源；队列中的排队指令同样保留消息关联，自动消费时也以引用回复方式发送
- **模型选择**：支持在会话内切换 Claude 模型（Sonnet / Opus / Haiku 等），通过 `/model` 指令弹出选择卡片；模型设置为会话级别，重置会话后恢复默认

## 特殊指令

在激活插件后，以下指令可直接发送：

| 指令 | 说明 |
|------|------|
| `/new` | 重置当前会话（保留工作目录）|
| `/session` | 列出历史会话（初始 5 条，可分页加载更多），点击按钮可恢复任意一条；支持收藏（置顶展示、永不过期）和重命名会话标题；恢复后展示该会话最近 3 轮对话预览 |
| `/star` | 收藏/取消收藏当前会话，可在会话开始前使用（收藏后置顶展示且不会被自动清理）|
| `/rename <标题>` | 重命名当前会话标题，可在会话开始前使用，方便后续查找 |
| `/cancel` | 终止当前正在运行的任务并清空消息队列 |
| `/queue` | 查看当前排队中的指令列表 |
| `/queue remove N` | 移除队列中第 N 条指令 |
| `/queue clear` | 清空队列（不终止当前任务）|
| `/status` | 查看当前会话状态（会话标题/ID、工作目录、队列、权限模式），已自定义标题或收藏的会话会显示对应信息 |
| `/permission` | 弹出权限模式选择卡片 |
| `/cd <路径>` | 切换工作目录并重置会话 |
| `/cd` | 重置工作目录为默认值并重置会话 |
| `/compact` | 压缩当前会话上下文（释放 token 空间），透传给 Claude Code 执行 |
| `/model` | 弹出模型选择卡片，切换当前会话使用的 Claude 模型 |
| `/help` | 显示帮助信息 |

### 工作区指令（需配置 `workspace` 段）

以下指令仅在 `claude_code.yaml` 中配置了 `workspace` 段后生效，未配置时不显示、不响应：

| 指令 | 说明 |
|------|------|
| `/profile [name] [email]` | 查看或设置 Git 身份（`/profile` 查看，`/profile 张三 email` 设置）|
| `/init <repo> [描述]` | 从配置仓库创建新工作区（clone + 配置 fork remote + 自动绑定）|
| `/bind <名称\|编号>` | 绑定指定工作区到当前会话（会重置会话）|
| `/unbind` | 解绑当前工作区，返回用户默认目录 |
| `/folders` | 列出所有工作区文件夹及状态（名称、大小、绑定状态）|
| `/rmfolder <名称\|编号>` | 删除指定工作区文件夹（若当前绑定则先解绑）|
| `/run <命令>` | 在当前工作目录下直接执行 bash 命令（不走 Claude Code，超时 30 秒）|

> 未被上述指令匹配的 `/` 开头文本（如 `/compact`）会直接作为 prompt 发送给 Claude Code。其中 `/compact` 对应 Claude Code 内置的上下文压缩命令，执行耗时较长（可能数分钟），完成后显示压缩前的 token 数。

## 权限模式

通过 `/permission` 指令或点击确认卡片上的按钮可切换权限模式。

| 模式 | 标识 | 说明 |
|------|------|------|
| 交互确认 | `interactive` | 所有操作均需飞书卡片确认 |
| 自动放行编辑 | `accept_edits` | 工作目录内的文件写入/编辑自动放行，其余操作仍需确认 |
| 全部放行 | `bypass` | 所有操作自动放行，适合高度信任场景（谨慎使用）。例外：`AskUserQuestion` 始终需要用户作答，`ExitPlanMode` 始终需要用户审批计划 |
| 手动选择 | `manual_select` | 新会话创建后弹出权限选择卡片，由用户手动选择模式 |

默认模式由配置项 `claude_code.default_perm_mode` 控制，新会话启动时生效。`manual_select` 仅作为配置项值使用，表示每次新建会话时自动弹出选择卡片；运行时实际使用的始终是 `interactive`、`accept_edits`、`bypass` 三者之一。

## 权限确认流程

```
Claude Code 触发危险操作
    ↓
permission_hook.sh（Claude PreToolUse Hook）
    ↓
POST /permission-request → PermissionServer（localhost:<动态端口>）
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

### ExitPlanMode 处理（计划审批）

当 Claude Code 在 Plan Mode 中调用 `ExitPlanMode` 时，走独立的处理路径：

```
Claude Code 调用 ExitPlanMode
    ↓
permission_hook.sh → PermissionServer → 插件识别为 ExitPlanMode
    ↓
读取计划内容（三级回退：追踪文件 → tool_input → mtime 搜索）
    ↓
构建计划审批卡片（橙色标题）
    ├─ 计划内容（markdown 格式，超 4000 字截断）
    ├─「批准计划」按钮
    ├─「拒绝计划」按钮
    └─ 修改意见输入框 +「拒绝并反馈」按钮
    ↓
用户在飞书中审批
    ├─ 批准 → 通过 Hook 传回"用户已批准"指令，Claude 开始执行
    ├─ 拒绝 → 传回"用户拒绝"，Claude 询问如何修改
    └─ 拒绝并反馈 → 传回拒绝理由，Claude 据此修改方案
    ↓
卡片变为灰色已处理状态（保留计划内容展示）
```

超时处理：计划审批超时 600 秒（10 分钟）。bypass 模式下 `ExitPlanMode` 仍需用户审批。`EnterPlanMode` 经权限服务器标记计划模式状态后自动放行，`TodoWrite` 在 Hook 脚本入口直接放行。

**超时处理**：用户在 `permission_timeout`（默认 120s）内未点击按钮，服务器自动按"拒绝"处理。Hook 脚本区分两类失败：权限确认超时（curl exit 28）→ 拒绝操作；连接失败（服务器未运行）→ 降级放行。

只读工具（`Read`、`Glob`、`Grep`）和非交互内部工具（`TodoWrite`）在 Hook 脚本入口直接放行，不经过权限服务器。`EnterPlanMode` 经权限服务器处理（标记计划模式状态、清除旧追踪路径）后自动放行。

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
├── workspace.py             # 工作区纯逻辑层（目录管理、Git clone/fork、profile 持久化）
├── workspace_plugin.py      # 工作区插件子类（继承 ClaudeCodePlugin，扩展工作区指令）
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
| `workspace.py` | 工作区纯逻辑层：目录创建/删除、Git clone/fork 配置、profile 持久化、目录大小计算 |
| `workspace_plugin.py` | 工作区插件子类：继承 `ClaudeCodePlugin`，扩展 `/profile`、`/init`、`/bind` 等工作区指令；未配置时完全降级 |
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
permission_server_port: 0           # 权限确认服务监听端口（0=OS 自动分配空闲端口）
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
| `permission_server_port` | `0` | 权限服务 HTTP 监听端口，`0` 表示由 OS 自动分配空闲端口 |
| `permission_timeout` | `120` | 等待用户确认的超时时间（秒）|
| `models` | 内置 Sonnet/Opus/Haiku | 可选模型列表，每项含 `alias`、`label`、`desc` |
| `default_model` | `""` | 新会话默认模型，留空使用 CLI 默认，填写应为 `models` 中的某个 `alias` |

### 工作区配置（可选）

在 `claude_code.yaml` 中添加 `workspace` 段即可启用工作区功能，不配置则工作区指令不生效、不显示：

```yaml
workspace:
  base_dir: "/data/workspaces"         # 工作区根目录（每个用户在此下自动创建独立子目录）
  repos:                                # 可用仓库列表
    simpler:                            # 仓库别名（用于 /init 命令）
      url: "git@github.com:Org/simpler.git"        # 原始仓库地址
      fork: "git@github.com:Bot/simpler.git"       # Bot fork 地址（可选）
    pypto:
      url: "git@github.com:Org/pypto.git"
      fork: "git@github.com:Bot/pypto.git"
```

| 配置项 | 说明 |
|--------|------|
| `workspace.base_dir` | 工作区根目录，用户首次 `/init` 时自动在此下创建 `<user_id>/` 子目录 |
| `workspace.repos` | 可用仓库字典，键为别名，`url` 为上游仓库，`fork` 为 Bot 的 fork 地址（`/init` 时自动配置为 push remote）|

## 会话状态

每个用户独立维护以下状态：

| 字段 | 说明 |
|------|------|
| `active` | 用户是否处于活跃会话 |
| `session_id` | Claude Code 会话 UUID（用于 `--resume`）|
| `session_started` | 会话是否已真正启动（首次调用 CLI 后置 True，包括被取消的情况）|
| `running` | 当前是否有任务正在运行 |
| `message_queue` | 排队中的用户指令（`collections.deque`，最多 10 条）|
| `working_dir` | 当前工作目录路径 |
| `last_chat_id` | 最近一次交互的飞书 chat_id（用于权限卡片推送）|
| `session_perm_mode` | 当前会话的权限模式 |
| `session_model` | 当前会话使用的模型别名，空字符串表示 CLI 默认 |
| `perm_timeout_count` | 当前任务中权限确认超时次数，任务结束时用于卡片警告提示 |

## 注意事项

- 本插件依赖本地安装的 `claude` CLI，需提前安装并完成认证
- 权限服务器绑定 `localhost` 动态端口，仅供本机 Hook 脚本访问，不对外暴露
- `run_as_user` 仅在 Linux 下生效，需要主进程有足够权限切换用户
- 长时间运行的任务可通过 `/cancel` 中止，避免资源占用
- `bypass` 模式将跳过所有权限确认，请在充分信任的场景下使用
- **卡片表格限制**：飞书卡片对单张卡片内的 markdown 表格数量有上限（约 5~10 个，取保守值 5）。当 Claude 回复中包含大量表格时，超出限制的表格自动转为代码块展示，保证卡片正常渲染。若卡片更新仍然失败，自动降级为纯文本发送，确保用户至少能收到回复内容
- **群聊唤醒模式**：群聊中默认需要 @机器人 才能触发响应。若需免 @使用，可在群内发送 `唤醒模式` 切换为「全部唤醒」，详见[项目 README](../../README.md#群聊唤醒模式)
