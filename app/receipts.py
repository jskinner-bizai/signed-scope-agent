"""Signed run receipts (Ed25519).

The receipt is canonical JSON (sorted keys, no spaces). The signature covers the
whole receipt body. Anyone with the public key can verify it with verify.py.
"""
import base64
import hashlib
import json
import os
import pathlib

from nacl.signing import SigningKey, VerifyKey

DATA = pathlib.Path(os.environ.get("SSA_DATA", pathlib.Path(__file__).resolve().parent.parent / "data"))
KEY_PATH = DATA / "signing.key"


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _key():
    DATA.mkdir(parents=True, exist_ok=True)
    if not KEY_PATH.exists():
        KEY_PATH.write_bytes(bytes(SigningKey.generate()))
        os.chmod(KEY_PATH, 0o600)
    return SigningKey(KEY_PATH.read_bytes())


def public_key_b64():
    return base64.b64encode(bytes(_key().verify_key)).decode()


def scope_hash(scope):
    return hashlib.sha256(canonical(scope)).hexdigest()


def sign(body):
    sig = _key().sign(canonical(body)).signature
    return {"receipt": body, "signature": base64.b64encode(sig).decode(),
            "public_key": public_key_b64(), "alg": "ed25519"}


def verify(signed, public_key=None):
    try:
        vk = VerifyKey(base64.b64decode(public_key or signed["public_key"]))
        vk.verify(canonical(signed["receipt"]), base64.b64decode(signed["signature"]))
        return True
    except Exception:
        return False
