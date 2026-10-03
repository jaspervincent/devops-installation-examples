#!/usr/bin/env python3
"""本地反向代理 + Chrome 无头截图，用来真正"看见"Grafana 面板。

背景：这台 Grafana 没装 image renderer 插件，/render 返回 500；而 Chrome 无头
把 URL 里的 user:pass 丢掉，直接访问会停在登录页。于是在本机起一个只转发到
Grafana 的小代理，由它补上 Authorization 头，Chrome 只访问 127.0.0.1。
全程不碰对端任何配置。

用法: grafana-shot.py <uid> <panelId> <from> <to> <输出png>
"""
import base64
import http.server
import socketserver
import subprocess
import sys
import threading
import urllib.error
import urllib.request

UP = "http://10.0.0.101:3000"
AUTH = base64.b64encode(b"admin:admin").decode()
PORT = 8899
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


class Proxy(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _go(self, body=None):
        req = urllib.request.Request(UP + self.path, data=body, method=self.command)
        req.add_header("Authorization", "Basic " + AUTH)
        for h in ("Content-Type", "Accept", "User-Agent", "x-grafana-org-id"):
            if self.headers.get(h):
                req.add_header(h, self.headers[h])
        try:
            r = urllib.request.urlopen(req, timeout=30)
            data, code, hdrs = r.read(), r.status, r.headers
        except urllib.error.HTTPError as e:
            data, code, hdrs = e.read(), e.code, e.headers
        except Exception as e:
            data, code, hdrs = str(e).encode(), 502, {}
        self.send_response(code)
        for k in ("Content-Type", "Cache-Control"):
            if hdrs and hdrs.get(k):
                self.send_header(k, hdrs[k])
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._go()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self._go(self.rfile.read(n) if n else b"")


def main():
    uid, pid, frm, to, out = sys.argv[1:6]
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", PORT), Proxy)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    url = ("http://127.0.0.1:%d/d-solo/%s/x?panelId=%s&from=%s&to=%s&theme=light"
           % (PORT, uid, pid, frm, to))
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    "--window-size=1500,560", "--virtual-time-budget=30000",
                    "--screenshot=" + out, url],
                   capture_output=True, text=True)
    srv.shutdown()
    print("截图: %s  (面板 %s@%s)" % (out, pid, uid))


if __name__ == "__main__":
    main()
