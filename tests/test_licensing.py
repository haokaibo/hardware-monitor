"""Licence issuing and verification: round trip plus every rejection path."""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path

import pytest

# licensing/ is a tool directory, not an installed package
LICENSING = Path(__file__).resolve().parent.parent / "licensing"
sys.path.insert(0, str(LICENSING))

import issue as issuer  # noqa: E402
import verify as verifier  # noqa: E402
from verify import LicenceError  # noqa: E402

from hwmon import release  # noqa: E402


@pytest.fixture
def keypair(tmp_path):
    priv = tmp_path / "license-private.pem"
    pub = tmp_path / "license-public.pem"
    release.generate_keypair(priv, pub)
    return priv, pub


@pytest.fixture
def licence(keypair):
    priv, _ = keypair
    return issuer.issue(priv, machine="machine-abc", tier="pro", days=30)


def test_issue_verify_roundtrip(keypair, licence):
    _, pub = keypair
    payload = verifier.verify(licence, pub, machine="machine-abc")
    assert payload["tier"] == "pro"
    assert payload["machine"] == "machine-abc"
    assert payload["expires_at"] > payload["issued_at"]


def test_canonical_form_is_shared(keypair):
    assert issuer.canonical({"b": 2, "a": 1}) == verifier.canonical({"a": 1, "b": 2})


def test_machine_mismatch_is_rejected(keypair, licence):
    _, pub = keypair
    with pytest.raises(LicenceError, match="different machine"):
        verifier.verify(licence, pub, machine="some-other-host")


def test_expiry_is_enforced(keypair, licence):
    _, pub = keypair
    future = licence["payload"]["expires_at"] + 1
    with pytest.raises(LicenceError, match="expired"):
        verifier.verify(licence, pub, machine="machine-abc", now=future)


def test_valid_just_before_expiry(keypair, licence):
    _, pub = keypair
    just_before = licence["payload"]["expires_at"] - 1
    assert verifier.verify(licence, pub, machine="machine-abc", now=just_before)["tier"] == "pro"


def test_tampered_payload_is_rejected(keypair, licence):
    _, pub = keypair
    licence["payload"]["tier"] = "site"
    with pytest.raises(LicenceError, match="signature does not match"):
        verifier.verify(licence, pub, machine="machine-abc")


def test_tampered_expiry_is_rejected(keypair, licence):
    _, pub = keypair
    licence["payload"]["expires_at"] += 10 * 365 * 86400
    with pytest.raises(LicenceError, match="signature does not match"):
        verifier.verify(licence, pub, machine="machine-abc")


def test_wrong_public_key_is_rejected(tmp_path, keypair, licence):
    _, _ = keypair
    other_priv = tmp_path / "o-private.pem"
    other_pub = tmp_path / "o-public.pem"
    release.generate_keypair(other_priv, other_pub)
    with pytest.raises(LicenceError, match="signature does not match"):
        verifier.verify(licence, other_pub, machine="machine-abc")


@pytest.mark.parametrize("field", ["payload", "signature", "alg", "version"])
def test_missing_envelope_field_is_rejected(keypair, licence, field):
    _, pub = keypair
    del licence[field]
    with pytest.raises(LicenceError, match=f"missing '{field}'"):
        verifier.verify(licence, pub)


@pytest.mark.parametrize("field", verifier.PAYLOAD_FIELDS)
def test_missing_payload_field_is_rejected(keypair, licence, field):
    _, pub = keypair
    del licence["payload"][field]
    with pytest.raises(LicenceError, match=f"missing '{field}'"):
        verifier.verify(licence, pub)


def test_unsupported_algorithm_is_rejected(keypair, licence):
    _, pub = keypair
    licence["alg"] = "rsa"
    with pytest.raises(LicenceError, match="unsupported algorithm"):
        verifier.verify(licence, pub)


def test_unsupported_version_is_rejected(keypair, licence):
    _, pub = keypair
    licence["version"] = 99
    with pytest.raises(LicenceError, match="unsupported licence version"):
        verifier.verify(licence, pub)


def test_bad_base64_signature_is_rejected(keypair, licence):
    _, pub = keypair
    licence["signature"] = "!!!not base64!!!"
    with pytest.raises(LicenceError, match="not valid base64"):
        verifier.verify(licence, pub)


def test_short_signature_is_rejected(keypair, licence):
    _, pub = keypair
    licence["signature"] = base64.b64encode(b"short").decode()
    with pytest.raises(LicenceError, match="signature does not match"):
        verifier.verify(licence, pub)


def test_licence_from_file_and_json_string(keypair, licence, tmp_path):
    _, pub = keypair
    path = tmp_path / "license.json"
    path.write_text(json.dumps(licence))
    assert verifier.verify(path, pub, machine="machine-abc")["tier"] == "pro"
    assert verifier.verify(json.dumps(licence), pub, machine="machine-abc")["tier"] == "pro"


def test_unreadable_licence_file(tmp_path, keypair):
    _, pub = keypair
    with pytest.raises(LicenceError, match="cannot read licence"):
        verifier.verify(tmp_path / "nope.json", pub)


def test_malformed_json_string(keypair):
    _, pub = keypair
    with pytest.raises(LicenceError, match="not JSON"):
        verifier.verify("{not json", pub)


def test_is_valid_helper(keypair, licence):
    _, pub = keypair
    assert verifier.is_valid(licence, pub, machine="machine-abc")
    assert not verifier.is_valid(licence, pub, machine="other")


def test_non_ed25519_public_key_is_rejected(tmp_path, licence):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = tmp_path / "rsa.pem"
    pem.write_bytes(
        rsa_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    with pytest.raises(LicenceError, match="not Ed25519"):
        verifier.verify(licence, pem)


def test_issue_cli_writes_a_file(keypair, tmp_path):
    priv, pub = keypair
    out = tmp_path / "out.json"
    assert issuer.main(["--key", str(priv), "--machine", "m1", "--days", "7", "--out", str(out)]) == 0
    payload = verifier.verify(out, pub, machine="m1")
    assert payload["expires_at"] - payload["issued_at"] == 7 * 86400
