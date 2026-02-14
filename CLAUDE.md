# CLAUDE.md

> **Role & Context**: 你是 Claude，正在协助开发一个基于 Python 3.10+ 和飞书开放平台 (Lark Open Platform) 的插件化机器人系统。

## 1. 项目概览与架构

本项目是一个模块化的飞书机器人框架。核心架构模式为 **微内核 + 插件** 体系。

* **FeishuBot (Core)**: 处理 WebSocket 连接、鉴权、基础消息收发。
* **HubBot (Core)**: 继承自 FeishuBot，作为系统的**中央处理器**。负责加载插件、路由消息、处理异常。
* **Plugins**: 独立的功能模块。

### 核心架构原则 (关键)

1.  **严格的插件隔离**:
    * 插件 **严禁** `import` 其他插件的模块（例如 `from plugins.other import x` 是禁止的）。
    * 插件只能依赖 `core` 目录下的基类和接口。
    * 插件目录应被视为可以随时被移除或替换的独立单元。
2.  **通信机制**:
    * 插件与外界的交互必须通过 `HubBot` 提供的统一接口（Context/API）进行。
    * 如果插件 A 需要触发插件 B 的逻辑，必须向 Hub 发送指令，由 Hub 调度。
3.  **容错性**:
    * **主进程永不崩溃**。单个插件的未捕获异常必须被 HubBot 捕获，并记录错误日志，同时向用户回复友好的错误提示（如：“该功能暂时遇到问题”）。

## 2. 技术栈与规范

* **语言**: Python 3.10+
* **并发模式**: `asyncio` (异步 I/O)。所有 I/O 操作（网络请求、数据库）必须是异步的 (`await`)。
* **依赖管理**: `pip` + `requirements.txt`。
* **测试框架**: `pytest`。

### 代码风格

* **类型提示 (Type Hints)**: 核心框架代码推荐添加，插件代码可选。
* **文档字符串 (Docstrings)**:
    * **强制**: 所有 `class`, `public method`, `interface` 必须包含详细的文档字符串。
    * **风格**: Google Style (包含 `Args:`, `Returns:`, `Raises:` 等部分)。
* **语言习惯**:
    * 变量/函数名: 英文 (描述性命名)。
    * **注释 & Commit Message**: **必须使用中文**。
    * Commit Message 格式: `类型: 描述` (例如: `功能: 新增天气查询插件`, `修复: 解决WebSocket重连失败问题`)。

## 3. 开发工作流与指令

### 常用命令

* **安装依赖**: `pip install -r requirements.txt`
* **运行机器人**: `python main.py`
* **运行测试**: `pytest` (或 `pytest plugins/some_plugin/`)

### 新增插件流程

当被要求"创建一个新插件"时，请遵循以下步骤：

1.  在 `plugins/<plugin_name>/` 创建目录。
2.  创建 `__init__.py` 和 `<plugin_name>.py`。
3.  继承 `Plugin` 基类，实现 `handle_message` 等标准接口。
4.  **注册**: 修改 `main.py`，将新插件实例添加到 `bot.register_all([...])` 列表中。
5.  **配置**: 如果插件需要配置，检查 `config.yaml` 是否存在对应字段；如果不存在，更新 `config.yaml.example` 并在 `config.yaml` 中添加（如果需要）。

## 4. 敏感信息与配置管理

* **config.yaml**: 你可以读取和修改此文件以进行本地调试。
* **Git 约束**:
    * 严禁将包含真实密钥（App ID, Secret）的 `config.yaml` 提交到 git 历史中。
    * 始终确保 `.gitignore` 包含 `config.yaml`。
    * 提交配置变更时，仅修改 `config.yaml.example`。

## 5. 错误处理示例

在 `HubBot` 中调用插件时，必须使用类似以下的防御性编程：

```python
try:
    await plugin.handle_message(message)
except Exception as e:
    logger.error(f"插件 {plugin.name} 运行错误: {e}")
    await self.reply_text(message.chat_id, "⚠️ 抱歉，该功能运行出错，请联系管理员。")
    # 绝对不能让这个异常导致 main loop 退出
```