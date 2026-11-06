"""Release signing: Ed25519 keypair, sign, verify, and fail-closed behaviour."""

from __future__ import annotations

import pytest

from hwmon import release
from hwmon.release import ReleaseVerificationError


@pytest.fixture
def keys(tmp_path):
    priv = tmp_path / "release-private.pem"
    pub = tmp_path / "release-public.pem"
    release.generate_keypair(priv, pub)
    return priv, pub


@pytest.fixture
def artefact(tmp_path):
    p = tmp_path / "hwmon-1.0.0.whl"
    p.write_bytes(b"pretend this is a wheel")
    return p


def test_keygen_creates_ed25519_pair(keys):
    priv, pub = keys
    assert b"BEGIN PRIVATE KEY" in priv.read_bytes()
    assert b"BEGIN PUBLIC KEY" in pub.read_bytes()
    # the private key must not be world readable
    assert priv.stat().st_mode & 0o077 == 0


def test_keygen_refuses_to_overwrite(keys):
    priv, pub = keys
    with pytest.raises(FileExistsError):
        release.generate_keypair(priv, pub)


def test_sign_and_verify_roundtrip(keys, artefact):
    priv, pub = keys
    sig = release.sign(artefact, priv)
    assert sig.name == artefact.name + ".sig"
    assert len(sig.read_bytes()) == 64  # Ed25519 signatures are fixed size
    release.verify(artefact, pub)  # must not raise
    assert release.is_verified(artefact, pub)


def test_modified_artefact_fails(keys, artefact):
    priv, pub = keys
    release.sign(artefact, priv)
    artefact.write_bytes(b"tampered")
    with pytest.raises(ReleaseVerificationError, match="does not match"):
        release.verify(artefact, pub)
    assert not release.is_verified(artefact, pub)


def test_missing_signature_fails_closed(keys, artefact):
    _, pub = keys
    with pytest.raises(ReleaseVerificationError, match="missing signature"):
        release.verify(artefact, pub)


def test_wrong_public_key_fails(tmp_path, keys, artefact):
    priv, _ = keys
    other_priv = tmp_path / "other-private.pem"
    other_pub = tmp_path / "other-public.pem"
    release.generate_keypair(other_priv, other_pub)
    release.sign(artefact, priv)
    with pytest.raises(ReleaseVerificationError, match="does not match"):
        release.verify(artefact, other_pub)


def test_non_ed25519_key_is_rejected(tmp_path, artefact):
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
    with pytest.raises(ReleaseVerificationError, match="not an Ed25519 public key"):
        release.verify(artefact, pem)


def test_garbage_signature_fails(keys, artefact):
    priv, pub = keys
    sig = release.sign(artefact, priv)
    sig.write_bytes(b"short")
    with pytest.raises(ReleaseVerificationError):
        release.verify(artefact, pub)
