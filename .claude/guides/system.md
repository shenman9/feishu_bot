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
* **并发模式**: `asyncio` (异步 I/O)。所有 I/O 操作（网络请求、数据库）必须是异步的 (`await`)。
* **依赖管理**: `pip` + `requirements.txt`。
* **测试框架**: `pytest`。

## 敏感信息与配置管理

* **config.yaml**: 你可以读取和修改此文件以进行本地调试。
* **Git 约束**:
    * 严禁将包含真实密钥（App ID, Secret）的 `config.yaml` 提交到 git 历史中。
    * 始终确保 `.gitignore` 包含 `config.yaml`。
    * 提交配置变更时，仅修改 `config.yaml.example`。

## 常用命令

* **安装依赖**: `pip install -r requirements.txt`
* **运行机器人**: `python main.py`
* **运行测试**: `pytest`
