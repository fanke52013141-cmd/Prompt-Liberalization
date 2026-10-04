# -*- coding: utf-8 -*-
"""模拟一个 OpenAI 风格的 /v1/chat/completions 接口，用于端到端自测（不随产品交付承诺）。

用法：python mock_llm.py [端口]   默认 8901
行为：
  - 优化改写请求（system 含"提示词优化助手"）→ 返回带【改动说明】【新提示词】的改写结果
  - 普通生成请求 → 返回确定性模拟输出（改进版提示词会产生可区分的输出）
  - 缺少 Authorization → 401（用于验证错误提示）
"""
import json
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    server_version = "MockLLM/0.1"

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if not parsed.path.endswith("/chat/completions"):
            self._send(404, {"error": {"message": "not found"}})
            return
        auth = self.headers.get("Authorization") or ""
        if auth != "Bearer sk-test":
            self._send(401, {"error": {"message": " Incorrect API key provided."}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        messages = body.get("messages") or []
        content = self._respond(messages)
        resp = {
            "id": "mock-" + str(abs(hash(content)) % 10 ** 8),
            "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 60, "total_tokens": 180},
        }
        self._send(200, resp)

    def _respond(self, messages):
        system = next((m["content"] for m in messages if m.get("role") == "system"), "")
        user = next((m["content"] for m in messages if m.get("role") == "user"), "")
        if "提示词优化助手" in system:
            # 从用户消息里抠出【现在的提示词】段落，模拟一次针对性改写
            original = user.split("【现在的提示词】")[-1].split("【真实例子与评价】")[0].strip()
            improved = (original + "\n\n【改进要求（模拟）】\n"
                        "1. 先核对参考材料，再下判断；证据不足时明确说明，不编造。")
            return "【改动说明】加入先核对再判断的要求\n【新提示词】\n" + improved
        if "请只回复两个字" in user:
            return "正常"
        # 普通生成：改进版提示词带标记，产生可区分输出
        if "【改进要求（模拟）】" in user:
            tail = user.strip().splitlines()[-1] if user.strip() else ""
            return "【模拟·改进版输出】已按新规则处理。输入要点：%s" % tail[-30:]
        return "【模拟输出】已处理这条输入。末尾：%s" % user[-30:]

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8901
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    print("mock LLM on http://127.0.0.1:%d/v1" % port)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
