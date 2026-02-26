#!/bin/bash
# Claude Code PreToolUse Hook 脚本
#
# 由 Claude Code 在工具调用前自动调用（PreToolUse 事件）。
# 从 stdin 读取权限请求 JSON，通过 curl 转发给飞书机器人的权限确认服务器，
# 阻塞等待用户在飞书中点击「允许」或「拒绝」。
#
# 退出码:
#   0 = 允许工具调用继续
#   2 = 拒绝工具调用（stderr 内容反馈给 Claude）
#
# 端口号从 ~/.claude/.feishu_perm_port 文件读取。
# 如果端口文件不存在（飞书 Bot 未运行），直接允许（exit 0）。
# 执行过程记录到 ~/.claude/.feishu_hook.log，方便调试。

set -euo pipefail

# 日志文件
LOG_FILE="${HOME}/.claude/.feishu_hook.log"

_log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE" 2>/dev/null || true
}

# 读取 stdin（权限请求 JSON）
INPUT=$(cat)

# 提取 tool_name 和 session_id 用于日志（python3 解析，失败则显示原始）
TOOL_NAME=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_name','unknown'))" 2>/dev/null) || TOOL_NAME="unknown"
SESSION_ID=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('session_id','')[:8])" 2>/dev/null) || SESSION_ID="unknown"

_log "=== 权限请求: tool=${TOOL_NAME}, session=${SESSION_ID} ==="

# 只读工具白名单：与原始 Claude Code 默认行为保持一致，这些工具无需飞书卡片确认
case "$TOOL_NAME" in
    Read|Glob|Grep)
        _log "只读工具 (${TOOL_NAME})，自动放行"
        exit 0
        ;;
esac

# 读取端口号：文件不存在说明飞书 Bot 未运行，直接允许
PORT_FILE="${HOME}/.claude/.feishu_perm_port"
if [ ! -f "$PORT_FILE" ]; then
    _log "端口文件不存在 (${PORT_FILE})，直接允许"
    exit 0
fi
PORT=$(cat "$PORT_FILE")
_log "端口文件读取成功: port=${PORT}"

# 转发请求到权限确认服务器（阻塞等待响应）
_log "发送请求到权限服务器 http://127.0.0.1:${PORT}/permission-request ..."
CURL_EXIT=0
RESPONSE=$(echo "$INPUT" | curl -sS --max-time 180 \
    -X POST \
    -H "Content-Type: application/json" \
    -d @- \
    "http://127.0.0.1:${PORT}/permission-request" 2>/tmp/.feishu_hook_curl_err) || CURL_EXIT=$?

if [ $CURL_EXIT -ne 0 ]; then
    CURL_ERR=$(cat /tmp/.feishu_hook_curl_err 2>/dev/null || true)
    _log "curl 失败: exit=${CURL_EXIT}, err=${CURL_ERR}"
fi

# curl 失败或空响应：服务器未运行（飞书 Bot 未启动或权限服务懒初始化尚未完成），直接放行
if [ -z "$RESPONSE" ]; then
    _log "响应为空（权限服务器未运行），直接放行"
    exit 0
fi

_log "收到响应: ${RESPONSE}"

# 解析决策（使用 Python3 解析 JSON，避免依赖 jq）
DECISION=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('decision','deny'))" 2>/dev/null) || DECISION="deny"
REASON=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('reason',''))" 2>/dev/null) || REASON=""

_log "解析结果: decision=${DECISION}, reason=${REASON}"

if [ "$DECISION" = "allow" ]; then
    _log "决策: 允许"
    exit 0
else
    _log "决策: 拒绝 (reason=${REASON})"
    if [ -n "$REASON" ]; then
        echo "$REASON" >&2
    else
        echo "用户拒绝了此操作" >&2
    fi
    exit 2
fi
