#!/usr/bin/env bash
set -eu
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 默认值，可通过环境变量 PORT / HOST 覆盖
PORT="${PORT:-8085}"
HOST="${HOST:-127.0.0.1}"

PID_DIR="$SCRIPT_DIR/.pids"
LOG_DIR="$SCRIPT_DIR/logs"
SERVER_LOG="$LOG_DIR/server.log"
WORKER_LOG="$LOG_DIR/worker.log"

echo "=== 视频转文章 — 启动 ==="

# 1. 停止已有进程
echo "[1/3] 停止已有进程..."
bash "$SCRIPT_DIR/stop.sh" "$PORT" 2>/dev/null || true
sleep 1

# 2. 检测 Python
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        PYTHON="$cmd"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo "ERROR: Python not found."
    exit 1
fi
echo "Python: $($PYTHON --version)"

# 3. 创建 PID 与日志目录
mkdir -p "$PID_DIR"
mkdir -p "$LOG_DIR"

# 3.5 检查 .env.local 配置文件
if [ ! -f "$SCRIPT_DIR/.env.local" ]; then
    echo "ERROR: 缺少配置文件 $SCRIPT_DIR/.env.local" >&2
    echo "      请先创建: cp $SCRIPT_DIR/.env.example $SCRIPT_DIR/.env.local" >&2
    exit 1
fi

# 4. 启动 HTTP 服务
echo "[2/3] 启动 HTTP 服务（端口 ${PORT}）..."
cd "$SCRIPT_DIR"
nohup "$PYTHON" server.py --host "$HOST" --port "$PORT" > "$SERVER_LOG" 2>&1 &
SERVER_PID=$!
echo "$SERVER_PID" > "$PID_DIR/server.pid"

# 等待服务就绪
for i in $(seq 1 10); do
    if curl -s "http://${HOST}:${PORT}/api/config" > /dev/null 2>&1; then
        break
    fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "ERROR: Server failed to start (check $SERVER_LOG)"
        exit 1
    fi
    sleep 1
done

if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "ERROR: Server process died (check $SERVER_LOG)"
    exit 1
fi
echo "  OK HTTP 服务已启动 (PID: $SERVER_PID)"

# 5. 启动 Worker
echo "[3/3] 启动后台 Worker..."
nohup "$PYTHON" worker.py > "$WORKER_LOG" 2>&1 &
WORKER_PID=$!
echo "$WORKER_PID" > "$PID_DIR/worker.pid"

sleep 1
if kill -0 "$WORKER_PID" 2>/dev/null; then
    echo "  OK Worker 已启动 (PID: $WORKER_PID)"
else
    echo "  WARN Worker 进程已退出，查看日志: $WORKER_LOG"
fi

echo ""
echo "----------------------------------------"
echo "  Web UI:    http://${HOST}:${PORT}"
echo "  Server log: $SERVER_LOG"
echo "  Worker log: $WORKER_LOG"
echo "  停止服务:   ./stop.sh"
echo "----------------------------------------"
