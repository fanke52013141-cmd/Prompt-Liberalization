"""提示词优化实验室 本地启动入口。

用法：
    python run_server.py            # 默认 127.0.0.1:8620
    python run_server.py --port 9000

只绑定本机回环地址（TC058）：不启用公网访问。
"""
from __future__ import annotations

import argparse
import os

import uvicorn

from prompt_lib.api import create_app
from prompt_lib.db import DB, DEFAULT_DB, set_db
from prompt_lib.runs import RunService
from prompt_core.instance_lock import acquire_instance_lock

_PORT_DEFAULT = 8620
_web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def main() -> None:
    parser = argparse.ArgumentParser(description="提示词优化实验室（本地版）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=_PORT_DEFAULT)
    parser.add_argument("--db", default=DEFAULT_DB, help="数据库路径；默认使用项目data目录")
    args = parser.parse_args()
    database_path = os.path.abspath(args.db)
    try:
        lock = acquire_instance_lock(database_path + ".service.lock")
    except OSError:
        parser.exit(1, "此数据库已有服务运行，请使用已有窗口或先停止服务。\n")
    database = None
    try:
        database = DB(database_path)
        set_db(database)
        app = create_app(_web_dir)
        RunService(database).recover_interrupted()
        print(f"提示词优化实验室已启动： http://{args.host}:{args.port}  （Ctrl+C 停止）")
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        if database is not None:
            database._conn.close()
        lock.close()


if __name__ == "__main__":
    main()
