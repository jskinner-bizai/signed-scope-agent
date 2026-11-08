"""Builds, uses and destroys the per-run sandbox with Docker.

For each run:
  * an internal Docker network with no route out (docker network create --internal)
  * a scope proxy container, the only thing on that network that can reach outside,
    enforcing the approved scope and logging every request
  * short-lived fetch containers on the internal network (read-only root, no
    capabilities, CPU/memory/PID caps, killed at the wall-clock cap)
  * short-lived code containers with no network at all
At the end everything is removed and the removal is verified.
"""
import json
import os
import pathlib
import subprocess
import time
import uuid

IMAGE = os.environ.get("SANDBOX_IMAGE", "python:3.12-slim")
ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = pathlib.Path(os.environ.get("SSA_DATA", ROOT / "data"))
DEMO_UPSTREAM = os.environ.get("DEMO_UPSTREAM", "http://host.docker.internal:8000")


def sh(args, timeout=120):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


class Sandbox:
    def __init__(self, run_id, scope, emit):
        self.run_id = run_id
        self.scope = scope
        self.emit = emit
        self.net = f"ssa-net-{run_id}"
        self.proxy = f"ssa-proxy-{run_id}"
        self.dir = DATA / "runs" / run_id
        (self.dir / "scope").mkdir(parents=True, exist_ok=True)
        (self.dir / "log").mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir / "log", 0o777)
        self.containers = []
        self._egress_seen = 0

    # ------------------------------------------------------------ lifecycle
    def write_scope(self):
        (self.dir / "scope" / "scope.json").write_text(json.dumps(self.scope))

    def build(self):
        self.write_scope()
        sh(["docker", "network", "create", "--internal", self.net])
        r = sh(["docker", "run", "-d", "--name", self.proxy, "--network", "bridge",
                "--add-host", "host.docker.internal:host-gateway",
                "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--memory", "128m", "--pids-limit", "64",
                "-e", f"DEMO_UPSTREAM={DEMO_UPSTREAM}",
                "-v", f"{self.dir / 'scope'}:/scope:ro",
                "-v", f"{self.dir / 'log'}:/log",
                "-v", f"{ROOT / 'proxy'}:/app:ro",
                IMAGE, "python", "/app/scope_proxy.py"])
        if r.returncode != 0:
            raise RuntimeError("proxy failed to start: " + r.stderr[-300:])
        sh(["docker", "network", "connect", "--alias", "scope-proxy", self.net, self.proxy])
        self.containers.append(self.proxy)
        time.sleep(1.0)
        self.emit("sandbox", f"Sandbox built for run {self.run_id}: internal network with no route out, "
                             f"scope proxy as the only exit, caps {self.scope['cpu']} CPU / "
                             f"{self.scope['memory_mb']} MB / {self.scope['wall_seconds']} s.",
                  {"network": self.net, "proxy": self.proxy})

    def destroy(self):
        for c in self.containers:
            sh(["docker", "rm", "-f", c])
        sh(["docker", "network", "rm", self.net])
        left = sh(["docker", "ps", "-a", "--filter", f"name=ssa-.*{self.run_id}", "--format", "{{.Names}}"]).stdout.split()
        net_left = sh(["docker", "network", "ls", "--filter", f"name={self.net}", "--format", "{{.Name}}"]).stdout.split()
        destroyed = not left and not net_left
        self.emit("sandbox", "Sandbox destroyed and verified gone." if destroyed else
                  f"Sandbox teardown incomplete: {left + net_left}",
                  {"destroyed": destroyed, "remaining": left + net_left})
        return destroyed

    # ------------------------------------------------------------ caps
    def _caps(self):
        return ["--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--user", "65534:65534",
                "--cpus", str(self.scope["cpu"]), "--memory", f"{self.scope['memory_mb']}m",
                "--memory-swap", f"{self.scope['memory_mb']}m", "--pids-limit", "64",
                "--tmpfs", "/tmp:rw,size=16m"]

    def _run(self, name, args, wall):
        start = time.time()
        try:
            r = sh(["docker", "run", "--name", name] + args, timeout=wall + 10)
            timed_out = False
            out, err, code = r.stdout, r.stderr, r.returncode
        except subprocess.TimeoutExpired:
            sh(["docker", "kill", name])
            out, err, code, timed_out = "", "", None, True
        insp = sh(["docker", "inspect", name, "--format", "{{json .State}}"]).stdout.strip()
        state = json.loads(insp) if insp else {}
        sh(["docker", "rm", "-f", name])
        return {"stdout": out, "stderr": err, "exit": code, "timed_out": timed_out,
                "oom_killed": state.get("OOMKilled", False), "elapsed_s": round(time.time() - start, 2)}

    # ------------------------------------------------------------ tools
    def fetch(self, url):
        name = f"ssa-fetch-{self.run_id}-{uuid.uuid4().hex[:6]}"
        args = self._caps() + ["--network", self.net,
                               "-e", "HTTP_PROXY=http://scope-proxy:3128",
                               "-e", "http_proxy=http://scope-proxy:3128",
                               "-v", f"{ROOT / 'sandbox'}:/sb:ro",
                               IMAGE, "python", "/sb/fetch.py", url]
        res = self._run(name, args, self.scope["wall_seconds"])
        self.emit("process", f"Fetch container ran: python fetch.py {url}",
                  {"container": name, "exit": res["exit"], "elapsed_s": res["elapsed_s"],
                   "timed_out": res["timed_out"]})
        self.drain_egress()
        try:
            return json.loads(res["stdout"].strip().splitlines()[-1])
        except Exception:
            return {"url": url, "status": 0, "error": (res["stderr"] or "no output")[-300:]}

    def run_code(self, label, files):
        """files: {"prices.csv": text, "prog.py": text}. Runs prog.py with no network."""
        name = f"ssa-code-{self.run_id}-{uuid.uuid4().hex[:6]}"
        indir = self.dir / f"in-{name}"
        indir.mkdir()
        for fn, content in files.items():
            (indir / fn).write_text(content)
        wall = self.scope["wall_seconds"]
        args = self._caps() + ["--network", "none",
                               "--tmpfs", "/work:rw,size=8m,mode=1777",
                               "-v", f"{indir}:/in:ro",
                               "-v", f"{ROOT / 'sandbox'}:/sb:ro",
                               IMAGE, "python", "/sb/harness.py", "/in/prog.py", str(max(wall - 5, 5))]
        res = self._run(name, args, wall)
        report = {}
        try:
            report = json.loads(res["stdout"].strip().splitlines()[-1])
        except Exception:
            report = {"harness_output_missing": True, "stderr": res["stderr"][-500:]}
        self.emit("process", f"Code container ran {label} with no network.",
                  {"container": name, "exit": report.get("exit_code", res["exit"]),
                   "timed_out": report.get("timed_out", res["timed_out"]),
                   "oom_killed": res["oom_killed"], "elapsed_s": res["elapsed_s"]})
        for f in report.get("work_files", []):
            self.emit("file", f"Wrote {f['path']} ({f['bytes']} bytes) inside the sandbox tmpfs.", f)
        return report

    def drain_egress(self):
        path = self.dir / "log" / "egress.jsonl"
        if not path.exists():
            return []
        lines = path.read_text().splitlines()
        new = [json.loads(l) for l in lines[self._egress_seen:]]
        self._egress_seen = len(lines)
        for ev in new:
            verdict = ev.get("verdict")
            self.emit("network", f"{verdict.upper()}: {ev.get('method')} {ev.get('url')}"
                      + (f" ({ev.get('reason')})" if ev.get("reason") else ""), ev)
        return new
