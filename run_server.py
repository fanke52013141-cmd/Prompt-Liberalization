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

_PORT_DEFAULT = 8620
_web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
app = create_app(_web_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description="提示词优化实验室（本地版）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=_PORT_DEFAULT)
    args = parser.parse_args()
    print(f"提示词优化实验室已启动： http://{args.host}:{args.port}  （Ctrl+C 停止）")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
