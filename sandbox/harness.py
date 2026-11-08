"""Runs inside the code sandbox (no network, read-only root, /work is a small tmpfs).
Executes one program with a wall-clock cap, then reports what happened and which
files exist under /work. Prints one JSON object on stdout as its last line.
"""
import json
import os
import signal
import subprocess
import sys
import time

prog = sys.argv[1]
cap = float(sys.argv[2]) if len(sys.argv) > 2 else 10
start = time.time()
result = {"program": prog, "wall_cap_s": cap}
try:
    p = subprocess.Popen([sys.executable, prog], cwd="/work", stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = p.communicate(timeout=cap)
        result.update(exit_code=p.returncode, stdout=out[-4000:], stderr=err[-4000:],
                      timed_out=False)
    except subprocess.TimeoutExpired:
        # Kill the whole process group, including anything a fork loop left behind.
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            out, err = p.communicate(timeout=2)
        except Exception:
            out, err = "", ""
        result.update(exit_code=None, timed_out=True, stdout=(out or "")[-2000:],
                      stderr=(err or "")[-2000:])
except Exception as e:  # harness could not start the program
    result.update(exit_code=None, timed_out=False, harness_error=str(e)[:300])
result["elapsed_s"] = round(time.time() - start, 2)
files = []
for root, _, names in os.walk("/work"):
    for n in names:
        path = os.path.join(root, n)
        try:
            files.append({"path": path, "bytes": os.path.getsize(path)})
        except OSError:
            pass
result["work_files"] = files
print(json.dumps(result))
