# Claude Code 插件（CC）

## 简介

通过飞书远程调用本地 Claude Code CLI 的桥接插件。用户在飞书聊天中发送提示词，插件以子进程方式启动 `claude` 命令，将输出实时流式推送到飞书卡片。同时内置 HTTP 权限确认服务，对涉及文件修改、命令执行等敏感操作提供飞书卡片级别的交互确认。

## 触发方式

在飞书聊天中发送关键词 `CC` 即可激活插件。

## 功能特性

- **远程执行**：在飞书中直接向本地 Claude Code 发送任务，无需打开终端
- **会话持久化**：同一用户的多次请求共享同一 Claude Code 会话（UUID 标识），保留上下文
- **流式输出**：Claude Code 的输出实时推送到飞书卡片，节流更新（0.5 秒/50 字符）
- **权限确认**：危险操作（写文件、执行命令等）通过飞书卡片弹出确认，用户可逐一审批
- **三态权限模式**：灵活的权限控制，适应不同工作场景
- **工作目录管理**：支持切换工作目录，切换后自动重置会话

## 特殊指令

在激活插件后，以下指令可直接发送：

| 指令 | 说明 |
|------|------|
| `/new` | 重置当前会话（保留工作目录）|
| `/cancel` | 终止当前正在运行的任务 |
| `/status` | 查看当前会话状态（会话 ID、工作目录、权限模式）|
| `/permission` | 弹出权限模式选择卡片 |
| `/cd <路径>` | 切换工作目录并重置会话 |
| `/cd` | 重置工作目录为默认值并重置会话 |
| `/help` | 显示帮助信息 |

## 权限模式

通过 `/permission` 指令或点击确认卡片上的按钮可切换权限模式。

| 模式 | 标识 | 说明 |
|------|------|------|
| 交互确认 | `interactive` | **默认模式**。所有操作均需飞书卡片确认 |
| 自动放行编辑 | `accept_edits` | 工作目录内的文件写入/编辑自动放行，其余操作仍需确认 |
| 全部放行 | `bypass` | 所有操作自动放行，适合高度信任场景（谨慎使用）|

默认模式由配置项 `claude_code.default_perm_mode` 控制，新会话启动时生效。

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

只读工具（`Read`、`Glob`、`Grep` 等）在 Hook 脚本入口直接放行，不经过权限服务器。

## 文件结构

```
claude_code/
├── __init__.py              # 导出 ClaudeCodePlugin
├── claude_code_plugin.py    # 插件主体（约 1170 行，含会话管理、流式推送）
├── permission_server.py     # HTTP 权限确认服务（277 行）
├── permission_hook.sh       # Claude Code PreToolUse Hook 脚本
├── standalone.py            # 独立机器人模式（跳过 HubBot 直连飞书）
└── __main__.py              # python -m plugins.claude_code 入口
```

## 模块职责

| 模块 | 职责 |
|------|------|
| `claude_code_plugin.py` | 插件核心：指令解析、子进程管理、输出流式推送、会话状态 |
| `permission_server.py` | 内嵌 HTTP 服务器，接收 Hook 请求并阻塞等待用户飞书确认 |
| `permission_hook.sh` | 由 Claude Code 调用，将权限请求转发给 PermissionServer |
| `standalone.py` | 绕过 HubBot，以独立机器人身份运行 CC 插件 |
| `__main__.py` | 支持 `python -m plugins.claude_code` 直接启动 |

## 配置

在 `config.yaml` 的 `claude_code` 节点下配置（参考 `config.yaml.example`）：

```yaml
claude_code:
  claude_path: "/usr/bin/claude"      # claude CLI 可执行文件路径
  default_working_dir: ""             # 默认工作目录（空则使用当前目录）
  timeout: 600                        # 单次任务超时时间（秒）
  max_output_chars: 28000             # 飞书卡片最大字符数（超出则截断）
  default_perm_mode: "interactive"    # 新会话默认权限模式
                                      #   interactive / accept_edits / bypass
  max_turns: 50                       # Claude Code 最大对话轮数
  run_as_user: ""                     # 子进程切换到指定系统用户运行（解决 root 限制）
  permission_server_port: 9876        # 权限确认服务监听端口
  permission_timeout: 120             # 用户确认超时时间（秒），超时自动拒绝
```

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `claude_path` | `"claude"` | Claude CLI 路径，确保可执行 |
| `default_working_dir` | `""` | 默认工作目录，留空使用进程当前目录 |
| `timeout` | `600` | 任务执行超时（秒），超时后自动终止子进程 |
| `max_output_chars` | `28000` | 飞书卡片字符上限，超出则保留最新内容 |
| `default_perm_mode` | `"interactive"` | 新会话启动时的默认权限模式 |
| `max_turns` | `50` | Claude Code `--max-turns` 参数值 |
| `run_as_user` | `""` | 以指定系统用户运行子进程，适用于 Docker root 环境 |
| `permission_server_port` | `9876` | 权限服务 HTTP 监听端口，需确保未被占用 |
| `permission_timeout` | `120` | 等待用户确认的超时时间（秒）|

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

## 注意事项

- 本插件依赖本地安装的 `claude` CLI，需提前安装并完成认证
- 权限服务器默认绑定 `localhost:9876`，仅供本机 Hook 脚本访问，不对外暴露
- `run_as_user` 仅在 Linux 下生效，需要主进程有足够权限切换用户
- 长时间运行的任务可通过 `/cancel` 中止，避免资源占用
- `bypass` 模式将跳过所有权限确认，请在充分信任的场景下使用
