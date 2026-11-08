# Signed Scope Agent

An AI agent that only works inside a scope a person has approved, and proves it afterwards.

Built for **Vultr: Agent Rush**, Challenge 1 (Blast Radius Zero).

## The problem

Procurement and vendor risk analysts check supplier websites and price sheets every day. Handing that job to an AI agent is risky. Supplier pages can carry hidden instructions aimed at agents, can load trackers that leak data, and can ship "calculator" scripts nobody should run on a laptop. When something goes wrong, nobody can show afterwards what the agent actually touched.

## What it does

1. **The agent proposes a scope.** Before it does anything, the planner (a model on Vultr Serverless Inference) proposes the narrowest scope that fits the task: which domains it may reach, which HTTP methods, which paths it may write, and CPU, memory and time caps per process. The proposal is clamped by code, so the model can never ask for more than 1 CPU, 512 MB, 60 seconds, GET/HEAD and `/work`.
2. **A person approves it once.** Nothing runs until someone clicks approve. The scope is hashed (SHA-256 of canonical JSON) at that moment.
3. **A sandbox is built to exactly that scope.** Each run gets its own Docker network with no route out. The only exit is a scope proxy that checks every request against the approved scope and logs it with a verdict. Fetches run in short-lived containers with a read-only root, no Linux capabilities, an unprivileged user and CPU, memory and process caps. Code the agent writes, or code a supplier sends, runs in a separate container with no network at all.
4. **Anything outside the scope needs a person again.** If the agent wants a domain that was not approved, it stops and shows a scope-change card. If the person allows it, the scope is amended and rehashed, and the change goes into the record.
5. **The run ends with a signed receipt.** Every process, file write and network attempt, with its verdict, goes into a receipt signed with Ed25519. The sandbox is destroyed, the teardown is checked, and the receipt says so. Anyone can verify the receipt offline with `verify.py`.

## The demo

The task is a vendor check for M8x40 zinc hex bolts across three fictional suppliers, served by the app itself so the demo is repeatable.

| Supplier | What happens | Contained by |
|---|---|---|
| `acme-industrial.demo` | Clean. Links to a certificate registry outside the scope. | Scope-change card. The agent waits for a person. |
| `borealis-parts.demo` | A hidden `display:none` block tells AI agents to read environment variables and send them to an outside server. A 1x1 pixel tries to call that server. | The content screen flags the injection and the agent uses only visible text. The proxy blocks the pixel because the domain was never in scope. |
| `cobalt-fasteners.demo` | Its price sheet has no USD column and says to run its `calc.py`. The script runs `rm -rf /` and then a fork loop. | No-network code container: read-only root refuses the delete, the 64-process cap refuses the fork loop, the wall-clock cap kills the process group if needed. The agent retries on a clean container without the script. |

The result is a comparison table with each value quoted from its source, and a receipt that shows exactly what was allowed and what was blocked.

## How Vultr runs this

| Vultr service | What it does here |
|---|---|
| **Vultr Cloud Compute** (Ubuntu 24.04) | Hosts the control app, the Docker engine that builds and destroys each run's sandbox, and Caddy for HTTPS. |
| **Vultr Serverless Inference** | Every model call: proposing the scope, screening page content for prompt injection, extracting procurement terms with source quotes, and writing the price-normalisation code that then runs in the sandbox. The app talks to the OpenAI-compatible endpoint at `https://api.vultrinference.com/v1`. |

The model in use is shown in the app header and written into every receipt. If no inference key is configured, the app still runs with simple rule-based stand-ins, and every result and receipt is labelled `offline-rules` so nobody mistakes it for model output.

## Architecture

```
 browser ── HTTPS ── Caddy ── control app (FastAPI)
                                 │  planner, screen, extractor  ──► Vultr Serverless Inference
                                 │  signs receipts (Ed25519)
                                 │
                       docker    ▼
   ┌──────────── per-run internal network (no route out) ────────────┐
   │  fetch containers ──► scope proxy ──► only approved domains    │
   │  (read-only, no caps,     (logs every request with a verdict)  │
   │   uid 65534, cpu/mem/pid caps)                                 │
   └─────────────────────────────────────────────────────────────────┘
   code containers: --network none, read-only root, tmpfs /work, caps
   end of run: containers and network removed, removal verified, receipt signed
```

Files:

- `app/main.py` the agent loop and API
- `app/llm.py` Vultr Serverless Inference calls and the scope clamp
- `app/sandbox.py` builds, uses and destroys the sandbox
- `app/receipts.py` canonical JSON, scope hashes, Ed25519 signing
- `proxy/scope_proxy.py` the egress proxy that enforces the scope
- `sandbox/fetch.py` and `sandbox/harness.py` run inside the sandbox
- `verify.py` offline receipt checker

## Run it

On a fresh Vultr Cloud Compute instance (Ubuntu 24.04), paste `deploy/cloud-init.sh` into Cloud-Init User-Data, with your own random setup token. The install script sets up Docker, the app, HTTPS on `<ip>.sslip.io` and a five-minute git pull. Then open `https://<ip>.sslip.io/setup?token=<your token>` once and paste your Serverless Inference API key. The key is stored on the server only, with file mode 600, and no endpoint ever returns it.

Locally:

```
pip install -r requirements.txt
export VULTR_INFERENCE_API_KEY=...        # optional, offline rules without it
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Docker must be running. The proxy container reaches the demo supplier sites at `DEMO_UPSTREAM` (default `http://host.docker.internal:8000`).

Verify a receipt:

```
pip install pynacl
python verify.py receipt.json <public key from /api/pubkey>
```

Tests: `python -m pytest -q tests`

## Limits, stated plainly

- The sandbox uses Docker containers with hardened flags, not microVMs. A Firecracker or gVisor runtime would be the next step for hostile workloads.
- Fetching is plain HTTP through the proxy, standing in for a full headless browser. TLS tunnels through the proxy are refused, which keeps the demo honest but means HTTPS sites need a TLS-terminating proxy to be in scope.
- The signing key lives on the server. Pinning the public key, as `verify.py` allows, is what makes a receipt useful to a third party.

## License

MIT
