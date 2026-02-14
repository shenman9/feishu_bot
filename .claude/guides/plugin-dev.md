# 插件开发指南

## 新增插件流程

当被要求"创建一个新插件"时，请遵循以下步骤：

1.  在 `plugins/<plugin_name>/` 创建目录。
2.  创建 `__init__.py` 和 `<plugin_name>.py`。
3.  继承 `Plugin` 基类，实现 `handle_message` 等标准接口。
4.  **注册**: 修改 `main.py`，将新插件实例添加到 `bot.register_all([...])` 列表中。
5.  **配置**: 如果插件需要配置，检查 `config.yaml` 是否存在对应字段；如果不存在，更新 `config.yaml.example` 并在 `config.yaml` 中添加（如果需要）。

## 插件隔离规则

* 插件 **严禁** `import` 其他插件的模块（例如 `from plugins.other import x` 是禁止的）。
* 插件只能依赖 `core` 目录下的基类和接口。
* 插件目录应被视为可以随时被移除或替换的独立单元。
* 插件与外界的交互必须通过 `HubBot` 提供的统一接口（Context/API）进行。
* 如果插件 A 需要触发插件 B 的逻辑，必须向 Hub 发送指令，由 Hub 调度。

## 错误处理

在 `HubBot` 中调用插件时，必须使用类似以下的防御性编程：

```python
try:
    await plugin.handle_message(message)
except Exception as e:
    logger.error(f"插件 {plugin.name} 运行错误: {e}")
    await self.reply_text(message.chat_id, "⚠️ 抱歉，该功能运行出错，请联系管理员。")
    # 绝对不能让这个异常导致 main loop 退出
```

**原则**: 主进程永不崩溃。单个插件的未捕获异常必须被 HubBot 捕获并记录，同时向用户回复友好的错误提示。
