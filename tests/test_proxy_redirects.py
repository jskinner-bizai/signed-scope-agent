"""The scope proxy must never follow a redirect to a host outside the scope."""
import http.server
import importlib.util
import json
import os
import pathlib
import tempfile
import threading
import urllib.error
import urllib.request

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _serve(handler):
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_redirect_out_of_scope_is_not_followed(code, monkeypatch):
    for k in ("no_proxy", "NO_PROXY", "http_proxy", "HTTP_PROXY"):
        monkeypatch.delenv(k, raising=False)
    hits = []

    class Outside(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200); self.end_headers(); self.wfile.write(b"secret")

    outside = _serve(Outside)

    class Inside(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            self.send_response(code)
            self.send_header("Location", f"http://127.0.0.1:{outside.server_port}/leak")
            self.end_headers()

    inside = _serve(Inside)

    tmp = tempfile.mkdtemp()
    scope_path, log_path = os.path.join(tmp, "scope.json"), os.path.join(tmp, "egress.jsonl")
    json.dump({"allowed_domains": ["localhost"], "allowed_methods": ["GET"]}, open(scope_path, "w"))
    os.environ["SCOPE_PATH"], os.environ["LOG_PATH"] = scope_path, log_path
    spec = importlib.util.spec_from_file_location("scope_proxy", ROOT / "proxy" / "scope_proxy.py")
    sp = importlib.util.module_from_spec(spec); spec.loader.exec_module(sp)
    proxy = sp.Server(("127.0.0.1", 0), sp.Handler)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{proxy.server_port}"}))
    with pytest.raises(urllib.error.HTTPError) as err:
        # The client follows the 3xx, so its second hop goes back through the proxy.
        opener.open(f"http://localhost:{inside.server_port}/start", timeout=5)
    assert err.value.code == 403
    assert hits == []
    events = [json.loads(l) for l in open(log_path)]
    assert events[0]["verdict"] == "allowed" and events[0]["redirect_to"].endswith("/leak")
    assert events[-1]["verdict"] == "blocked" and events[-1]["host"] == "127.0.0.1"
    for s in (outside, inside, proxy):
        s.shutdown()
