import http.server, socketserver, functools, sys
port = int(sys.argv[1]); directory = sys.argv[2]

class ThreadedHTTP(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)
ThreadedHTTP(("", port), handler).serve_forever()
