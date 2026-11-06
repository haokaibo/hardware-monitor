"""Release artefact signing and verification (Ed25519).

Why this exists
---------------
The agent is installed by copying an artefact onto a monitored host and running
it as root under systemd. Verifying that artefact against a public key before
installation turns "I trust this download" into "I trust this signature", which
is the only part of the supply chain this project can actually control.

Design
------
* Ed25519 detached signatures over the artefact's SHA-256 digest.
* The digest is computed over the exact bytes of the file, so signatures are
  transport independent (tar, zip, scp, HTTP) and cheap to re-verify.
* Verification fails closed: a missing key, a malformed signature or a digest
  mismatch all raise :class:`ReleaseVerificationError`.
* The tool never touches the network and never runs the artefact.

Ordinary runtime does **not** import this module; only the CLI paths
``hwmon sign`` / ``hwmon verify`` and the release tooling do.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

SIGNATURE_SUFFIX = ".sig"


class ReleaseVerificationError(Exception):
    """The artefact could not be verified against the trusted public key."""


def digest(path: Path) -> bytes:
    """SHA-256 over the artefact bytes."""
    return hashlib.sha256(path.read_bytes()).digest()


def generate_keypair(private_path: Path, public_path: Path) -> None:
    """Create a new Ed25519 keypair, refusing to overwrite existing files."""
    for p in (private_path, public_path):
        if p.exists():
            raise FileExistsError(f"refusing to overwrite {p}")
    key = Ed25519PrivateKey.generate()
    private_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    private_path.chmod(0o600)
    public_path.write_bytes(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )


def sign(artefact: Path, private_key: Path, signature_path: Path | None = None) -> Path:
    """Sign ``artefact`` and write a detached ``.sig`` next to it."""
    key = serialization.load_pem_private_key(private_key.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError(f"{private_key} is not an Ed25519 private key")
    out = signature_path or artefact.with_suffix(artefact.suffix + SIGNATURE_SUFFIX)
    out.write_bytes(key.sign(digest(artefact)))
    return out


def verify(artefact: Path, public_key: Path, signature_path: Path | None = None) -> None:
    """Raise :class:`ReleaseVerificationError` unless the signature matches."""
    # Validate the key first: a misconfigured trust root should be diagnosed as
    # such, not masked by whatever else happens to be wrong with the artefact.
    try:
        pub = serialization.load_pem_public_key(public_key.read_bytes())
    except (OSError, ValueError) as e:
        raise ReleaseVerificationError(f"cannot load public key {public_key}: {e}") from e
    if not isinstance(pub, Ed25519PublicKey):
        raise ReleaseVerificationError(f"{public_key} is not an Ed25519 public key")

    sig_path = signature_path or artefact.with_suffix(artefact.suffix + SIGNATURE_SUFFIX)
    try:
        sig = sig_path.read_bytes()
    except OSError as e:
        raise ReleaseVerificationError(f"missing signature {sig_path}: {e}") from e
    try:
        pub.verify(sig, digest(artefact))
    except InvalidSignature as e:
        raise ReleaseVerificationError(f"signature does not match {artefact}") from e


def is_verified(artefact: Path, public_key: Path, signature_path: Path | None = None) -> bool:
    """Boolean wrapper for callers that do not want exceptions."""
    try:
        verify(artefact, public_key, signature_path)
        return True
    except ReleaseVerificationError:
        return False
