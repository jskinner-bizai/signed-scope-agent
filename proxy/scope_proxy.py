"""Scope-enforcing egress proxy.

Runs inside its own container, attached to the run's internal network (where the
sandbox lives) and to the outside network. It is the sandbox's only way out.

Every request is checked against the approved scope (scope.json, mounted
read-only) and logged to /log/egress.jsonl with the verdict, whatever happens.
"""
import http.server
import json
import os
import socketserver
import time
import urllib.parse
import urllib.request

SCOPE_PATH = os.environ.get("SCOPE_PATH", "/scope/scope.json")
LOG_PATH = os.environ.get("LOG_PATH", "/log/egress.jsonl")
# Demo supplier sites (*.demo) are served by the control app.
DEMO_UPSTREAM = os.environ.get("DEMO_UPSTREAM", "http://host.docker.internal:8000")


def load_scope():
    with open(SCOPE_PATH) as f:
        return json.load(f)


def log(event):
    event["ts"] = time.time()
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(event) + "\n")


class NoFollow(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect inside the proxy. The 3xx goes back to the sandbox,
    so the next hop is a new request that is checked against the scope again."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoFollow())


def host_allowed(host, scope):
    host = (host or "").lower().split(":")[0]
    for allowed in scope.get("allowed_domains", []):
        allowed = allowed.lower()
        if host == allowed or host.endswith("." + allowed):
            return True
    return False


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def _deny(self, host, reason):
        log({"kind": "network", "method": self.command, "url": self.path,
             "host": host, "verdict": "blocked", "reason": reason})
        body = json.dumps({"blocked": True, "host": host, "reason": reason}).encode()
        self.send_response(403)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Scope-Verdict", "blocked")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_CONNECT(self):
        host = self.path.split(":")[0]
        # TLS tunnels are not part of the demo scope; refuse them all so the
        # sandbox cannot open an opaque channel.
        self._deny(host, "tls tunnel not in scope")

    def _forward(self):
        url = urllib.parse.urlsplit(self.path)
        host = url.hostname or ""
        scope = load_scope()
        if self.command not in scope.get("allowed_methods", ["GET"]):
            return self._deny(host, f"method {self.command} not in scope")
        if not host_allowed(host, scope):
            return self._deny(host, "domain not in approved scope")
        if host.endswith(".demo"):
            target = DEMO_UPSTREAM + "/sites/" + host + (url.path or "/")
            if url.query:
                target += "?" + url.query
        else:
            target = self.path
        location = None
        try:
            req = urllib.request.Request(target, method=self.command,
                                         headers={"User-Agent": "signed-scope-sandbox"})
            with OPENER.open(req, timeout=10) as resp:
                data = resp.read(2_000_000)
                status = resp.status
                ctype = resp.headers.get("Content-Type", "application/octet-stream")
        except urllib.error.HTTPError as e:
            data, status, ctype = e.read(), e.code, "text/plain"
            location = e.headers.get("Location") if 300 <= e.code < 400 else None
        except Exception as e:  # upstream failure
            log({"kind": "network", "method": self.command, "url": self.path,
                 "host": host, "verdict": "error", "reason": str(e)[:200]})
            self.send_response(502)
            self.end_headers()
            return
        event = {"kind": "network", "method": self.command, "url": self.path,
                 "host": host, "verdict": "allowed", "status": status, "bytes": len(data)}
        if location:
            location = urllib.parse.urljoin(self.path, location)
            event["redirect_to"] = location
        log(event)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("X-Scope-Verdict", "allowed")
        if location:
            # Passed back, not followed: the next hop is checked like any other request.
            self.send_header("Location", location)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = _forward
    do_POST = _forward
    do_PUT = _forward
    do_HEAD = _forward
    do_DELETE = _forward


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    Server(("0.0.0.0", 3128), Handler).serve_forever()
