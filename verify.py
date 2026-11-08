#!/usr/bin/env python3
"""Verify a Signed Scope Agent receipt.

    python verify.py receipt.json            # checks the signature with the key in the receipt
    python verify.py receipt.json <pubkey>   # also pins the server's published public key

Needs: pip install pynacl
"""
import base64
import json
import sys

from nacl.signing import VerifyKey


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def main():
    signed = json.load(open(sys.argv[1]))
    key = sys.argv[2] if len(sys.argv) > 2 else signed["public_key"]
    if len(sys.argv) > 2 and key != signed["public_key"]:
        print("FAIL: receipt was signed by a different key than the one you pinned")
        sys.exit(1)
    try:
        VerifyKey(base64.b64decode(key)).verify(canonical(signed["receipt"]), base64.b64decode(signed["signature"]))
    except Exception:
        print("FAIL: signature does not match the receipt")
        sys.exit(1)
    r = signed["receipt"]
    blocked = sum(1 for e in r["events"] if e["kind"] == "network" and e["data"].get("verdict") == "blocked")
    print("OK: signature valid")
    print(f"run {r['run_id']}  scope {r['scope_hash'][:12]}  model {r['model']}")
    print(f"events {len(r['events'])}  blocked network attempts {blocked}  sandbox destroyed {r['sandbox_destroyed']}")


if __name__ == "__main__":
    main()
