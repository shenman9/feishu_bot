#!/usr/bin/env bash
# 飞书机器人服务管理脚本
# 用法: ./run.sh {start|stop|restart|status|log}

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$PROJECT_DIR/.bot.pid"
LOG_FILE="$PROJECT_DIR/.bot.log"
PYTHON="${PYTHON:-python3}"

_is_running() {
    [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

do_start() {
    if _is_running; then
        echo "机器人已在运行中 (PID: $(cat "$PID_FILE"))"
        return 0
    fi
    echo "正在启动机器人..."
    cd "$PROJECT_DIR"
    nohup "$PYTHON" main.py >> "$LOG_FILE" 2>&1 &
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
        echo "机器人未在运行"
        rm -f "$PID_FILE"
        return 0
    fi
    local pid
    pid=$(cat "$PID_FILE")
    echo "正在停止机器人 (PID: $pid)..."
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
        echo "机器人正在运行 (PID: $(cat "$PID_FILE"))"
    else
        echo "机器人未在运行"
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
