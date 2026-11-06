# AGENT.md — working agreement for this repository

Context for anyone (human or AI agent) making changes to `hardware-monitor`.
Read this before editing. Keep it current: a stale AGENT.md is worse than none.

## What this project is

A tiny self-hosted hardware monitor with two roles in **one** file:

| Role | Runs where | Port | Responsibility |
|---|---|---|---|
| `agent` | monitored Linux host (root, systemd) | 9101 | samples `/proc`, `/sys`, `hwmon` once per second |
| `web` | any host that can reach the agent (Docker) | 8080 | serves `index.html`, proxies `/api/metrics` |

```
Browser -> web (:8080) -> agent (:9101) -> /proc, /sys
```

The browser never talks to the agent. `web` is the only thing exposed.

## Hard constraints (do not break these)

1. **Standard library only.** No `requirements.txt`, no `pip install`, no
   third-party imports anywhere in `app.py`, `index.html`, or `tests/`. The
   agent is copied as a single file onto a distro's stock Python.
2. **Python 3.10 floor.** `app.py` and `tests/` must parse and run on 3.10.
   Verified: `python3 -c "import ast;ast.parse(open('app.py').read(),feature_version=(3,10))"`.
   Do not introduce `match`, `X | Y` in annotations at runtime, `tomllib`
   (3.11), `ExceptionGroup`, or `typing` features newer than the floor.
   CI matrix: 3.10 / 3.11 / 3.12 / 3.13 / 3.14.
3. **Linux + AMD only.** The agent requires `/proc`, `/sys`, systemd, and GPUs
   with `vendor == 0x1002`. NVIDIA/Windows/macOS are explicitly out of scope —
   do not add compatibility layers for them.
4. **No deployment from GitHub.** `.github/workflows/ci.yml` builds and tests
   only. Deploys happen exclusively through `.gitea/workflows/deploy.yaml` on a
   self-hosted runner inside the intranet. Never add a deploy step to GitHub.
5. **`AGENT_TOKEN` never enters the repo.** `.env` is gitignored. Sample data
   uses placeholders, never real hostnames or hardware serials.
6. **Comments and identifiers are English.** User-facing strings may be
   bilingual (`index.html` ships an `en` and a `zh` locale); code, comments,
   commit messages, docs are English. `README.zh-CN.md` is the only Chinese doc.

## Architecture map

### `app.py` (~250 lines, the whole backend)

| Function | Contract |
|---|---|
| `rd` / `num` / `read_text` | tolerant file readers; return `None` on failure, never raise |
| `cpu()` | `/proc/stat` delta for usage, `model name` from `/proc/cpuinfo`, `k10temp`/`coretemp`/`zenpower` for temp |
| `mem()` | `/proc/meminfo` |
| `gpus()` | `drm/card*`, filtered to AMD + VRAM ≥ 2 GB unless `INCLUDE_IGPU=1` |
| `pcie()` | link gen/width → one-way GB/s |
| `parents()` | dm/LVM/partition → physical drive, depth-limited to 5 |
| `disks()` | `/sys/block` minus loop/ram/dm/md/sr, `/proc/mounts` for filesystems |
| `sample(n)` | writes `STATE`; **on any exception nothing is written and `ts` does not advance** |
| `loop()` | 1 Hz; disks only every 10th tick (`n % 10 == 0`) to avoid waking HDDs |
| `H.do_GET` | `/healthz`, `/api/info`, `/api/metrics`, `/`, `/favicon.svg` |

**`ts` is the freshness signal.** The frontend treats a non-advancing `ts` as
stalled data. Do not advance `ts` on partial or failed samples — that would hide
a broken agent behind stale values.

### Endpoints

| Path | Auth | Notes |
|---|---|---|
| `/healthz` | none | plain `ok`, used by Docker healthcheck and smoke test |
| `/api/info` | none | `{"version": "1.0.123"}` |
| `/api/metrics` | `X-Token` on the agent only | agent returns `STATE`; web proxies, 502 on agent failure |
| `/` , `/favicon.svg` | none | web mode only; missing file → 404, never a dropped connection |

### `index.html` (single file, no build step)

- `I18N` object with `en` + `zh`; default `en`; choice persisted in
  `localStorage['hw_lang']`.
- `TH` holds configurable thresholds, overridable via `window.HW_THRESHOLDS`.
- Trend charts are an in-memory ring buffer in the browser (1 h at 1 Hz).
  There is no server-side history store — do not add one without discussion.
- `localizeErr` maps the backend's Chinese error prefix
  (`无法连接 agent: ...`) to English for the EN locale. The prefix is part of
  the contract between `app.py` and `index.html`: changing one breaks the other.

### `tests/`

| File | Purpose |
|---|---|
| `tests/contract.py` | shared field contract for `/api/metrics`; used by unit tests **and** the smoke test |
| `tests/test_app.py` | unit tests; builds a fake host tree via `HWMON_ROOT` |
| `tests/smoke.py` | post-deploy checks; `--page-only` skips metrics when no agent is reachable |
| `tests/fixtures/` | sample payloads with placeholder host/hardware names |

`HWMON_ROOT` redirects all `/proc` and `/sys` reads into a fake tree. That is
how tests run without hardware — use it for every new collector test instead of
mocking individual calls.

## Workflow

```bash
# run the suite (must be 0 failures, currently 39 tests)
python3 -m unittest discover -s tests -v

# check the Python floor before committing
python3 -c "import ast;ast.parse(open('app.py').read(),feature_version=(3,10))"

# smoke a running web instance (no agent needed with --page-only)
python3 tests/smoke.py http://127.0.0.1:8080 --expect-index index.html --page-only
```

### When adding a metric

1. Add the field in `app.py`.
2. Add it to the relevant list in `tests/contract.py` — `CPU`, `MEM`, `GPU`,
   `DISK`, `FS`, `PCIE`, or `TOP`.
3. Add a fixture value in `tests/fixtures/sample_metrics.json`.
4. Read it in `index.html`.
5. Add a unit test using the fake host tree.

Skipping step 2 is the most common mistake: the contract test exists precisely
to catch a rename on one side only.

### Versioning

`HWMON_VERSION = <VERSION file major.minor>.<total commits>`, e.g. `1.0.123`.
CI writes it into `.env`; the page shows it bottom-right and highlights amber
when the agent's version differs from the web build. Bump `VERSION` manually
only for a real release.

## Deployment

| Channel | Trigger | Result |
|---|---|---|
| Gitea (intranet, private) | push to `main` | self-hosted runner on the Docker host: build → deploy to `/opt/hwmon` → health check |
| GitHub (public) | push / PR | unit tests + `docker build` + boot/page smoke test. **Never deploys** |

Both remotes coexist because each platform reads only its own workflow
directory (`.gitea/workflows/` vs `.github/workflows/`). Details in
[docs/deployment-ci.md](docs/deployment-ci.md).

Config lives in `.env` (see `.env.example`). Deploy scripts preserve the host's
existing `.env`; `AGENT_TOKEN` and `AGENT_URL` come from repository secrets.

## Reserved for the maintainer

**Do not run `git push` (or `gh repo` mutations) on this repository.** The
maintainer pushes to both remotes. Committing locally is fine; publishing is
not. If a change seems to require a push, stop and ask.

## Security posture

Intranet-only by design: no TLS, no login, no accounts. The only credential is
the shared `AGENT_TOKEN` between web and agent. See [SECURITY.md](SECURITY.md)
for the full model and the hardening checklist. Do not add auth shims or
"helpful" default-open behaviour; the deployment assumptions are documented and
deliberate.

## House style

- One file per role, no frameworks, no build step, no bundler.
- `read_text`/`num` return `None` instead of raising; collectors must degrade,
  not crash, when a sysfs node is missing.
- Prefer a small, boring implementation over a clever one. This code is meant to
  be readable in one sitting.
- Commit messages: imperative subject line, body explains *why*. No emoji, no
  attribution trailers.
