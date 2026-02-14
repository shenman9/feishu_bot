# CLAUDE.md

> 你是 Claude，正在协助开发一个基于 Python 3.10+ 和飞书开放平台的插件化机器人系统。

## 开发指南索引

本项目的详细开发指南按场景拆分存放在 `.claude/guides/` 目录下。请根据当前任务场景，阅读对应的指南文件：

| 场景 | 指南文件 | 说明 |
|------|---------|------|
| 系统架构 | `.claude/guides/system.md` | 项目概览、核心架构原则、技术栈、配置管理 |
| 插件开发 | `.claude/guides/plugin-dev.md` | 新增插件流程、隔离规则、错误处理模式 |
| 测试 | `.claude/guides/testing.md` | pytest 使用规范、测试命名、异步测试 |
| 代码风格 | `.claude/guides/code-style.md` | 类型提示、Docstring、命名规范、Commit 格式 |

## 通用规则（始终生效）

* **注释 & Commit Message 必须使用中文**。Commit 格式: `类型: 描述`。
* **config.yaml 严禁提交到 git**，配置变更只改 `config.yaml.example`。
* **主进程永不崩溃**，插件异常必须被 HubBot 捕获。
