"""HTTP server — serves the web UI and API endpoints (Flask + waitress).

Usage::

    python server.py [--host 127.0.0.1] [--port 8085]

The server reads/writes job state via ``db.py`` and does **not** run a worker
thread.  Start ``worker.py`` separately to process queued jobs.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app import load_local_env, STATIC_DIR  # noqa: E402
from db import init_db  # noqa: E402

load_local_env()


def main() -> None:
    parser = argparse.ArgumentParser(description="视频转文章 HTTP 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8085, type=int)
    parser.add_argument("--threads", default=8, type=int,
                        help="waitress 工作线程数（默认 8）")
    args = parser.parse_args()

    init_db()
    STATIC_DIR.mkdir(exist_ok=True)

    from waitress import serve
    from web import create_app

    app = create_app()
    print(f"[server] Listening on http://{args.host}:{args.port} (threads={args.threads})")
    try:
        serve(app, host=args.host, port=args.port, threads=args.threads)
    except KeyboardInterrupt:
        print("\n[server] 已停止")


if __name__ == "__main__":
    main()
