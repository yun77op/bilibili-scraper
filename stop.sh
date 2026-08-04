#!/usr/bin/env bash
set -eu
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${1:-8000}"
PID_DIR="$SCRIPT_DIR/.pids"

stop_by_pid_file() {
    local pid_file="$1"
    local name="$2"
    if [ -f "$pid_file" ]; then
        local pid
        pid=$(cat "$pid_file" 2>/dev/null || true)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            echo "Stopping $name (PID: $pid)"
            kill "$pid" 2>/dev/null || true
            for i in 1 2 3 4 5; do
                if ! kill -0 "$pid" 2>/dev/null; then break; fi
                sleep 1
            done
            if kill -0 "$pid" 2>/dev/null; then
                echo "Force stopping $name (PID: $pid)"
                kill -9 "$pid" 2>/dev/null || true
            fi
        fi
        rm -f "$pid_file"
    fi
}

stop_by_pid_file "$PID_DIR/server.pid" "server"
stop_by_pid_file "$PID_DIR/worker.pid" "worker"

# Fallback: port-based
SERVER_PID=$(lsof -ti ":$PORT" 2>/dev/null || true)
if [ -n "$SERVER_PID" ]; then
    echo "Stopping process on port $PORT (PID: $SERVER_PID)"
    kill "$SERVER_PID" 2>/dev/null || true
    sleep 1
    if kill -0 "$SERVER_PID" 2>/dev/null; then
        kill -9 "$SERVER_PID" 2>/dev/null || true
    fi
fi

# Fallback: worker.py processes
WORKER_PIDS=$(pgrep -f "python.*worker.py" 2>/dev/null || true)
if [ -n "$WORKER_PIDS" ]; then
    for pid in $WORKER_PIDS; do
        echo "Stopping orphan worker (PID: $pid)"
        kill "$pid" 2>/dev/null || true
    done
fi

echo "All stopped."
