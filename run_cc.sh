#!/usr/bin/env bash
# Claude Code 专属机器人服务管理脚本
# 用法: ./run_cc.sh {start|stop|restart|status|log}

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$PROJECT_DIR/.cc_bot.pid"
LOG_FILE="$PROJECT_DIR/.cc_bot.log"
PYTHON="${PYTHON:-python3}"
PROC_NAME="cc_agent"       # 进程名，用于 ps/pgrep/pkill 精准识别

# CC 专属机器人配置目录和数据目录，与 hub_agent 隔离
export CC_CONFIG_DIR="$PROJECT_DIR/config/cc"
export CC_DATA_DIR="$PROJECT_DIR/data/cc_agent"

_is_running() {
    if [ ! -f "$PID_FILE" ]; then
        return 1
    fi
    local pid
    pid=$(cat "$PID_FILE")
    # 进程存活检查
    kill -0 "$pid" 2>/dev/null || return 1
    # 验证进程命令行包含 PROC_NAME，防止 PID 被其他进程复用导致误判
    if [ -f "/proc/${pid}/cmdline" ]; then
        # Linux: 通过 procfs 检查（cmdline 以 NUL 分隔，grep 仍可匹配）
        grep -q "$PROC_NAME" "/proc/${pid}/cmdline" 2>/dev/null
    else
        # 非 Linux（如 macOS）降级：通过 ps args 检查
        ps -p "$pid" -o args= 2>/dev/null | grep -q "$PROC_NAME"
    fi
}

# 从 config/cc/claude_code.yaml 读取权限服务器端口，缺省 9876
_get_perm_port() {
    local port=""
    if [ -f "$CC_CONFIG_DIR/claude_code.yaml" ]; then
        port=$(awk '/^[[:space:]]*permission_server_port:[[:space:]]*/{ print $2; exit }' \
            "$CC_CONFIG_DIR/claude_code.yaml" 2>/dev/null || true)
    fi
    echo "${port:-9876}"
}

# 启动前环境预检，任一失败则打印错误并返回 1
_preflight_check() {
    local failed=false

    # 1. Python 可用性
    if ! "$PYTHON" -c "" 2>/dev/null; then
        echo "  [错误] Python 解释器不可用: $PYTHON"
        failed=true
    fi

    # 2. 配置文件
    if [ ! -f "$CC_CONFIG_DIR/system.yaml" ]; then
        echo "  [错误] CC 系统配置文件不存在，请参考 config/cc/system.yaml.example 创建"
        failed=true
    fi

    # 3. CC 插件入口
    if [ ! -f "$PROJECT_DIR/plugins/claude_code/__main__.py" ]; then
        echo "  [错误] CC 插件入口不存在: $PROJECT_DIR/plugins/claude_code/__main__.py"
        failed=true
    fi

    # 4. 权限服务器端口占用检查
    local port
    port=$(_get_perm_port)
    local holder_pid="" holder_cmd="" port_occupied=false

    if command -v ss >/dev/null 2>&1; then
        if ss -tlnp 2>/dev/null | grep -q ":${port}[[:space:]]"; then
            port_occupied=true
            holder_pid=$(ss -tlnp 2>/dev/null \
                | sed -n "/:${port}[[:space:]]/s/.*pid=\([0-9]*\).*/\1/p" \
                | head -1 || true)
        fi
    elif command -v lsof >/dev/null 2>&1; then
        if lsof -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1; then
            port_occupied=true
            holder_pid=$(lsof -iTCP:"${port}" -sTCP:LISTEN -t 2>/dev/null | head -1 || true)
        fi
    fi

    if [ "$port_occupied" = true ]; then
        [ -n "$holder_pid" ] && \
            holder_cmd=$(ps -p "$holder_pid" -o comm= 2>/dev/null || true)
        echo "  [错误] 权限服务器端口 $port 已被占用" \
            "${holder_pid:+(PID: $holder_pid${holder_cmd:+, $holder_cmd})}"
        echo "         若为未正常退出的机器人进程，请先执行: ./run_cc.sh stop"
        echo "         若为其他进程，请修改 config/cc/claude_code.yaml 中的 permission_server_port"
        failed=true
    fi

    [ "$failed" = false ]
}

do_start() {
    if _is_running; then
        echo "CC 机器人已在运行中 (PID: $(cat "$PID_FILE"))"
        return 0
    fi
    echo "正在检查运行环境..."
    if ! _preflight_check; then
        echo "预检未通过，启动取消"
        return 1
    fi
    echo "正在启动 CC 机器人..."
    cd "$PROJECT_DIR"
    nohup bash -c "exec -a \"$PROC_NAME\" \"$PYTHON\" -m plugins.claude_code" >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    sleep 1
    if _is_running; then
        echo "启动成功 (PID: $(cat "$PID_FILE"))，日志: $LOG_FILE"
    else
        echo "启动失败，请查看日志: $LOG_FILE"
        rm -f "$PID_FILE"
        tail -20 "$LOG_FILE"
        return 1
    fi
}

do_stop() {
    if ! _is_running; then
        echo "CC 机器人未在运行"
        rm -f "$PID_FILE"
        return 0
    fi
    local pid
    pid=$(cat "$PID_FILE")
    echo "正在停止 CC 机器人 (PID: $pid)..."
    kill "$pid"
    # 等待进程退出，最多 10 秒
    for i in $(seq 1 10); do
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "已停止"
            rm -f "$PID_FILE"
            return 0
        fi
        sleep 1
    done
    # 超时强杀
    echo "进程未响应，强制终止..."
    kill -9 "$pid" 2>/dev/null || true
    rm -f "$PID_FILE"
    echo "已强制停止"
}

do_status() {
    if _is_running; then
        echo "CC 机器人正在运行 (PID: $(cat "$PID_FILE"))"
    else
        echo "CC 机器人未在运行"
        rm -f "$PID_FILE"
    fi
}

do_log() {
    if [ ! -f "$LOG_FILE" ]; then
        echo "日志文件不存在"
        return 1
    fi
    tail -50 "$LOG_FILE"
}

case "${1:-}" in
    start)   do_start ;;
    stop)    do_stop ;;
    restart) do_stop; do_start ;;
    status)  do_status ;;
    log)     do_log ;;
    *)
        echo "用法: $0 {start|stop|restart|status|log}"
        exit 1
        ;;
esac
