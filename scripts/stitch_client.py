"""Stitch MCP 直连客户端。

ZCode 客户端已配置 stitch（~/.zcode/cli/config.json），但本会话未加载为原生工具；
这里用等价的 JSON-RPC over HTTP 直连，能力与官方 MCP 工具一致。
用法：
    python scripts/stitch_client.py list_projects
    python scripts/stitch_client.py call <tool_name> '<json_args>'
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

CONFIG = r"C:\Users\Administrator\.zcode\cli\config.json"


def _srv():
    cfg = json.load(open(CONFIG, encoding="utf-8"))
    s = cfg["mcp"]["servers"]["stitch"]
    return s["url"], s["headers"]["X-Goog-Api-Key"]


def rpc(method: str, params: dict, _id: int = 1, timeout: int = 300) -> dict:
    url, key = _srv()
    req = urllib.request.Request(
        url, method="POST",
        data=json.dumps({"jsonrpc": "2.0", "id": _id, "method": method, "params": params}).encode(),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream",
                 "X-Goog-Api-Key": key})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def call_tool(name: str, args: dict, timeout: int = 300) -> dict:
    r = rpc("tools/call", {"name": name, "arguments": args}, timeout=timeout)
    if "error" in r:
        raise RuntimeError(json.dumps(r["error"], ensure_ascii=False)[:500])
    return r.get("result", {})


def list_tools() -> None:
    r = rpc("tools/list", {})
    for t in r.get("result", {}).get("tools", []):
        print(f"== {t['name']}")
        print((t.get("description") or "").strip()[:200])
        schema = t.get("inputSchema", {})
        req = schema.get("required", [])
        props = schema.get("properties", {})
        for k, v in props.items():
            star = "*" if k in req else " "
            print(f"   {star} {k}: {v.get('type', '?')}")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "list_tools":
        list_tools()
    elif len(sys.argv) >= 3 and sys.argv[1] == "call":
        args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
        out = call_tool(sys.argv[2], args)
        print(json.dumps(out, ensure_ascii=False, indent=1)[:4000])
    else:
        print("用法: stitch_client.py list_tools | call <tool> '<json>'")
