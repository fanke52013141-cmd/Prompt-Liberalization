"""复现：客户端在请求途中断开时，_dispatch 的兜底 except 会再写一次响应，
第二次写失败抛出未捕获异常，打印整段 traceback 到用户可见的控制台窗口。"""
import sys, os, threading, time, socket, sqlite3
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompt-lab"))
os.environ["PLAB_DB"] = ":memory:"

import server

PORT = server.find_free_port(8901)
httpd = server.ThreadingHTTPServer(("127.0.0.1", PORT), server.Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(0.4)

# 场景：发起请求后立刻粗暴断开（SYN RST），模拟关标签页
def rude_close():
    s = socket.socket()
    s.connect(("127.0.0.1", PORT))
    s.sendall(b"GET /api/state HTTP/1.1\r\nHost: x\r\n\r\n")
    time.sleep(0.002)
    # 设置 SO_LINGER 0 → 关闭时发 RST 而非 FIN
    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
    s.close()

for i in range(3):
    rude_close()
    time.sleep(0.3)

time.sleep(0.6)
print("=== 服务仍存活？ ===")
try:
    s = socket.create_connection(("127.0.0.1", PORT), timeout=3)
    s.sendall(b"GET /api/state HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    data = s.recv(200)
    print("存活，返回:", data[:80])
    s.close()
except Exception as e:
    print("服务不可用:", type(e).__name__, e)
httpd.shutdown()