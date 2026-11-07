# hwmon — 轻量级硬件监控

> 轻量级硬件监控 · MIT License · 零依赖

[English](README.md) · 中文

一个自托管的小型硬件监控：服务器上跑一个 Python agent，浏览器里一个单页
仪表盘。没有数据库，没有第三方依赖，无需安装——`python3 app.py` 即可。

- **agent** — 运行在被监控主机上（Linux + systemd），每秒从 `/proc` 与 `/sys`
  读取 CPU / GPU / 内存 / 磁盘指标。
- **web** — 提供仪表盘，并转发 agent 的 `/api/metrics`，浏览器从不直接接触 agent。
- 两种模式都在同一个 [`app.py`](app.py) 里，由第一个命令行参数（`agent` / `web`）
  决定运行哪种。

```
浏览器 ──► web (:8080) ──► agent (:9101) ──► /proc, /sys
```

---

## 平台要求

| 组件 | 要求 |
|---|---|
| 操作系统 | **Linux**（agent 直接读 `/proc`、`/sys`，不支持 Windows/macOS） |
| 初始化系统 | **systemd**（agent 以 systemd 服务方式运行） |
| 显卡 | **仅 AMD**（要求 `vendor == 0x1002`，NVIDIA/Intel/Apple GPU 会被跳过）；CPU 核显温度（coretemp/zenpower）正常显示 |
| Python | **3.10+**（由 `requires-python` 约束）。Rocky 8 自带 3.6、Rocky 9 自带 3.9，因此由 `uv` 安装并固定 **3.12**，不需要改动系统 Python。CI 覆盖 3.10 / 3.11 / 3.12 / 3.13 / 3.14 |
| 运行时依赖 | 仅 `cryptography`（用于发布包与许可证签名），由 `uv.lock` 安装，无需在目标机手工构建 |
| web 宿主机 | 任何能访问 agent 的机器（Docker 可选） |

> **注意：** 如果你用 NVIDIA 显卡或 Windows，本项目不适合你，请不要为此开 issue。agent 刻意保持最小化，只面向 Linux + AMD。

## 监控指标

- **CPU**：使用率、温度（`k10temp`/`coretemp`/`zenpower`）、频率、型号、load average。
- **GPU（AMD）**：使用率、显存、温度（显存/边缘/结温）、功耗与功耗上限、PCIe 链路（Gen × 位宽，GB/s）。
- **内存**：已用/总量、Swap。
- **磁盘**：各挂载点容量（LVM/分区归并到物理盘）、NVMe 温度（hwmon）、SATA 温度（`drivetemp`）。
- 磁盘信息每 10 秒刷新（避免频繁唤醒机械盘），其余每秒一次。

## 快速开始

全流程由 [`uv`](https://docs.astral.sh/uv/) 管理：它负责安装固定版本的解释器和锁定
的依赖集合，所以你运行到的版本就是 CI 验证过的版本。

```bash
git clone <repo> && cd hwmon
uv sync                    # 解释器 + 锁定依赖 + 项目本体
```

**1. agent —— 在被监控的 Linux 宿主机上**

```bash
echo "drivetemp" | sudo tee /etc/modules-load.d/drivetemp.conf   # SATA 盘温度
sudo modprobe drivetemp

# 由 uv 管理解释器，因此 Rocky 8/9 的系统 Python 不会被使用
curl -LsSf https://astral.sh/uv/install.sh | sh
sudo mkdir -p /opt/hwmon && sudo git clone <repo> /opt/hwmon
cd /opt/hwmon && sudo uv sync --frozen --no-dev

sudo install -d -m 0755 /etc/hwmon
sudo tee /etc/hwmon/agent.env >/dev/null <<EOF
AGENT_TOKEN=$(openssl rand -hex 16)
AGENT_LABEL=$(hostname)
EOF
sudo chmod 600 /etc/hwmon/agent.env

sudo tee /etc/systemd/system/hwmon-agent.service >/dev/null <<'EOF'
[Unit]
Description=hwmon agent
After=network-online.target

[Service]
EnvironmentFile=/etc/hwmon/agent.env
ExecStart=/opt/hwmon/.venv/bin/hwmon agent
Restart=always

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now hwmon-agent
curl -H "X-Token: $(sudo grep TOKEN /etc/hwmon/agent.env | cut -d= -f2)" localhost:9101/api/metrics
```

RHEL 系主机也可以直接装 RPM：它会把 venv 部署到 `/opt/hwmon` 并安装同样的 unit，
详见 [packaging/hwmonpro.spec](packaging/hwmonpro.spec)。

**2. web —— 任何能访问 agent 的机器**

```bash
cp .env.example .env      # 填入 AGENT_URL 与共享的 AGENT_TOKEN
uv run hwmon web          # 浏览器打开 http://<web主机>:8080
```

或用 Docker（镜像内部同样用 uv）：

```bash
docker compose up -d --build
# RHEL 目标: docker build -f packaging/docker/Dockerfile.rocky9 -t hwmon-web .
```

**3. 安装前校验发布包**

```bash
uv run hwmon verify dist/hwmon-1.0.0.tar.gz -k release-public.pem
```

## 专业版模块

`src/hwmon/pro/` 下的可选模块，导入它们不影响开源版 agent：

| 模块 | 作用 |
|---|---|
| `history.py` | SQLite 指标存档，支持区间查询与分桶降采样——趋势可跨天、跨浏览器会话 |
| `alerts.py` | 阈值告警引擎，带防抖与滞回：在状态**跃迁**时触发，而不是每个采样都报 |

```python
from hwmon.pro import AlertEngine, HistoryStore

store = HistoryStore("/var/lib/hwmon/history.db")
engine = AlertEngine(sink=lambda a: print(a.level, a.message))
engine.evaluate(payload)          # 每来一个 /api/metrics 采样喂一次
store.record(payload)
```

许可证使用 Ed25519 签名并绑定机器标识：

```bash
uv run python licensing/issue.py --key license-private.pem --machine <id> --days 365 --out license.json
```

## 安全

- **仅限内网。** 页面与 agent 均为**明文 HTTP，无 TLS、无登录**，千万不要把 8080 / 9101 端口直接暴露公网。
- 唯一凭证是**共享令牌 `AGENT_TOKEN`**（agent 与 web 两端一致），请用强随机值（`openssl rand -hex 16`）。agent 端留空则不校验 token——多主机内网下不要这样做。
- 用防火墙把 agent 的 9101 端口限制为仅 web 主机可访问，如
  `ufw allow from <WEB_HOST_IP> to any port 9101`。
- 需要远程访问时，把 **web** 容器放在 VPN 或 TLS 反向代理（nginx/caddy）后面，而不是暴露 agent。

## 环境变量

| 变量 | 位置 | 说明 |
|---|---|---|
| `AGENT_TOKEN` | 两端 | 共享令牌；agent 端为空则不校验 |
| `AGENT_URL` | web | agent 地址，如 `http://<agent-ip>:9101` |
| `AGENT_LABEL` | agent | 可选，页面顶部显示的标签（默认取主机名） |
| `GPU_NAME` | agent | 覆盖显卡显示名（sysfs 通常没有型号） |
| `INCLUDE_IGPU` | agent | 设为 1 则显示核显（默认隐藏 VRAM < 2GB 的） |
| `PORT` | 两端 | 默认 agent 9101 / web 8080 |
| `HWMON_VERSION` | 两端 | 页面展示的版本号，格式 `major.minor.build`（如 `1.0.123`）。CI 自动填写：`major.minor` 取自 `VERSION` 文件（发版时手动升），build 号 = 总 commit 数（每次 push 自动 +1）。手动部署填 `$(cut -d. -f1,2 VERSION).$(git rev-list --count HEAD)`，留空显示 `dev`。agent 版本与 web 构建不一致时页面以橙色高亮（提示 agent 未同步更新） |

## 开发与测试

只用标准库，无需安装测试依赖。

```bash
uv sync                       # 同时安装开发依赖
uv run pytest -q              # 158 个用例
uv lock --check               # uv.lock 过期时失败（CI 门禁）
uv build                      # 生成 sdist + wheel 到 dist/

# 部署后冒烟测试（对运行中的实例）
uv run python tests/smoke.py http://127.0.0.1:8080 --token T
uv run python tests/smoke.py http://127.0.0.1:8080 \
  --expect-index src/hwmon/static/index.html --page-only
```

- [`src/hwmon/contract.py`](src/hwmon/contract.py) 是 agent 与前端之间的数据契约（`/api/metrics` 字段名），单元测试与冒烟测试都会校验，任何一边改名另一边都会挂。
- agent 支持 `HWMON_ROOT`（仅测试用）：把 `/proc`、`/sys` 读取重定向到假目录树，见 `tests/conftest.py`，测试因此无需真实硬件即可运行。
- 测试为 pytest 风格；`tests/test_packaging.py` 还会断言 wheel 内含仪表盘、且锁文件是最新的。

## 已知限制

- LXC/虚机内部的文件系统在宿主机上不可见（其 rootfs 通常在 LVM-thin 上，宿主机不挂载）。
- 部分 BACO 电源状态配置下 GPU 温度可能读不到，温度缺失时检查 udev 配置。
- 页面无历史存储；趋势图由浏览器端最近一小时内存采样构成。

## 目录结构

```
app.py            agent + web 服务（单文件，纯标准库）
index.html        仪表盘（单文件，中/英双语，无构建步骤）
VERSION           major.minor 基础版本（如 1.0.0）；build 号 = 总 commit 数
tests/            测试套件（无第三方依赖）
  contract.py       /api/metrics 数据契约
  test_app.py       单元测试（unittest）
  smoke.py          部署后冒烟测试
  fixtures/         测试用样例数据
docs/             部署文档、打包对比、离线安装、排障手册
.github/workflows/  GitHub CI（测试 + docker build，不做部署）
.gitea/workflows/   Gitea 部署工作流（仅内网）
SECURITY.md        安全模型：仅内网、无 TLS、无登录
README.md          英文说明
Dockerfile / docker-compose.yml   web 容器
```

## 双远端：Gitea + GitHub

同一份代码可以同时推送到内网 Gitea 和公开 GitHub，两边各跑各自的 CI，互不干扰：

| 远端 | 工作流 | 作用 |
|---|---|---|
| `gitea`（私有，内网） | [`.gitea/workflows/deploy.yaml`](.gitea/workflows/deploy.yaml) | Docker 宿主机上的自托管 runner：构建、部署、健康检查 |
| `origin`（GitHub） | [`.github/workflows/ci.yml`](.github/workflows/ci.yml) | GitHub 托管 runner：单元测试、`docker build`、启动容器并冒烟测试页面 |

关键点在于**两个平台各读自己的目录**：Gitea 读 `.gitea/workflows/`，GitHub 读
`.github/workflows/`。所以推送 GitHub 永远不会触发内网部署，推送 Gitea 也不会
触发 GitHub 构建。两个目录都要保留在仓库里。

### 一次性配置

```bash
# 原 Gitea 远端改名保留（不是删除），部署链路不受影响
git remote rename origin gitea
git remote add origin git@github.com:<you>/hardware-monitor.git
```

之后 `git push origin main` 发布到 GitHub，`git push gitea main` 触发内网部署。

想在一条命令里同时推两边，用两个 push URL：

```bash
BASE="$(git remote get-url gitea)"          # 先取到真实 URL
git remote set-url --add --push gitea "$BASE"   # 保留原来的 push 目标
git remote set-url --add --push gitea "git@github.com:<you>/hardware-monitor.git"
# 现在 git push gitea main 会依次推到内网与 GitHub
```

> 顺序很重要：必须先 `get-url` 拿到真实地址再 `set-url --add --push`，
> 否则容易把同一个地址注册两次。

### 发布前检查

- `.env` 已被 gitignore，绝不可提交（里面有 `AGENT_TOKEN`）。
- `tests/fixtures/` 的样例数据使用占位符（`host-a`、`CPU`、通用磁盘型号），
  不含真实主机标识。
- `.git/config` 里仍带着 Gitea 内网地址。若要打包分发仓库，请导出干净副本：

  ```bash
  git clone --no-local . /tmp/hwmon-clean
  cd /tmp/hwmon-clean && git remote remove origin
  ```

## 可选：Gitea Actions 自动部署

如果你自托管 Gitea，仓库自带一个现成的工作流（[`.gitea/workflows/deploy.yaml`](.gitea/workflows/deploy.yaml)）：推送 `main` 后自动重建并滚动更新 web 容器。配置方法（Secrets + runner）见
[docs/deployment-ci.md](docs/deployment-ci.md)。

## 许可证

[MIT](LICENSE)
