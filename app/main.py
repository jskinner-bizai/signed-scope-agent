"""Signed Scope Agent: control app.

The agent proposes a scope, a person approves it, a sandbox is built to exactly
that scope, the agent does the work inside it, and the run ends with a signed
receipt of everything that happened and proof the sandbox was destroyed.
"""
import csv
import io
import json
import os
import pathlib
import threading
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel

from . import llm, receipts
from .demo_sites import SITES
from .sandbox import DATA, Sandbox

app = FastAPI(title="Signed Scope Agent")
STATIC = pathlib.Path(__file__).parent / "static"
RUNS = {}
LOCK = threading.Lock()

# Optional pause between suppliers, so a live presentation can follow along. 0 by default.
DEMO_PACE = float(os.environ.get("DEMO_PACE", "0"))
DEFAULT_SUPPLIERS = ["acme-industrial.demo", "borealis-parts.demo", "cobalt-fasteners.demo"]
DEFAULT_TASK = ("Vendor check for M8x40 zinc hex bolts: for each supplier, confirm ISO 9001, payment terms, "
                "minimum order and lead time, and normalise the unit price from their price sheet.")


# ------------------------------------------------------------------ run state
class Run:
    def __init__(self, task, suppliers):
        self.id = uuid.uuid4().hex[:10]
        self.task = task
        self.suppliers = suppliers
        self.status = "proposed"
        self.events = []
        self.scope = None
        self.scope_model = None
        self.scope_hash = None
        self.pending_scope_change = None
        self.decision = threading.Event()
        self.decision_value = None
        self.results = {}
        self.receipt = None
        self.created = time.time()

    def emit(self, kind, message, data=None):
        ev = {"seq": len(self.events), "t": round(time.time() - self.created, 2), "kind": kind,
              "message": message, "data": data or {}}
        self.events.append(ev)
        return ev

    def public(self, since=0):
        return {"id": self.id, "task": self.task, "suppliers": self.suppliers, "status": self.status,
                "scope": self.scope, "scope_hash": self.scope_hash, "scope_model": self.scope_model,
                "pending_scope_change": self.pending_scope_change, "results": self.results,
                "events": self.events[since:], "receipt_ready": self.receipt is not None,
                "model": llm.model_name(), "online": llm.online()}


class NewRun(BaseModel):
    task: str = DEFAULT_TASK
    suppliers: list[str] = DEFAULT_SUPPLIERS


class Decision(BaseModel):
    approve: bool


class SetupKey(BaseModel):
    token: str
    key: str


# ------------------------------------------------------------------ the agent
def domain_of(url):
    return url.split("//", 1)[-1].split("/", 1)[0].split(":")[0].lower()


def agent(run: Run):
    sb = Sandbox(run.id, run.scope, run.emit)
    try:
        sb.build()
        for i, supplier in enumerate(run.suppliers):
            if i and DEMO_PACE:
                time.sleep(DEMO_PACE)
            run.emit("step", f"Checking {supplier}.")
            page = sb.fetch(f"http://{supplier}/")
            if page.get("status") != 200:
                run.results[supplier] = {"error": "page could not be loaded"}
                continue
            blocked = [r for r in page.get("requests", []) if r.get("verdict") == "blocked"]
            if blocked:
                run.emit("containment", f"{supplier} made the page load {len(blocked)} outside resource(s); "
                                        "the sandbox blocked them because those domains were never in scope.",
                         {"blocked": blocked})
            full_text = page.get("text", "") + "\n" + page.get("hidden_text", "")
            verdict, model = llm.screen_content(full_text)
            if verdict["injection"]:
                run.emit("containment", f"Prompt injection found on {supplier}. The planner refuses its "
                                        "instructions and uses only the visible facts.",
                         {"quote": verdict["quote"], "reason": verdict["reason"], "model": model})
                text_for_agent = page.get("text", "")
            else:
                text_for_agent = page.get("text", "")
            terms, model = llm.extract_terms(supplier, text_for_agent)
            terms["source"] = f"http://{supplier}/"
            terms["model"] = model
            run.results[supplier] = {"terms": terms, "injection_flagged": verdict["injection"]}

            # Out-of-scope link: ask the person, never follow silently.
            outside = [l for l in page.get("links", []) if domain_of(l) not in run.scope["allowed_domains"]]
            for link in outside[:1]:
                dom = domain_of(link)
                run.pending_scope_change = {"domain": dom, "url": link,
                                            "reason": f"{supplier} points to {dom} to confirm its certificate."}
                run.status = "awaiting_scope_change"
                run.emit("scope_request", f"The agent wants to visit {dom}, which is outside the approved scope. "
                                          "Waiting for a person to decide.", run.pending_scope_change)
                run.decision.clear()
                approved = run.decision.wait(timeout=180) and run.decision_value
                run.pending_scope_change = None
                run.status = "running"
                if approved:
                    run.scope["allowed_domains"].append(dom)
                    sb.scope = run.scope
                    sb.write_scope()
                    run.scope_hash = receipts.scope_hash(run.scope)
                    run.emit("scope", f"Scope amended by a person: added {dom}. New scope hash {run.scope_hash[:12]}.",
                             {"scope_hash": run.scope_hash})
                    reg = sb.fetch(link)
                    run.results[supplier]["registry_check"] = (reg.get("text") or "")[:300]
                else:
                    run.emit("scope", f"Request for {dom} declined. The agent continues without it.")

            # Price sheet
            sheet = sb.fetch(f"http://{supplier}/prices.csv")
            raw = sheet.get("raw", "")
            if not raw:
                continue
            headers = next(csv.reader(io.StringIO(raw)), [])
            vendor_script = [l for l in page.get("links", []) if l.endswith(".py")]
            if vendor_script and "unit_price_usd" not in headers:
                calc = sb.fetch(vendor_script[0])
                run.emit("step", f"{supplier} says its prices need its own calculator script. "
                                 "Running it only inside the no-network code sandbox.")
                rep = sb.run_code("supplier calculator calc.py",
                                  {"prices.csv": raw, "prog.py": calc.get("raw", "")})
                out = rep.get("stdout", "") + rep.get("stderr", "")
                evidence = []
                if "Read-only file system" in out or "Permission denied" in out:
                    evidence.append("rm -rf / was refused: read-only root filesystem, unprivileged user")
                if "Resource temporarily unavailable" in out:
                    evidence.append("fork loop was refused by the 64-process cap")
                if rep.get("timed_out"):
                    evidence.append("the wall-clock cap killed the whole process group")
                if evidence or rep.get("exit_code") not in (0, None):
                    run.emit("containment", f"{supplier}'s calculator is hostile. It tried to delete the filesystem "
                                            "and fork without limit. Contained: " + "; ".join(evidence or ["it exited with an error"])
                                            + ". It never had a network, and its container was removed.",
                             {"stdout_head": rep.get("stdout", "")[:300], "stderr_tail": rep.get("stderr", "")[-300:],
                              "timed_out": rep.get("timed_out"), "exit_code": rep.get("exit_code"), "evidence": evidence})
                run.emit("step", "Retrying the price analysis on a clean sandbox without the supplier's script.")
            code, model = llm.write_analysis_code(headers)
            rep = sb.run_code("agent analysis code", {"prices.csv": raw, "prog.py": code})
            try:
                rows = json.loads(rep.get("stdout", "").strip().splitlines()[-1])
            except Exception:
                rows = []
            run.results[supplier]["prices"] = rows
            run.results[supplier]["price_model"] = model
            if rows and all(r.get("unit_price_usd") is None for r in rows):
                run.results[supplier]["price_note"] = "No USD unit price on the sheet; not comparable without the vendor's script."
        run.emit("step", "All suppliers checked.")
    except Exception as e:
        run.emit("error", f"Run failed: {type(e).__name__}: {e}")
    finally:
        sb.drain_egress()
        destroyed = sb.destroy()
        finish(run, destroyed)


def finish(run, destroyed):
    body = {
        "run_id": run.id,
        "task": run.task,
        "scope": run.scope,
        "scope_hash": run.scope_hash,
        "scope_approved_by": "person (UI click)",
        "model": llm.model_name(),
        "inference": "Vultr Serverless Inference" if llm.online() else "offline rules (no inference key set)",
        "events": run.events,
        "results": run.results,
        "sandbox_destroyed": destroyed,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    run.receipt = receipts.sign(body)
    (DATA / "receipts").mkdir(parents=True, exist_ok=True)
    (DATA / "receipts" / f"{run.id}.json").write_text(json.dumps(run.receipt, indent=1))
    run.status = "done"


# ------------------------------------------------------------------ API
@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text()


@app.get("/verify", response_class=HTMLResponse)
def verify_page():
    return (STATIC / "verify.html").read_text()


@app.get("/api/health")
def health():
    return {"ok": True, "online": llm.online(), "model": llm.model_name()}


@app.get("/api/pubkey")
def pubkey():
    return {"alg": "ed25519", "public_key": receipts.public_key_b64()}


@app.post("/api/runs")
def create_run(body: NewRun):
    suppliers = [s.strip().lower() for s in body.suppliers if s.strip()][:5]
    run = Run(body.task[:600], suppliers)
    run.scope, run.scope_model = llm.propose_scope(run.task, suppliers)
    run.emit("scope", "Planner proposed a scope. Nothing runs until a person approves it.",
             {"scope": run.scope, "model": run.scope_model})
    with LOCK:
        RUNS[run.id] = run
    return run.public()


@app.post("/api/runs/{rid}/approve")
def approve(rid: str):
    run = RUNS.get(rid) or _404()
    if run.status != "proposed":
        raise HTTPException(409, "already started")
    run.scope_hash = receipts.scope_hash(run.scope)
    run.status = "running"
    run.emit("scope", f"Scope approved by a person. Scope hash {run.scope_hash[:12]}.", {"scope_hash": run.scope_hash})
    threading.Thread(target=agent, args=(run,), daemon=True).start()
    return run.public()


@app.post("/api/runs/{rid}/scope-change")
def scope_change(rid: str, d: Decision):
    run = RUNS.get(rid) or _404()
    if not run.pending_scope_change:
        raise HTTPException(409, "no pending request")
    run.emit("scope", "Person " + ("approved" if d.approve else "declined") + " the scope change.")
    run.decision_value = d.approve
    run.decision.set()
    return {"ok": True}


@app.get("/api/runs/{rid}")
def get_run(rid: str, since: int = 0):
    return (RUNS.get(rid) or _404()).public(since)


@app.get("/api/receipts/{rid}")
def get_receipt(rid: str):
    path = DATA / "receipts" / f"{rid}.json"
    if not path.exists():
        _404()
    return JSONResponse(json.loads(path.read_text()))


@app.post("/api/verify")
async def verify_receipt(request: Request):
    signed = await request.json()
    ok = receipts.verify(signed)
    ours = signed.get("public_key") == receipts.public_key_b64()
    return {"valid_signature": ok, "signed_by_this_server": ours}


@app.get("/sites/{host}/{path:path}")
def demo_site(host: str, path: str = ""):
    site = SITES.get(host) or _404()
    ctype, body = site.get("/" + path) or _404()
    return Response(body, media_type=ctype)


# ------------------------------------------------------------------ one-time setup
# The inference key is pasted once by the owner on /setup. The page is guarded by a
# random token written to data/setup.token at install time. The key is stored on the
# server only (data/inference.key, mode 600) and is never returned by any endpoint.
def _setup_token():
    path = DATA / "setup.token"
    return path.read_text().strip() if path.exists() else ""


@app.get("/setup", response_class=HTMLResponse)
def setup_page(token: str = ""):
    if not _setup_token() or token != _setup_token():
        raise HTTPException(404, "not found")
    return (STATIC / "setup.html").read_text()


@app.post("/api/setup")
def setup_key(body: SetupKey):
    if not _setup_token() or body.token != _setup_token():
        raise HTTPException(404, "not found")
    if len(body.key.strip()) < 10:
        raise HTTPException(400, "that does not look like a key")
    llm.save_key(body.key)
    try:
        model = llm.model_name()
        out, used = llm.chat_json("You are a connectivity check.", 'Return {"ok": true}.', max_tokens=20)
        return {"saved": True, "model": used, "test": out}
    except Exception as e:
        return {"saved": True, "model": llm.model_name(), "test_error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/verify.py", response_class=PlainTextResponse)
def verify_script():
    return (pathlib.Path(__file__).resolve().parent.parent / "verify.py").read_text()


def _404():
    raise HTTPException(404, "not found")
