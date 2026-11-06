#!/usr/bin/env python3
"""Issue a signed licence file for the pro tier.

The licence is a small JSON document bound to a machine identifier and an
expiry date, signed with the maintainer's Ed25519 key using the same primitives
as release verification (:mod:`hwmon.release`).

    python3 licensing/issue.py --key license-private.pem --machine <id> \
        --tier pro --days 365 --out license.json

Keep the private key offline. The public key ships inside the pro package and is
the only thing the verifier trusts.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

PAYLOAD_FIELDS = ("machine", "tier", "issued_at", "expires_at", "seat")


def canonical(payload: dict) -> bytes:
    """Deterministic byte form that is signed: sorted keys, no whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def issue(private_key: Path, machine: str, tier: str = "pro", days: int = 365, seat: int = 1) -> dict:
    key = serialization.load_pem_private_key(private_key.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError(f"{private_key} is not an Ed25519 private key")

    now = int(time.time())
    payload = {
        "machine": machine,
        "tier": tier,
        "issued_at": now,
        "expires_at": now + days * 86400,
        "seat": seat,
    }
    signature = key.sign(canonical(payload))
    return {
        "payload": payload,
        "signature": base64.b64encode(signature).decode(),
        "alg": "ed25519",
        "version": 1,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Issue a signed hwmon pro licence")
    ap.add_argument("--key", required=True, help="Ed25519 private key (PEM)")
    ap.add_argument("--machine", required=True, help="machine identifier the licence is bound to")
    ap.add_argument("--tier", default="pro", choices=("pro", "trial", "site"))
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--seat", type=int, default=1)
    ap.add_argument("--out", help="write to this file instead of stdout")
    args = ap.parse_args(argv)

    doc = issue(Path(args.key), args.machine, args.tier, args.days, args.seat)
    text = json.dumps(doc, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(text)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
