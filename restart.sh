#!/usr/bin/env bash
set -eu
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-8000}"

echo "=== video-scraper restart ==="
bash "$SCRIPT_DIR/stop.sh" "$PORT"
echo ""
bash "$SCRIPT_DIR/start.sh"
