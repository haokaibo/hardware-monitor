#!/usr/bin/env python3
"""Licence verification library.

This module is what gets compiled into (or shipped with) the pro tier. It must
stay small and dependency-light: only ``cryptography`` and the standard library.

Verification is fail-closed. Every rejection path raises
:class:`LicenceError` with a specific reason, so support can tell an expired
licence from a tampered one from a machine mismatch.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PAYLOAD_FIELDS = ("machine", "tier", "issued_at", "expires_at", "seat")
REQUIRED_FIELDS = ("payload", "signature", "alg", "version")


class LicenceError(Exception):
    """The licence is missing, malformed, expired, tampered with or mismatched."""


def canonical(payload: dict) -> bytes:
    """Must match ``licensing/issue.py``: sorted keys, no whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _load_public_key(source: Path | bytes) -> Ed25519PublicKey:
    raw = source.read_bytes() if isinstance(source, Path) else source
    try:
        key = serialization.load_pem_public_key(raw)
    except (ValueError, TypeError) as e:
        raise LicenceError(f"cannot load public key: {e}") from e
    if not isinstance(key, Ed25519PublicKey):
        raise LicenceError("public key is not Ed25519")
    return key


def verify(
    licence: dict | str | Path,
    public_key: Path | bytes,
    *,
    machine: str | None = None,
    now: float | None = None,
) -> dict:
    """Verify a licence document and return its payload.

    ``machine`` enables the binding check; ``now`` makes expiry testable.
    """
    if isinstance(licence, Path):
        try:
            licence = json.loads(licence.read_text())
        except (OSError, ValueError) as e:
            raise LicenceError(f"cannot read licence: {e}") from e
    elif isinstance(licence, str):
        try:
            licence = json.loads(licence)
        except ValueError as e:
            raise LicenceError(f"licence is not JSON: {e}") from e

    if not isinstance(licence, dict):
        raise LicenceError("licence must be a JSON object")

    for field in REQUIRED_FIELDS:
        if field not in licence:
            raise LicenceError(f"licence is missing '{field}'")
    if licence["alg"] != "ed25519":
        raise LicenceError(f"unsupported algorithm {licence['alg']!r}")
    if licence.get("version") != 1:
        raise LicenceError(f"unsupported licence version {licence.get('version')!r}")

    payload = licence["payload"]
    if not isinstance(payload, dict):
        raise LicenceError("licence payload must be an object")
    for field in PAYLOAD_FIELDS:
        if field not in payload:
            raise LicenceError(f"payload is missing '{field}'")

    try:
        signature = base64.b64decode(licence["signature"], validate=True)
    except (ValueError, TypeError) as e:
        raise LicenceError(f"signature is not valid base64: {e}") from e

    key = _load_public_key(public_key)
    try:
        key.verify(signature, canonical(payload))
    except InvalidSignature as e:
        raise LicenceError("signature does not match the payload") from e

    current = time.time() if now is None else now
    if payload["expires_at"] < current:
        raise LicenceError(f"licence expired at {time.strftime('%Y-%m-%d', time.gmtime(payload['expires_at']))}")
    if payload["issued_at"] > current + 86400:
        raise LicenceError("licence was issued in the future")

    if machine is not None and payload["machine"] != machine:
        raise LicenceError("licence is bound to a different machine")

    return payload


def is_valid(licence, public_key, *, machine: str | None = None, now: float | None = None) -> bool:
    """Boolean wrapper for callers that do not want exceptions."""
    try:
        verify(licence, public_key, machine=machine, now=now)
        return True
    except LicenceError:
        return False
