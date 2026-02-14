# 测试指南

## 测试框架

* 使用 `pytest` 作为测试框架。
* 测试文件放在 `tests/` 目录下，按模块组织。

## 常用命令

* **运行全部测试**: `pytest`
* **运行指定插件测试**: `pytest plugins/some_plugin/`
* **运行指定测试目录**: `pytest tests/test_core/`

## 测试规范

* 测试文件命名: `test_<module>.py`
* 测试函数命名: `test_<功能描述>()`
* 异步测试使用 `@pytest.mark.asyncio` 装饰器。
* 所有 I/O 操作应使用 mock，避免真实网络调用。
