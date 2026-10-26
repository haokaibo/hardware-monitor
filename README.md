# hwmon — Lightweight Hardware Monitor

> 轻量级硬件监控 · MIT License · Zero dependencies

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

[English](#english) · [中文](#中文)

---

## English

### Platform requirements

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

### Metrics

- **CPU**: usage, per-core temp (`k10temp`/`coretemp`/`zenpower`), frequency,
  model, load average.
- **GPU (AMD)**: busy %, VRAM, temps (mem/edge/junction), power draw & cap,
  PCIe link (gen × width, GB/s).
- **RAM**: used/total, swap.
- **Disks**: capacity per mount point (LVM/partitions collapsed to the
  physical drive), NVMe temp via hwmon, SATA temp via `drivetemp`.
- Disk info refreshes every 10 s (to avoid waking HDDs); everything else at 1 Hz.

### Quick start

**1. Agent — on the monitored Linux host**

```bash
sudo mkdir -p /opt/hwmon && sudo cp app.py /opt/hwmon/
echo "drivetemp" | sudo tee /etc/modules-load.d/drivetemp.conf && sudo modprobe drivetemp   # SATA disk temps
sudo cat > /etc/hwmon-agent.env <<EOF
AGENT_TOKEN=$(openssl rand -hex 16)
AGENT_LABEL=AI PC
HWMON_VERSION=$(git rev-parse --short=7 HEAD)   # optional: build version shown on the page (short SHA of the commit app.py was copied from)
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

### Security

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

### Configuration

| Variable | Where | Meaning |
|---|---|---|
| `AGENT_TOKEN` | both | shared token; empty on the agent disables the check |
| `AGENT_URL` | web | agent address, e.g. `http://<agent-ip>:9101` |
| `AGENT_LABEL` | agent | hostname shown in the dashboard header |
| `GPU_NAME` | agent | override GPU display name (sysfs rarely has a model string) |
| `INCLUDE_IGPU` | agent | set `1` to also show the CPU iGPU (hidden when VRAM < 2 GB) |
| `PORT` | both | default: agent 9101 / web 8080 |
| `HWMON_VERSION` | both | build version shown on the page; CI sets it to the commit short SHA automatically (a new version on every push). For manual deploys set `git rev-parse --short=7 HEAD`; empty shows `dev`. If the agent's version differs from the web build, the page highlights it in amber (agent out of date) |

### Development & testing

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

### Known limitations

- Filesystems inside LXC/VM guests are not visible from the host (their
  rootfs usually lives on LVM-thin and is not mounted on the host).
- GPU temp may read empty on some BACO-powerstate setups — check your udev
  fix if temps are missing.
- The dashboard has no history storage; trend charts are built from the last
  hour of in-memory samples on the browser side.

### Project layout

```
app.py            agent + web server (one file, stdlib only)
index.html        dashboard (single file, EN/ZH, no build step)
tests/            test suite (no third-party deps)
  contract.py       shared /api/metrics data contract
  test_app.py       unit tests (unittest)
  smoke.py          post-deploy smoke test
  fixtures/         sample metric payloads for tests
docs/             deployment guides (e.g. Gitea Actions)
docs/legacy/      pre-refactor single-script monitor & sample data
Dockerfile / docker-compose.yml   web container
```

### Optional: Gitea Actions auto-deploy

If you self-host Gitea, the repo ships a ready-made workflow
([`.gitea/workflows/deploy.yaml`](.gitea/workflows/deploy.yaml)) that rebuilds
and rolls the web container on every push to `main`. See
[docs/deployment-ci.md](docs/deployment-ci.md) for setup (secrets + runner).

### License

[MIT](LICENSE)

---

## 中文

### 平台要求

| 组件 | 要求 |
|---|---|
| 操作系统 | **Linux**（agent 直接读 `/proc`、`/sys`，不支持 Windows/macOS） |
| 初始化系统 | **systemd**（agent 以 systemd 服务方式运行） |
| 显卡 | **仅 AMD**（要求 `vendor == 0x1002`，NVIDIA/Intel/Apple GPU 会被跳过）；CPU 核显温度（coretemp/zenpower）正常显示 |
| Python | **3.8+**，只用标准库，无需 `pip install` |
| web 宿主机 | 任何能访问 agent 的机器（Docker 可选） |

> **注意：** 如果你用 NVIDIA 显卡或 Windows，本项目不适合你，请不要为此开 issue。agent 刻意保持最小化，只面向 Linux + AMD。

### 监控指标

- **CPU**：使用率、温度（`k10temp`/`coretemp`/`zenpower`）、频率、型号、load average。
- **GPU（AMD）**：使用率、显存、温度（显存/边缘/结温）、功耗与功耗上限、PCIe 链路（Gen × 位宽，GB/s）。
- **内存**：已用/总量、Swap。
- **磁盘**：各挂载点容量（LVM/分区归并到物理盘）、NVMe 温度（hwmon）、SATA 温度（`drivetemp`）。
- 磁盘信息每 10 秒刷新（避免频繁唤醒机械盘），其余每秒一次。

### 快速开始

**1. agent — 在被监控的 Linux 宿主机上**

```bash
sudo mkdir -p /opt/hwmon && sudo cp app.py /opt/hwmon/
echo "drivetemp" | sudo tee /etc/modules-load.d/drivetemp.conf && sudo modprobe drivetemp   # SATA 盘温度
sudo cat > /etc/hwmon-agent.env <<EOF
AGENT_TOKEN=$(openssl rand -hex 16)
AGENT_LABEL=AI PC
HWMON_VERSION=$(git rev-parse --short=7 HEAD)   # 可选：页面展示的版本号（app.py 来源 commit 的短 SHA）
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

**2. web — 任何能访问 agent 的机器**

```bash
cp .env.example .env   # 填入 AGENT_URL 与共享的 AGENT_TOKEN
python3 app.py web     # 浏览器打开 http://<web主机>:8080
```

或用 Docker：

```bash
docker run -d --name hwmon-web --env-file .env -p 8080:8080 --restart unless-stopped \
  -e AGENT_URL -e AGENT_TOKEN -e HWMON_VERSION <image>
# 或: docker compose up -d --build（见 docker-compose.yml）
```

### 安全

- **仅限内网。** 页面与 agent 均为**明文 HTTP，无 TLS、无登录**，千万不要把 8080 / 9101 端口直接暴露公网。
- 唯一凭证是**共享令牌 `AGENT_TOKEN`**（agent 与 web 两端一致），请用强随机值（`openssl rand -hex 16`）。agent 端留空则不校验 token——多主机内网下不要这样做。
- 用防火墙把 agent 的 9101 端口限制为仅 web 主机可访问，如
  `ufw allow from <WEB_HOST_IP> to any port 9101`。
- 需要远程访问时，把 **web** 容器放在 VPN 或 TLS 反向代理（nginx/caddy）后面，而不是暴露 agent。

### 环境变量

| 变量 | 位置 | 说明 |
|---|---|---|
| `AGENT_TOKEN` | 两端 | 共享令牌；agent 端为空则不校验 |
| `AGENT_URL` | web | agent 地址，如 `http://<agent-ip>:9101` |
| `AGENT_LABEL` | agent | 页面顶部显示的主机名 |
| `GPU_NAME` | agent | 覆盖显卡显示名（sysfs 通常没有型号） |
| `INCLUDE_IGPU` | agent | 设为 1 则显示核显（默认隐藏 VRAM < 2GB 的） |
| `PORT` | 两端 | 默认 agent 9101 / web 8080 |
| `HWMON_VERSION` | 两端 | 页面展示的版本号；CI 自动填 commit 短 SHA（每次 push 一个新版本）。手动部署填 `git rev-parse --short=7 HEAD`，留空显示 `dev`。agent 版本与 web 构建不一致时页面以橙色高亮（提示 agent 未同步更新） |

### 开发与测试

只用标准库，无需安装测试依赖。

```bash
# 单元测试（39 个用例：数据契约、agent 采样、HTTP 服务、冒烟脚本）
python3 -m unittest discover -s tests -v

# 部署后冒烟测试（对运行中的 web 实例）
python3 tests/smoke.py http://127.0.0.1:8080 [--token T]
```

- [`tests/contract.py`](tests/contract.py) 是 agent 与前端之间的数据契约（`/api/metrics` 字段名），单元测试与冒烟测试都会校验，任何一边改名另一边都会挂。
- agent 支持 `HWMON_ROOT`（仅测试用）：把 `/proc`、`/sys` 读取重定向到假目录树，单元测试因此无需真实硬件即可运行。

### 已知限制

- LXC/虚机内部的文件系统在宿主机上不可见（其 rootfs 通常在 LVM-thin 上，宿主机不挂载）。
- 部分 BACO 电源状态配置下 GPU 温度可能读不到，温度缺失时检查 udev 配置。
- 页面无历史存储；趋势图由浏览器端最近一小时内存采样构成。

### 目录结构

```
app.py            agent + web 服务（单文件，纯标准库）
index.html        仪表盘（单文件，中/英双语，无构建步骤）
tests/            测试套件（无第三方依赖）
  contract.py       /api/metrics 数据契约
  test_app.py       单元测试（unittest）
  smoke.py          部署后冒烟测试
  fixtures/         测试用样例数据
docs/             部署文档（如 Gitea Actions 自动部署）
docs/legacy/      重构前的单脚本监控与样例数据
Dockerfile / docker-compose.yml   web 容器
```

### 可选：Gitea Actions 自动部署

如果你自托管 Gitea，仓库自带一个现成的工作流（[`.gitea/workflows/deploy.yaml`](.gitea/workflows/deploy.yaml)）：推送 `main` 后自动重建并滚动更新 web 容器。配置方法（Secrets + runner）见
[docs/deployment-ci.md](docs/deployment-ci.md)。

### 许可证

[MIT](LICENSE)
