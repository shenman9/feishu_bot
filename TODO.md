# 优化待办列表

> 按优先级排列，逐项推进。

---

## 一、高优先级 — 运行鲁棒性

### 1. ~~HubBot 插件调用增加异常隔离~~ ✅ 已完成

- **状态**：已在 commit `e07e2bf` 中修复，HubBot 的所有插件调用点已添加 try-except 保护。

---

### 2. CC 插件关键状态增加线程锁保护

- **文件**：`plugins/claude_code/claude_code_plugin.py`
- **问题**：CC 插件存在三类并发线程——①飞书 WebSocket 回调线程（触发 `handle_message` / `handle_card_action`）、②`_run_claude_code` 后台 worker 线程（执行子进程并更新状态）、③超时计时器线程（`_start_timeout_timer`）。三者同时读写以下共享状态，但无任何锁保护：
  - `state["running"]`：主线程在 line 1515 设为 True，worker 在 line 1084 的 finally 中设为 False，卡片回调在 line 1643 读取
  - `_running_processes`：worker 的 finally（line 1085）和 `_kill_process()` 都调用 `.pop()`
  - `_running_threads`：同上（line 1086）

  具体竞态场景：用户发 `/cancel` 终止任务 → `_kill_process()` 清理进程 → 用户立刻发新 prompt → 主线程设 `running=True` 并启动新 worker → 旧 worker 的 finally 执行，将 `running` 设回 False → 新任务的 `running` 标志被错误清除，系统认为没有任务在跑。

  注：虽然 `_perm_server_lock` 和 `_sessions_lock` 已存在，但关键的 user_states 和 processes 字典没有锁。
- **目标**：为 `user_states` 和 `_running_processes` / `_running_threads` 的关键操作引入 `threading.Lock`，消除上述竞态条件。

---

### 3. ~~权限服务器增加故障检测与恢复~~ ✅ 已完成

- **状态**：已在 commit `e365956` 中修复。权限服务器改为 OS 动态分配端口（`port=0`），`ensure_server()` 返回 bool 控制任务是否执行，hook 脚本改为 fail-close，进程退出时通过 atexit 清理端口文件。

---

## 二、中优先级 — 代码质量 & 可维护性

### 4. `_run_claude_code()` 方法拆分

- **文件**：`plugins/claude_code/claude_code_plugin.py:811-1096`
- **问题**：`_run_claude_code()` 是 CC 插件的核心执行方法，单个方法体长达约 286 行，包含以下职责：子进程启动与环境配置、stdout 流式读取循环、JSON 行解析与 segment 构建、飞书卡片节流更新、超时计时器管理、异常处理与错误卡片推送、finally 资源清理（进程终止、状态重置、权限注销、加急通知）。这么长的方法难以阅读、测试和维护，修改任一环节都需要理解整个方法的上下文。
- **目标**：将方法拆分为若干职责单一的子方法，如 `_read_output_stream()`（流读取与解析）、`_finalize_output()`（构建最终卡片内容）、`_cleanup_task()`（资源清理）等。主方法保留流程编排逻辑，每个子方法可独立理解和测试。

---

### 5. 插件用户状态增加过期清理机制

- **文件**：所有有状态插件的 `user_states` 字典 —— CC 插件（`claude_code_plugin.py:117`）、Claude Chat（`claude_chat_plugin.py`）、文件阅读（`file_reader_plugin.py`）、石头剪刀布（`rps_plugin.py`）
- **问题**：各插件通过 `self.user_states: dict[str, dict]` 维护用户会话状态。`_get_state()` 方法在用户首次交互时创建条目，但只有用户主动发送"退出"关键词时才会通过 `deactivate_user()` 清除。如果用户直接关闭飞书或长期不使用，其状态永远驻留内存。随着时间推移和用户数增长，内存占用持续增加且不可回收。对于 CC 插件尤为明显，每个用户状态包含 session_id、working_dir、权限模式等多个字段。
- **目标**：在各状态条目中记录 `last_activity` 时间戳，由基类或 HubBot 定期扫描并清理超过一定时间（如 7 天）未活跃的用户状态。

---

## 三、中优先级 — 功能增强

### 8. 会话消息队列——排队执行用户指令

- **涉及文件**：`plugins/claude_code/claude_code_plugin.py`（`handle_message` 入口及 `_run_claude_code` 流程）
- **问题**：当 CC 插件正在执行一轮会话（`state["running"] == True`）时，用户发送的新命令会被直接丢弃或返回"正在执行中"提示。用户必须等待上一轮执行完毕后手动重新发送，体验不佳。
- **目标**：为每个用户会话引入消息队列（如 `asyncio.Queue` 或 `collections.deque`），当会话正在执行时，将新到达的命令入队；上一轮执行完成后，自动取出队列中的下一条指令继续执行。需考虑：队列长度上限（避免堆积）、用户取消时清空队列、队列状态在 `/status` 中可见。

---

### 9. ~~支持读取飞书转发的合并消息（多选对话转发）~~ ✅ 已完成

- **状态**：已在 `feishu_bot.py` 中完整实现。通过 GetMessage API 获取合并转发子消息，利用 `upper_message_id` 构建消息树，递归格式化为带发送者、时间戳和缩进层级的对话文本，包裹 `<forwarded_messages>` 标签后传递给插件处理。支持嵌套转发（深度上限 10 层）、媒体占位提示、interactive 卡片降级提取。转发附带的留言消息会通过 2 秒时间窗口聚合为一条完整指令，解决群聊中转发消息无 @mention 导致丢失的问题。

---

## 四、低优先级 — 其他改进

### 10. 文件阅读插件编码探测增强

- **文件**：`plugins/file_reader/file_reader_plugin.py:110-116`
- **问题**：用户上传文件后，插件按 UTF-8 → GBK 顺序尝试解码。问题在于 GBK 编码范围很广，几乎所有字节序列都能被"成功"解码为 GBK 文本，导致实际为 Shift-JIS、EUC-KR、Latin-1 等编码的文件被错误地以 GBK 解码，用户看到的是乱码但系统不报错。
- **目标**：引入 `chardet`（或轻量的 `charset-normalizer`）库做自动编码探测，在置信度不足时提示用户文件编码无法确定。

---

### 11. config.py 增加启动时配置校验

- **文件**：`config.py:20-24`
- **问题**：`load_config()` 函数读取配置后，仅检查了 `app_id` 和 `app_secret` 两个必填项是否存在。其余配置项（如 `claude_code.permission_server_port` 是否为有效端口范围 0-65535、`timeout` 是否为正整数、`claude_path` 对应的文件是否存在且可执行等）完全不校验。错误配置只会在运行时某个具体功能触发时才暴露，报错信息往往是底层异常，难以定位到配置问题。
- **目标**：在 `load_config()` 或启动流程中增加关键配置项的类型和范围校验，启动时即报出明确的配置错误信息。

---
