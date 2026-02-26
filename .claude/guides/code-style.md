# 代码风格指南

## 类型提示 (Type Hints)

* 核心框架代码推荐添加。
* 插件代码可选。

## 文档字符串 (Docstrings)

* **强制**: 所有 `class`, `public method`, `interface` 必须包含详细的文档字符串。
* **风格**: Google Style (包含 `Args:`, `Returns:`, `Raises:` 等部分)。

## 命名与语言习惯

* 变量/函数名: 英文 (描述性命名)。
* **注释 & Commit Message**: **必须使用中文**。
* Commit Message 格式: `类型: 描述`
    * 例如: `功能: 新增天气查询插件`, `修复: 解决WebSocket重连失败问题`

## 并发规范

* 主线程处理飞书 WebSocket 事件（同步）。
* 阻塞型 I/O（子进程调用、流式 HTTP 请求等）必须放入 `threading.Thread` 中执行，避免阻塞主线程事件处理。
