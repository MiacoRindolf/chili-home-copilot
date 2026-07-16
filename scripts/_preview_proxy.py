"""HTTP->HTTPS proxy so the preview browser can drive the dockerized HTTPS app.
Serves http://localhost:8002 -> https://localhost:8001 (self-signed ignored)."""
import http.server
import ssl
import urllib.request

CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE
UP = "https://localhost:8001"


class H(http.server.BaseHTTPRequestHandler):
    def _fwd(self, method):
        body = None
        if "Content-Length" in self.headers:
            body = self.rfile.read(int(self.headers["Content-Length"]))
        req = urllib.request.Request(UP + self.path, data=body, method=method)
        for k, v in self.headers.items():
            if k.lower() not in ("host", "accept-encoding", "connection"):
                req.add_header(k, v)
        try:
            r = urllib.request.urlopen(req, context=CTX, timeout=120)
            data = r.read()
            self.send_response(r.status)
            for k, v in r.headers.items():
                if k.lower() not in ("transfer-encoding", "connection", "content-encoding", "content-length"):
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_error(502, str(e)[:100])

    def do_GET(self): self._fwd("GET")
    def do_POST(self): self._fwd("POST")
    def log_message(self, *a): pass


http.server.ThreadingHTTPServer(("127.0.0.1", 8002), H).serve_forever()
