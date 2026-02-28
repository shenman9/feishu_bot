# 论文日报插件

## 简介

自动抓取 ArXiv 最新论文，使用大语言模型（LLM）按用户配置的研究方向进行筛选，并生成中文摘要，定时推送到飞书。用户也可手动触发即时获取。

## 触发方式

在飞书聊天中发送关键词 `论文日报` 即可手动触发当日论文推送。

## 功能特性

- **定时推送**：在配置的北京时间每天自动向已订阅用户推送当日论文
- **智能筛选**：使用 LLM 根据配置的研究方向过滤无关论文，精准匹配关注领域
- **中文摘要**：LLM 对筛选后的论文生成简明中文摘要，降低阅读门槛
- **即时查询**：发送关键词可立即触发一次论文抓取与推送
- **订阅管理**：每位用户独立订阅，插件激活即视为订阅，退出即取消
- **进度反馈**：抓取和处理过程中实时更新卡片状态，避免长时间无响应

## 使用流程

```
用户: 论文日报
机器人: [开始抓取，显示进度卡片...]
机器人: [推送今日论文列表，含标题、作者、中文摘要及 ArXiv 链接]

（每日定时自动推送给已订阅用户）

用户: 退出
机器人: 已退出论文日报，取消定时推送订阅。
```

## 文件结构

```
paper_daily/
├── __init__.py              # 导出 PaperDailyPlugin
├── paper_daily_plugin.py    # 插件主体，负责调度与消息路由（485 行）
├── config.py                # AppConfig 数据类，加载插件配置
├── fetcher.py               # 从 ArXiv API 抓取论文
├── llm_client.py            # 调用 LLM 进行筛选与摘要生成
├── processor.py             # 论文过滤与处理逻辑
├── notifier.py              # 构建飞书卡片格式
├── reporter.py              # 生成摘要报告
├── models.py                # 数据模型（Paper 等）
└── templates/
    └── report.html.j2       # HTML 报告 Jinja2 模板
```

## 模块职责

| 模块 | 职责 |
|------|------|
| `paper_daily_plugin.py` | 插件入口，管理定时任务与用户订阅 |
| `fetcher.py` | 调用 ArXiv API，获取最新论文列表 |
| `processor.py` | 调用 LLM 筛选与摘要，协调处理流程 |
| `llm_client.py` | 封装 LLM API 调用（筛选 + 摘要两阶段）|
| `notifier.py` | 将论文数据转换为飞书消息卡片 |
| `reporter.py` | 生成 HTML 或文本格式的报告内容 |
| `models.py` | `Paper` 等数据结构定义 |
| `config.py` | `AppConfig` 数据类定义 |

## 配置

在 `config/paper_daily.yaml` 中配置（参考 `config/paper_daily.yaml.example`）：

```yaml
topics:                          # 关注的研究方向（LLM 筛选依据）
  - "Attention机制"
  - "Memory机制"
  - "KV cache压缩"
  - "Retrieval(检索)"
  - "长序列"
categories:                      # ArXiv 分类（cs.CL / cs.AI / cs.LG 等）
  - "cs.CL"
  - "cs.AI"
  - "cs.LG"
max_papers: 50                   # 每次最多处理的论文数量
llm_base_url: "https://api.anthropic.com"  # LLM API 地址
llm_api_key: "your_llm_api_key_here"       # LLM API Key
llm_model: "claude-opus-4-6"               # 用于筛选和摘要的模型
schedule_time: "10:00"           # 每日定时推送时间（北京时间 HH:MM）
```

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `topics` | 必填 | 感兴趣的研究方向列表，LLM 据此筛选论文 |
| `categories` | 必填 | ArXiv 分类代码列表 |
| `max_papers` | `50` | 单次最多处理的论文数，过多会增加 LLM 费用 |
| `llm_base_url` | 必填 | LLM API 服务地址 |
| `llm_api_key` | 必填 | LLM API 密钥 |
| `llm_model` | 必填 | 执行筛选和摘要的模型名称 |
| `schedule_time` | `"10:00"` | 每日自动推送时间（北京时间）|

## 注意事项

- 每次运行都会调用 LLM API，处理大量论文时会产生一定费用，建议合理设置 `max_papers`
- ArXiv 数据源为 UTC 时区，插件内部已做时区换算
- 定时任务在 `on_register` 生命周期中启动，以后台线程运行
