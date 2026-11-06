# hwmon — Lightweight Hardware Monitor

> Self-hosted · MIT License · Zero dependencies · Linux + AMD

A tiny self-hosted hardware monitor: one Python agent on your server, one
single-page dashboard in the browser. No database, no third-party packages,
no install — just `python3 app.py`.

- **agent** — runs on the monitored host (Linux, systemd). Reads CPU / GPU /
  memory / disk metrics from `/proc` and `/sys` once per second.
- **web** — serves the dashboard and proxies `/api/metrics` from the agent,
  so the browser never talks to the agent directly.
- Both modes live in the same [`app.py`](app.py); the first CLI argument
  (`agent` / `web`) decides which one runs.

```
Browser ──► web (:8080) ──► agent (:9101) ──► /proc, /sys
```

[English](README.md) · [中文](README.zh-CN.md)

---

## Platform requirements

| Component | Requirement |
|---|---|
| OS | **Linux** (agent reads `/proc`, `/sys` directly; Windows/macOS not supported) |
| Init | **systemd** (the agent is designed to run as a systemd service) |
| GPU | **AMD only** (`vendor == 0x1002` is required; NVIDIA / Intel / Apple GPUs are skipped). CPU iGPU temp via `coretemp`/`zenpower` hwmon is fine. |
| Python | **3.8+**, standard library only — no `pip install` needed |
| Web host | Any machine that can reach the agent over the network (Docker optional) |

> **Heads up:** if you use NVIDIA or run Windows, this project is not for
> you — please don't open issues asking for that. The agent is deliberately
> minimal and Linux+AMD specific.

## Metrics

- **CPU**: usage, per-core temp (`k10temp`/`coretemp`/`zenpower`), frequency,
  model, load average.
- **GPU (AMD)**: busy %, VRAM, temps (mem/edge/junction), power draw & cap,
  PCIe link (gen × width, GB/s).
- **RAM**: used/total, swap.
- **Disks**: capacity per mount point (LVM/partitions collapsed to the
  physical drive), NVMe temp via hwmon, SATA temp via `drivetemp`.
- Disk info refreshes every 10 s (to avoid waking HDDs); everything else at 1 Hz.

## Quick start

**1. Agent — on the monitored Linux host**

```bash
sudo mkdir -p /opt/hwmon && sudo cp app.py /opt/hwmon/
echo "drivetemp" | sudo tee /etc/modules-load.d/drivetemp.conf && sudo modprobe drivetemp   # SATA disk temps
sudo cat > /etc/hwmon-agent.env <<EOF
AGENT_TOKEN=$(openssl rand -hex 16)
AGENT_LABEL=my-host
HWMON_VERSION=$(cut -d. -f1,2 VERSION).$(git rev-list --count HEAD)   # optional: build version shown on the page (major.minor.build of the commit app.py was copied from)
EOF
sudo cat > /etc/systemd/system/hwmon-agent.service <<'EOF'
[Unit]
Description=hwmon agent
After=network-online.target
[Service]
EnvironmentFile=/etc/hwmon-agent.env
ExecStart=/usr/bin/python3 /opt/hwmon/app.py agent
Restart=always
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now hwmon-agent
curl -H "X-Token: $(grep TOKEN /etc/hwmon-agent.env | cut -d= -f2)" localhost:9101/api/metrics
```

**2. Web — on any machine that can reach the agent**

```bash
cp .env.example .env   # set AGENT_URL and the shared AGENT_TOKEN
python3 app.py web     # open http://<web-host>:8080
```

Or with Docker:

```bash
docker run -d --name hwmon-web --env-file .env -p 8080:8080 --restart unless-stopped \
  -e AGENT_URL -e AGENT_TOKEN -e HWMON_VERSION <image>
# or: docker compose up -d --build  (see docker-compose.yml)
```

## Security

- **Intranet only.** There is **no TLS** and **no login** — the agent and the
  web dashboard are plain HTTP. Never expose port 8080 or 9101 to the public
  internet.
- The only credential is **`AGENT_TOKEN`** (shared between agent and web).
  Use a strong random value (`openssl rand -hex 16`). If it is empty on the
  agent, the token check is disabled — do not do that on a multi-host LAN.
- Restrict agent port 9101 to the web host, e.g.
  `ufw allow from <WEB_HOST_IP> to any port 9101`.
- If you need remote access, put the **web** container behind a VPN or a TLS
  reverse proxy (nginx/caddy) — not the agent.

## Configuration

| Variable | Where | Meaning |
|---|---|---|
| `AGENT_TOKEN` | both | shared token; empty on the agent disables the check |
| `AGENT_URL` | web | agent address, e.g. `http://<agent-ip>:9101` |
| `AGENT_LABEL` | agent | optional label shown in the dashboard header (defaults to the hostname) |
| `GPU_NAME` | agent | override GPU display name (sysfs rarely has a model string) |
| `INCLUDE_IGPU` | agent | set `1` to also show the CPU iGPU (hidden when VRAM < 2 GB) |
| `PORT` | both | default: agent 9101 / web 8080 |
| `HWMON_VERSION` | both | build version shown on the page, format `major.minor.build` (e.g. `1.0.123`). CI sets it automatically: `major.minor` from the `VERSION` file (bump it manually for releases) + build number = total commits (increments on every push). For manual deploys set `$(cut -d. -f1,2 VERSION).$(git rev-list --count HEAD)`; empty shows `dev`. If the agent's version differs from the web build, the page highlights it in amber (agent out of date) |

## Development & testing

Standard library only — no test dependencies to install.

```bash
# unit tests (39 cases: data contract, agent sampling, HTTP server, smoke script)
python3 -m unittest discover -s tests -v

# post-deploy smoke test against a running web instance
python3 tests/smoke.py http://127.0.0.1:8080 [--token T]
```

- [`tests/contract.py`](tests/contract.py) is the shared data contract between agent and
  frontend (`/api/metrics` field names); both the unit tests and the smoke
  test enforce it, so a rename on one side fails on the other.
- The agent accepts `HWMON_ROOT` (test-only) to redirect `/proc` and `/sys`
  reads into a fake directory tree — this is how the unit tests run without
  touching real hardware.

## Known limitations

- Filesystems inside LXC/VM guests are not visible from the host (their
  rootfs usually lives on LVM-thin and is not mounted on the host).
- GPU temp may read empty on some BACO-powerstate setups — check your udev
  fix if temps are missing.
- The dashboard has no history storage; trend charts are built from the last
  hour of in-memory samples on the browser side.

## Project layout

```
app.py            agent + web server (one file, stdlib only)
index.html        dashboard (single file, EN/ZH, no build step)
VERSION           major.minor base (e.g. 1.0.0); build number = total commits
tests/            test suite (no third-party deps)
  contract.py       shared /api/metrics data contract
  test_app.py       unit tests (unittest)
  smoke.py          post-deploy smoke test
  fixtures/         sample metric payloads for tests
docs/             deployment guides (Gitea Actions)
docs/legacy/      pre-refactor single-script monitor & sample data
.github/workflows/  GitHub CI (tests + docker build; never deploys)
.gitea/workflows/   Gitea deploy workflow (intranet only)
SECURITY.md       security model: intranet-only, no TLS, no login
README.zh-CN.md   中文说明
Dockerfile / docker-compose.yml   web container
```

## Two remotes: Gitea + GitHub

The same tree can be pushed to both a private Gitea instance and a public
GitHub repository, and each side runs its own CI. They do not interfere:

| Remote | Workflow | What it does |
|---|---|---|
| `gitea` (private, intranet) | [`.gitea/workflows/deploy.yaml`](.gitea/workflows/deploy.yaml) | Self-hosted runner on the Docker host: build, deploy, health-check |
| `origin` (GitHub) | [`.github/workflows/ci.yml`](.github/workflows/ci.yml) | GitHub-hosted runner: unit tests, `docker build`, boot + page smoke test |

The key property is that **each platform only reads its own directory**:
Gitea looks at `.gitea/workflows/`, GitHub at `.github/workflows/`. A push to
GitHub can therefore never trigger a deploy to the intranet, and a Gitea push
never triggers the GitHub build. Keep both directories in the repo.

### One-time setup

```bash
# current remote (Gitea) is renamed, not removed: deployment keeps working
git remote rename origin gitea
git remote add origin git@github.com:<you>/hardware-monitor.git

# optional: push to both in one command
git remote set-url --add --push gitea "$(git remote get-url gitea)"
```

After this, `git push origin main` publishes to GitHub and
`git push gitea main` triggers the intranet deploy. To push everywhere at once:

```bash
git remote set-url --add --push gitea "$(git remote get-url gitea)"
# then `git push gitea main` pushes to both URLs registered on `gitea`
```

> Order matters: run `set-url --add --push` **after** `git remote get-url gitea`
> returns the real URL, otherwise you can register the same URL twice.

### Publishing safeguards

- `.env` is gitignored and must never be committed; it holds `AGENT_TOKEN`.
- Sample payloads under `tests/fixtures/` and `docs/legacy/` use placeholders
  (`host-a`, `CPU`, generic disk models), not real host identities.
- `.git` carries the Gitea URL in `origin`/`gitea` config. When shipping the
  repository as an archive, export a clean copy instead:

  ```bash
  git clone --no-local . /tmp/hwmon-clean
  cd /tmp/hwmon-clean && git remote remove origin
  ```

## Optional: Gitea Actions auto-deploy

If you self-host Gitea, the repo ships a ready-made workflow
([`.gitea/workflows/deploy.yaml`](.gitea/workflows/deploy.yaml)) that rebuilds
and rolls the web container on every push to `main`. See
[docs/deployment-ci.md](docs/deployment-ci.md) for setup (secrets + runner).

## License

[MIT](LICENSE)

---
