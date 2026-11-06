"""Command line entry point.

Installed as the ``hwmon`` console script; ``python -m hwmon`` works too.

    hwmon agent                  sample this host and serve /api/metrics
    hwmon web                    serve the dashboard and proxy the agent
    hwmon verify ARTEFACT -k K   verify a release signature
    hwmon sign ARTEFACT -k K     sign a release artefact (maintainers only)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Config, package_version
from .sampler import Sampler
from .server import make_server

def _serve(config: Config) -> int:
    sampler = None
    if config.is_agent:
        sampler = Sampler(label=config.label, version=config.version)
        sampler.start()
        state_provider = lambda: sampler.state  # noqa: E731
        print(f"hwmon agent v{config.version} listening on :{config.port}")
    else:
        state_provider = lambda: {}  # noqa: E731 - the web client proxies
        print(f"hwmon web v{config.version} listening on :{config.port}")

    server = make_server(config, state_provider)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if sampler is not None:
            sampler.stop()
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    from . import release

    artefact = Path(args.artefact)
    sig = Path(args.signature) if args.signature else None
    try:
        release.verify(artefact, Path(args.key), sig)
    except release.ReleaseVerificationError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    print(f"OK: {artefact} matches {args.key}")
    return 0


def _cmd_sign(args: argparse.Namespace) -> int:
    from . import release

    sig = release.sign(Path(args.artefact), Path(args.key))
    print(f"signed {args.artefact} -> {sig}")
    return 0


def _cmd_keygen(args: argparse.Namespace) -> int:
    from . import release

    private = Path(args.private)
    public = Path(args.public) if args.public else private.with_name(private.name.replace("private", "public"))
    try:
        release.generate_keypair(private, public)
    except FileExistsError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    print(f"private key: {private} (keep offline)\npublic key:  {public} (publish this)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="hwmon", description="Lightweight hardware monitor")
    ap.add_argument("--version", action="version", version=f"hwmon {package_version()}")
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("agent", help="sample this host and serve /api/metrics")
    sub.add_parser("web", help="serve the dashboard and proxy the agent")

    v = sub.add_parser("verify", help="verify a release artefact signature")
    v.add_argument("artefact")
    v.add_argument("-k", "--key", required=True, help="trusted Ed25519 public key (PEM)")
    v.add_argument("-s", "--signature", help="detached signature (default: ARTEFACT.sig)")
    v.set_defaults(func=_cmd_verify)

    s = sub.add_parser("sign", help="sign a release artefact")
    s.add_argument("artefact")
    s.add_argument("-k", "--key", required=True, help="Ed25519 private key (PEM)")
    s.set_defaults(func=_cmd_sign)

    g = sub.add_parser("keygen", help="generate an Ed25519 signing keypair")
    g.add_argument("private", help="path for the private key")
    g.add_argument("-p", "--public", help="path for the public key")
    g.set_defaults(func=_cmd_keygen)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in ("agent", "web"):
        return _serve(Config.from_env(args.command))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
