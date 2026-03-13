# Claude 对话插件

## 简介

基于 Anthropic Claude API 的多轮智能对话插件。用户发送消息后，机器人以流式方式实时输出回复，并自动维护多轮对话历史，支持连贯的上下文交流。

## 触发方式

在飞书聊天中发送关键词 `Claude` 即可进入对话模式。

## 功能特性

- **多轮对话**：自动维护每个用户的独立对话历史，支持上下文连续交流
- **流式输出**：API 响应边生成边推送到飞书卡片，延迟感更低
- **更新节流**：最小间隔 0.5 秒、最小变化 50 字符，避免消息刷屏
- **历史管理**：历史超过上限时自动裁剪旧消息，保持对话流畅
- **清空对话**：发送 `清空对话` 可重置当前对话历史
- **系统提示词**：可通过配置注入自定义 System Prompt

## 使用流程

```
用户: Claude
机器人: 已进入 Claude 对话模式，请输入您的问题。

用户: 解释一下什么是快速排序？
机器人: [实时流式输出回复...]

用户: 能给我写一段 Python 示例吗？
机器人: [基于上下文继续回复...]

用户: 清空对话
机器人: 对话历史已清空。

用户: 退出
机器人: 已退出 Claude 对话模式。
```

## 文件结构

```
claude_chat/
├── __init__.py            # 导出 ClaudeChatPlugin
└── claude_chat_plugin.py  # 插件主体实现（299 行）
```

## 主要实现

| 类/方法 | 说明 |
|--------|------|
| `ClaudeChatPlugin` | 插件主类 |
| `handle_message()` | 处理用户消息，调用 API 并流式推送回复 |
| `_stream_response()` | 流式请求 Claude API，节流更新飞书卡片 |
| `_trim_history()` | 裁剪超长对话历史 |
| `is_user_active(user_id, chat_id)` | 返回用户是否处于活跃会话中 |

## 配置

在 `config/claude_chat.yaml` 中配置（参考 `config/claude_chat.yaml.example`）：

```yaml
api_url: "https://api.anthropic.com/v1/messages"  # API 地址（可替换为代理）
api_key: "your_claude_api_key_here"               # Anthropic API Key
model: "claude-opus-4-6"                          # 使用的模型
max_history: 20                                   # 保留的最大历史消息条数
max_tokens: 4096                                  # 单次回复最大 Token 数
system_prompt: ""                                 # 可选的系统提示词
```

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `api_url` | Anthropic 官方地址 | 支持替换为第三方代理 |
| `api_key` | 必填 | Anthropic API 密钥 |
| `model` | `claude-opus-4-6` | 对话使用的 Claude 模型 |
| `max_history` | `20` | 最多保留的历史消息条数（超出则裁剪）|
| `max_tokens` | `4096` | 单次回复的最大 Token 限制 |
| `system_prompt` | `""` | 为模型注入固定系统角色/指令 |

## 注意事项

- **卡片表格限制**：飞书卡片对单张卡片内的 markdown 表格数量有上限（约 5~10 个，取保守值 5）。当 Claude 回复中包含大量表格时，超出限制的表格自动转为代码块展示；若卡片更新仍然失败，自动降级为纯文本发送，确保用户至少能收到回复内容
