"""Runs inside the sandbox. Loads a page the way a simple browser would:
the page itself, then the images, scripts and stylesheets it references.
All traffic goes through the scope proxy (HTTP_PROXY); there is no other route out.
Prints one JSON object on stdout.
"""
import html.parser
import json
import sys
import urllib.parse
import urllib.request


class Parser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.text, self.links, self.resources, self.hidden = [], [], [], []
        self._skip = 0
        self._hidden_depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        style = (a.get("style") or "").replace(" ", "").lower()
        if self._hidden_depth or "display:none" in style or "hidden" in a:
            self._hidden_depth += 1
        if tag in ("script", "style"):
            self._skip += 1
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])
        if tag in ("img", "script") and a.get("src"):
            self.resources.append(a["src"])
        if tag == "link" and a.get("href"):
            self.resources.append(a["href"])

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        if self._hidden_depth:
            self._hidden_depth -= 1

    def handle_data(self, data):
        if self._skip:
            return
        d = data.strip()
        if not d:
            return
        (self.hidden if self._hidden_depth else self.text).append(d)


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            return r.status, r.headers.get("X-Scope-Verdict", ""), r.read(1_000_000)
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("X-Scope-Verdict", ""), e.read()
    except Exception as e:
        return 0, "error", str(e).encode()


def main():
    url = sys.argv[1]
    status, verdict, body = get(url)
    out = {"url": url, "status": status, "verdict": verdict, "requests": []}
    if status != 200:
        out["body"] = body.decode("utf-8", "replace")[:500]
        print(json.dumps(out))
        return
    ctype_html = body.lstrip()[:15].lower().startswith((b"<!doctype", b"<html"))
    if not ctype_html:
        out["raw"] = body.decode("utf-8", "replace")
        print(json.dumps(out))
        return
    p = Parser()
    p.feed(body.decode("utf-8", "replace"))
    out["text"] = "\n".join(p.text)
    out["hidden_text"] = "\n".join(p.hidden)
    out["links"] = [urllib.parse.urljoin(url, l) for l in p.links]
    for res in p.resources:
        full = urllib.parse.urljoin(url, res)
        s, v, _ = get(full)
        out["requests"].append({"url": full, "status": s, "verdict": v})
    print(json.dumps(out))


if __name__ == "__main__":
    main()
