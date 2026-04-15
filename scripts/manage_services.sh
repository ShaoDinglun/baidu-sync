#!/bin/bash

set -u

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
FRONTEND_DIR="$ROOT_DIR/frontend"
LOG_DIR="$ROOT_DIR/log"

BACKEND_PID_FILE="$LOG_DIR/backend.pid"
FRONTEND_PID_FILE="$LOG_DIR/frontend.pid"
FRONTEND_LOG_FILE="$LOG_DIR/frontend.out.log"

mkdir -p "$LOG_DIR"

usage() {
    cat <<EOF
用法: $(basename "$0") {start|stop|restart|status} [backend|frontend|all]

示例:
  ./scripts/manage_services.sh start
  ./scripts/manage_services.sh stop backend
  ./scripts/manage_services.sh restart frontend
EOF
}

get_backend_log_file() {
    echo "$LOG_DIR/backend.$(date +%F).out.log"
}

resolve_python_cmd() {
    if [ -n "${PYTHON_CMD:-}" ]; then
        echo "$PYTHON_CMD"
        return 0
    fi

    if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
        echo "$ROOT_DIR/.venv/bin/python"
        return 0
    fi

    if command -v python3 >/dev/null 2>&1; then
        echo "python3"
        return 0
    fi

    if command -v python >/dev/null 2>&1; then
        echo "python"
        return 0
    fi

    echo "未找到可用的 Python 命令，请安装 python3 或设置 PYTHON_CMD 环境变量" >&2
    exit 1
}

is_running() {
    local pid_file="$1"

    if [ ! -f "$pid_file" ]; then
        return 1
    fi

    local pid
    pid=$(cat "$pid_file")

    if [ -z "$pid" ]; then
        rm -f "$pid_file"
        return 1
    fi

    if kill -0 "$pid" >/dev/null 2>&1; then
        return 0
    fi

    rm -f "$pid_file"
    return 1
}

start_backend() {
    if is_running "$BACKEND_PID_FILE"; then
        echo "后端已在运行，PID: $(cat "$BACKEND_PID_FILE")"
        return 0
    fi

    local python_cmd
    python_cmd=$(resolve_python_cmd)

    local backend_log_file
    backend_log_file=$(get_backend_log_file)
    touch "$backend_log_file"

    (
        cd "$ROOT_DIR" || exit 1
        setsid bash -c "
            ROOT_DIR=\$1
            PYTHON_CMD=\$2
            LOG_DIR=\$3

            cd \"\$ROOT_DIR\" || exit 1

            if command -v stdbuf >/dev/null 2>&1; then
                    PYTHONUNBUFFERED=1 stdbuf -oL -eL \"\$PYTHON_CMD\" -m backend.web_app 2>&1
            else
                    PYTHONUNBUFFERED=1 \"\$PYTHON_CMD\" -m backend.web_app 2>&1
            fi | awk -v log_dir=\"\$LOG_DIR\" '{
                file = log_dir \"/backend.\" strftime(\"%F\") \".out.log\"
                print \$0 >> file
                fflush(file)
            }'
        " bash "$ROOT_DIR" "$python_cmd" "$LOG_DIR" >/dev/null 2>&1 < /dev/null &
        echo $! >"$BACKEND_PID_FILE"
    )

    sleep 1

    if is_running "$BACKEND_PID_FILE"; then
        echo "后端启动成功，PID: $(cat "$BACKEND_PID_FILE")"
        echo "后端日志: $backend_log_file"
        return 0
    fi

    echo "后端启动失败，请检查日志: $backend_log_file" >&2
    return 1
}

start_frontend() {
    if is_running "$FRONTEND_PID_FILE"; then
        echo "前端已在运行，PID: $(cat "$FRONTEND_PID_FILE")"
        return 0
    fi

    if ! command -v node >/dev/null 2>&1; then
        echo "未找到 node，请先安装 Node.js" >&2
        return 1
    fi

    if ! command -v npm >/dev/null 2>&1; then
        echo "未找到 npm，请先安装 npm" >&2
        return 1
    fi

    (
        cd "$FRONTEND_DIR" || exit 1

        if [ ! -d "node_modules" ]; then
            echo "[$(date '+%F %T')] 安装前端依赖" >>"$FRONTEND_LOG_FILE"
            npm install >>"$FRONTEND_LOG_FILE" 2>&1 || exit 1
        fi

        setsid npm run dev >>"$FRONTEND_LOG_FILE" 2>&1 < /dev/null &
        echo $! >"$FRONTEND_PID_FILE"
    )

    sleep 1

    if is_running "$FRONTEND_PID_FILE"; then
        echo "前端启动成功，PID: $(cat "$FRONTEND_PID_FILE")"
        echo "前端日志: $FRONTEND_LOG_FILE"
        return 0
    fi

    echo "前端启动失败，请检查日志: $FRONTEND_LOG_FILE" >&2
    return 1
}

stop_process() {
    local name="$1"
    local pid_file="$2"

    if ! is_running "$pid_file"; then
        echo "$name 未运行"
        return 0
    fi

    local pid
    pid=$(cat "$pid_file")
    kill -TERM "-$pid" >/dev/null 2>&1 || kill "$pid" >/dev/null 2>&1 || true

    local attempts=10
    while [ $attempts -gt 0 ]; do
        if ! kill -0 "$pid" >/dev/null 2>&1; then
            rm -f "$pid_file"
            echo "$name 已停止"
            return 0
        fi
        sleep 1
        attempts=$((attempts - 1))
    done

    kill -KILL "-$pid" >/dev/null 2>&1 || kill -9 "$pid" >/dev/null 2>&1 || true
    rm -f "$pid_file"
    echo "$name 已强制停止"
}

status_process() {
    local name="$1"
    local pid_file="$2"

    if is_running "$pid_file"; then
        echo "$name 运行中，PID: $(cat "$pid_file")"
    else
        echo "$name 未运行"
    fi
}

start_target() {
    local target="$1"

    case "$target" in
        backend)
            start_backend
            ;;
        frontend)
            start_frontend
            ;;
        all)
            start_backend
            start_frontend
            ;;
        *)
            echo "未知服务: $target" >&2
            usage
            exit 1
            ;;
    esac
}

stop_target() {
    local target="$1"

    case "$target" in
        backend)
            stop_process "后端" "$BACKEND_PID_FILE"
            ;;
        frontend)
            stop_process "前端" "$FRONTEND_PID_FILE"
            ;;
        all)
            stop_process "前端" "$FRONTEND_PID_FILE"
            stop_process "后端" "$BACKEND_PID_FILE"
            ;;
        *)
            echo "未知服务: $target" >&2
            usage
            exit 1
            ;;
    esac
}

status_target() {
    local target="$1"

    case "$target" in
        backend)
            status_process "后端" "$BACKEND_PID_FILE"
            ;;
        frontend)
            status_process "前端" "$FRONTEND_PID_FILE"
            ;;
        all)
            status_process "后端" "$BACKEND_PID_FILE"
            status_process "前端" "$FRONTEND_PID_FILE"
            ;;
        *)
            echo "未知服务: $target" >&2
            usage
            exit 1
            ;;
    esac
}

ACTION="${1:-}"
TARGET="${2:-all}"

if [ -z "$ACTION" ]; then
    usage
    exit 1
fi

case "$ACTION" in
    start)
        start_target "$TARGET"
        ;;
    stop)
        stop_target "$TARGET"
        ;;
    restart)
        stop_target "$TARGET"
        start_target "$TARGET"
        ;;
    status)
        status_target "$TARGET"
        ;;
    *)
        echo "未知操作: $ACTION" >&2
        usage
        exit 1
        ;;
esac