# AGENT.md — working agreement for this repository

Context for anyone (human or AI agent) making changes to `hwmon`. Read this
before editing. Keep it current: a stale AGENT.md is worse than none.

## What this project is

A self-hosted hardware monitor with two roles in one package:

| Role | Runs where | Port | Responsibility |
|---|---|---|---|
| `hwmon agent` | monitored Linux host (root, systemd) | 9101 | samples `/proc`, `/sys`, `hwmon` once per second |
| `hwmon web` | any host that can reach the agent (Docker) | 8080 | serves the dashboard, proxies `/api/metrics` |

```
Browser -> web (:8080) -> agent (:9101) -> /proc, /sys
```

The browser never talks to the agent. `web` is the only thing exposed.

The `pro` tier (SQLite history + alert engine) and the licensing tool are in the
same repository but stay behind their own package boundary.

## Hard constraints (do not break these)

1. **uv manages everything.** `pyproject.toml` + `uv.lock` are the single source
   of truth for dependencies and the interpreter. Never hand-edit `uv.lock`, and
   never add a dependency without `uv add`. CI runs `uv lock --check` and
   `uv sync --frozen`, so a stale lock fails loudly.
2. **Python 3.10 floor, 3.12 pinned.** `requires-python = ">=3.10"` is the
   agent-side contract (Rocky 8/9 stock Pythons are below it, which is why uv
   installs its own interpreter there). `.python-version` pins 3.12 for
   development and containers. CI: 3.10 / 3.11 / 3.12 / 3.13 / 3.14.
   Do not use `match`, runtime `X | Y` annotations, `tomllib` (3.11),
   `ExceptionGroup`, or `typing` features newer than the floor.
3. **Linux + AMD only.** The agent needs `/proc`, `/sys`, systemd and GPUs with
   `vendor == 0x1002`. NVIDIA/Windows/macOS are out of scope — do not add
   compatibility layers.
4. **No deployment from GitHub.** `.github/workflows/ci.yml` builds and tests
   only. Deploys happen exclusively through `.gitea/workflows/deploy.yaml` on a
   self-hosted runner inside the intranet. Never add a deploy step to GitHub.
5. **Secrets never enter the repo.** `.env`, `*.pem`, `*.key`, `license.json`
   and `*.sig` are gitignored. Sample data uses placeholders, never real
   hostnames or hardware serials.
6. **Comments and identifiers are English.** User-facing strings may be
   bilingual (`src/hwmon/static/index.html` ships `en` and `zh` locales); code,
   comments, commit messages and docs are English. `README.zh-CN.md` is the only
   Chinese document.
7. **`cryptography` stays confined to `release.py` and `licensing/`.** The
   collectors and the HTTP server must never import it: the agent should be
   installable on a host where building wheels is not practical.

## Layout

```
pyproject.toml / uv.lock / .python-version   dependency and interpreter truth
src/hwmon/
  __init__.py        package version helper
  __main__.py        python -m hwmon
  cli.py             argparse entry point: agent | web | verify | sign | keygen
  config.py          Config.from_env, package_version
  sysfs.py           tolerant /proc and /sys readers, HWMON_ROOT redirection
  collectors/
    cpu.py           /proc/stat deltas, model, freq, k10temp
    mem.py           /proc/meminfo
    gpu.py           /sys/class/drm, AMD-only, PCIe link
    disk.py          /sys/block + /proc/mounts, LVM folded onto physical drives
  sampler.py         the 1 Hz loop and the ts invariant
  server.py          the HTTP handler for both roles
  contract.py        three contracts: /api/metrics, HISTORY, ALERT
  release.py         Ed25519 signing/verification of release artefacts
  pro/
    history.py       SQLite downsample store
    alerts.py        threshold engine with debounce and hysteresis
  static/            index.html, favicon.svg (packaged, served by web mode)
licensing/           issue.py (sign), verify.py (compiled into pro)
packaging/           docker/Dockerfile.rocky{8,9}, hwmonpro.spec
docs/                deployment-ci.md, troubleshooting.md
tests/               pytest suite; conftest.py builds the fake host tree
```

## Key invariants

### `ts` only advances on a successful sample

`Sampler.sample` writes `cpu`/`mem`/`gpus` first; if any raises, only `error` is
recorded and `ts` stays put. The dashboard treats a non-advancing `ts` as
stalled data. Do not "helpfully" update `ts` on partial samples — that hides a
broken agent behind plausible-looking stale numbers.

Disks are the exception: they refresh every 10th tick, and a disk failure keeps
the previous list (so drives do not vanish on a transient `/sys` hiccup) while
recording the error.

### `HWMON_ROOT` is read at call time

`sysfs.ROOT` is consulted per call, never captured at import. Use
`sysfs.expand(pattern)` for ROOT-prefixed globs and `sysfs.glob_abs(pattern)` for
patterns that already include the root. Getting this wrong is the single most
common bug when adding a collector — tests will catch it, production will not.

### The error prefix is a frontend contract

`server.ERR_AGENT_UNREACHABLE` (`无法连接 agent`) is matched by `localizeErr` in
`static/index.html` to produce the English message. Changing one without the
other breaks the EN locale; `tests/test_server.py` asserts they agree.

### The data contracts

`hwmon/contract.py` holds three lists that are each enforced by tests, because
they all fail the same way — a field renamed on one side only:

| List | Promise | Enforced by |
|---|---|---|
| `TOP` / `CPU` / `MEM` / `GPU` / `PCIE` / `DISK` / `FS` | fields `/api/metrics` carries | `check_payload`, used by the unit tests **and** the smoke script |
| `HISTORY` | SQLite columns in `pro/history.py` | the DDL is *generated* from it; `PRAGMA table_info` must match |
| `ALERT` | keys in `pro/alerts.Alert.as_dict` | `check_alert`, called before an alert leaves the engine |

`ALERT_LEVELS` is likewise the single source for the levels the engine may emit.

Do not hardcode a column list or dict literal in `pro/`; derive it from the
contract (`COLUMNS`, `{name: getattr(self, name) for name in contract.ALERT}`)
so the two cannot drift.

## Workflow

```bash
uv sync                      # install locked deps + the project
uv run pytest -q             # full suite (199 tests)
uv run hwmon agent           # run the agent from source
uv run hwmon web             # run the dashboard from source
uv build                     # sdist + wheel into dist/
uv lock --check              # verify the lock is current (CI gate)
```

Smoke test against a running instance (no agent needed with `--page-only`):

```bash
uv run python tests/smoke.py http://127.0.0.1:8080 \
  --expect-index src/hwmon/static/index.html --page-only
```

### When adding a metric

1. Add the field in the relevant `collectors/*.py`.
2. Add it to `hwmon/contract.py` (`CPU`, `MEM`, `GPU`, `DISK`, `FS`, `PCIE`, `TOP`).
   For a pro-tier field, extend `HISTORY` or `ALERT` instead — the schema and the
   output dicts are generated from those lists.
3. Add a value to `tests/fixtures/sample_metrics.json`.
4. Read it in `static/index.html`.
5. Add a test in `tests/test_collectors.py` using the `fake_host` fixture.

Step 2 is the one people skip: the contract test exists precisely to catch a
rename on one side only.

### When adding a dependency

```bash
uv add <package>          # runtime
uv add --dev <package>    # test/dev only
```

Then commit `pyproject.toml` **and** `uv.lock`. Prefer the standard library; the
agent's installability on a bare host is a feature.

### Release signing

```bash
uv run hwmon keygen keys/release-private.pem      # once, keep the private key offline
uv build
uv run hwmon sign dist/hwmon-1.0.0.tar.gz -k keys/release-private.pem
uv run hwmon verify dist/hwmon-1.0.0.tar.gz -k keys/release-public.pem
```

Verification fails closed on a missing key, wrong key type, tampered artefact or
malformed signature. `docs/troubleshooting.md` explains what each error means.

## Versioning

`HWMON_VERSION` wins when set; otherwise the installed distribution version
(from `VERSION` via hatch-vcs-style dynamic versioning) is used; a bare checkout
reports `dev`. CI exports `HWMON_VERSION=<VERSION major.minor>.<commit count>`,
e.g. `1.0.123`, and the page shows it bottom-right, highlighting amber when the
agent and web builds differ. Bump `VERSION` manually only for a real release.

## Deployment

| Channel | Trigger | Result |
|---|---|---|
| Gitea (intranet, private) | push to `main` | self-hosted runner on the Docker host: build → deploy `/opt/hwmon` → health check |
| GitHub (public) | push / PR | tests matrix, lock check, docker build + page smoke, sdist/wheel. **Never deploys** |
| RPM | manual `rpmbuild -ta` | agent-only package; `hwmon-setup` provisions the venv via uv |
| Docker | `docker build` or compose | Rocky 8/9 variants under `packaging/docker/` |

Both remotes coexist because each platform reads only its own workflow directory
(`.gitea/workflows/` vs `.github/workflows/`). Details in
[docs/deployment-ci.md](docs/deployment-ci.md).

Config lives in `.env` (see `.env.example`). Deploy scripts preserve the host's
existing `.env`; `AGENT_TOKEN` and `AGENT_URL` come from repository secrets.

## Reserved for the maintainer

**Do not run `git push` (or `gh repo` mutations) on this repository.** The
maintainer pushes to both remotes. Committing locally is fine; publishing is
not. If a change seems to require a push, stop and ask.

## Security posture

Intranet-only by design: no TLS, no login, no accounts. The only credential is
the shared `AGENT_TOKEN` between web and agent. See [SECURITY.md](SECURITY.md).
Do not add auth shims or default-open behaviour; the deployment assumptions are
documented and deliberate.

Licence and release keys are Ed25519; private keys never live in the repo or in
CI secrets that a fork could reach.

## House style

- One module per responsibility. No frameworks, no bundler, no build step for
  the frontend.
- Readers in `sysfs.py` return `None` instead of raising; collectors degrade,
  never crash, when a node is missing.
- Prefer a small, boring implementation over a clever one. The collectors should
  be readable in one sitting.
- Tests use the `fake_host` fixture rather than mocking individual reads.
- Commit messages: imperative subject, body explains *why*. No emoji, no
  attribution trailers.
