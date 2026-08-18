# -*- coding: utf-8 -*-
"""Serviço HTTP do índice CLIP (Fase 2). Roda na Xeon (localhost), exposto na VPS via túnel reverso.
Endpoints: GET /health ; POST /query ; POST /index  (POST exige header X-Busca-Secret)."""
import os, sys, json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, '/home/corexia/corexia-ia/busca')
import clip_index as ci

SECRET = os.environ.get("BUSCA_SECRET", "")
PORT = int(os.environ.get("BUSCA_PORT", "8765"))

def _warm():
    try:
        ci.model()
        print("CLIP quente", flush=True)
    except Exception as e:
        print("warm erro:", str(e)[:200], flush=True)
threading.Thread(target=_warm, daemon=True).start()

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def _send(self, code, obj):
        b = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
    def do_GET(self):
        if self.path.startswith("/health"):
            self._send(200, {"ok": True, "model": ci._MODEL is not None})
        else:
            self._send(404, {"error": "nao encontrado"})
    def do_POST(self):
        if SECRET and self.headers.get("X-Busca-Secret", "") != SECRET:
            self._send(403, {"error": "forbidden"})
            return
        try:
            ln = int(self.headers.get("Content-Length", "0") or 0)
            body = json.loads(self.rfile.read(ln) or b"{}")
        except Exception:
            self._send(400, {"error": "json invalido"})
            return
        try:
            if self.path.startswith("/query"):
                r = ci.query(body.get("text", ""), body.get("camera_key", ""), body.get("date", ""),
                             body.get("t0"), body.get("t1"), int(body.get("topk", 30)),
                             float(body.get("min_score", 0.0)))
                self._send(200, r)
            elif self.path.startswith("/index"):
                n = ci.index_segment(body.get("camera_key", ""), body.get("date", ""), body.get("arquivo", ""),
                                     body.get("inicio", "00:00:00"), body.get("signed_url", ""),
                                     float(body.get("step", 3.0)), int(body.get("max_frames", 0)))
                self._send(200, {"indexed": n})
            else:
                self._send(404, {"error": "nao encontrado"})
        except Exception as e:
            self._send(500, {"error": str(e)[:200]})

if __name__ == "__main__":
    print("busca_service escutando 127.0.0.1:%d" % PORT, flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
