"""Live board server (VM-side) — instant board updates, same moment as the ntfy ping.

The GitHub Pages board lags: VM push -> Pages rebuild (~20-90s) -> phone poll. This serves
docs/ DIRECTLY off the VM so a fresh bake is live the instant dashboard.py writes it, and
pushes a Server-Sent-Events 'reload' the moment index.html changes so connected phones
refresh in ~1s. Exposed publicly via a cloudflared quick tunnel (board_tunnel.sh); the Pages
board auto-redirects here when the tunnel is up and falls back to static Pages when it isn't.

Stdlib only (no deps), threaded, tiny footprint. Localhost:8899; the tunnel fronts HTTPS.
"""
from __future__ import annotations

import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DOCS = Path(__file__).resolve().parent / "docs"
INDEX = DOCS / "index.html"
PORT = 8899


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(DOCS), **kw)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")

    def end_headers(self):
        self._cors()
        super().end_headers()

    def do_GET(self):
        if self.path.split("?")[0] == "/events":
            return self._sse()
        return super().do_GET()

    def _sse(self):
        """Long-lived Server-Sent-Events stream: push 'reload' whenever index.html changes."""
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "keep-alive")
            self._cors()
            self.end_headers()
            last = INDEX.stat().st_mtime if INDEX.exists() else 0
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            beats = 0
            while True:
                time.sleep(1.0)
                cur = INDEX.stat().st_mtime if INDEX.exists() else 0
                if cur != last:
                    last = cur
                    self.wfile.write(b"data: reload\n\n")
                    self.wfile.flush()
                else:
                    beats += 1
                    if beats % 15 == 0:                       # keep-alive comment every 15s
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return                                            # client went away — fine

    def log_message(self, *a):                                # quiet
        pass


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"board_server on :{PORT} serving {DOCS}")
    srv.serve_forever()
