# hwmon — AI PC 远程硬件监控

## 架构

```
浏览器 ──► G3 容器 (web, :8080) ──► AI PC 宿主机 (agent, :9101) ──► /proc、/sys
```

- **agent**：跑在 AI PC 的 **PVE 宿主机**上（systemd，仅需 python3，无第三方依赖）。传感器和 amdgpu 的 sysfs 在宿主机上，容器里读不到全貌，所以不放容器。
  配置了 `LLAMA_URL` 后，agent 还会在 `:8081` 开一个 **llama-server 透明代理**并监控推理状态（见下文「LLM 推理监控」）。
- **web**：跑在 G3 的容器里，提供页面，并把 `/api/metrics` 转发给 agent（浏览器不直连 AI PC）。
- 两者是同一个 `app.py`，参数 `agent` / `web` 区分。

## 1. AI PC 宿主机：部署 agent

```bash
mkdir -p /opt/hwmon && cp app.py /opt/hwmon/
echo "drivetemp" > /etc/modules-load.d/drivetemp.conf && modprobe drivetemp   # SATA 盘 (4TB HDD) 温度
cat > /etc/hwmon-agent.env <<EOF
AGENT_TOKEN=$(openssl rand -hex 16)
AGENT_LABEL=AI PC
# 可选：启用 LLM 推理监控（见下文），<CT101_IP> 换成 llama-server 所在容器的 IP
LLAMA_URL=http://<CT101_IP>:8080
EOF
cat > /etc/systemd/system/hwmon-agent.service <<'EOF'
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
systemctl daemon-reload && systemctl enable --now hwmon-agent
curl -H "X-Token: $(grep TOKEN /etc/hwmon-agent.env | cut -d= -f2)" localhost:9101/api/metrics
```

只允许 G3 访问 9101（PVE 防火墙或 `ufw allow from <G3_IP> to any port 9101`）。
启用 LLM 代理后，8081 只放行实际要调用模型的客户端（如你的 Mac）。

## 2. G3：部署 web 容器

```bash
cd /opt/hwmon
cp .env.example .env      # 填入 AI PC 的 AGENT_URL 与共享 AGENT_TOKEN
docker compose up -d --build
# 浏览器打开 http://<G3_IP>:8080
```

## 3. Gitea Actions 自动部署（可选）

推送 `main` 分支后自动重建并更新 G3 上的 `hwmon-web` 容器，工作流见
[`.gitea/workflows/deploy.yaml`](.gitea/workflows/deploy.yaml)。

### 一次性准备

1. **在仓库配置 Secrets**（Settings → Actions → Secrets）：

   | Secret | 说明 |
   |---|---|
   | `GIT_REPO_URL` | 仓库克隆地址，如 `http://<GITEA_HOST>:3000/<owner>/<repo>.git`（不含凭据） |
   | `DEPLOY_TOKEN` | 可读该仓库的 Gitea token，供 CI 拉取代码 |
   | `AGENT_URL` | 如 `http://<AI_PC_IP>:9101` |
   | `AGENT_TOKEN` | 与 agent 一致的共享令牌 |

2. **在 Docker 宿主机（`<DOCKER_HOST_IP>`）注册一个 runner**，标签为 `hwmon-host`：

   ```bash
   # 仓库 Settings → Actions → Runners → 新建 runner，复制 registration token
   act_runner register --no-interactive \
     --instance http://<GITEA_HOST>:3000 \
     --token <REGISTRATION_TOKEN> \
     --name hwmon-runner \
     --labels hwmon-host
   ```

   并在 `config.yaml` 中把该标签映射为宿主机执行，使 CI 能直接调用 `docker`：

   ```yaml
   runner:
     labels:
       - "hwmon-host:host"
   ```

   然后以 systemd（或 `act_runner daemon`）常驻运行。

> runner 用户需拥有 `/opt/hwmon` 的写权限，并处于 `docker` 组（或等价权限）。
> 若你的 Docker 宿主机上已有其他标签的 runner，把工作流里的 `runs-on: hwmon-host`
> 改成那个标签即可。agent 端（第 1 节）仍为手动部署，因为需读取宿主机 sysfs。

## LLM 推理监控（可选）

页面上的「模型状态 / 推理速度 / 上下文占用 / 最近请求」来自 llama-server，设置 agent 的 `LLAMA_URL` 即启用；不设置则这些区块自动隐藏，其余功能不受影响。

**两步启用：**

1. llama-server 启动参数加 `--metrics`（`/slots` 默认可用）。
2. 把客户端（pi、脚本等）的接口地址从 `http://<CT101_IP>:8080` 改成 `http://<PVE宿主机_IP>:8081`。代理会原样转发（含流式），客户端无感。

**数据从哪来：**

| 页面内容 | 来源 |
|---|---|
| 最近请求表（prompt / 复用 / 输出 / 预填充 / 解码 / 耗时 / 状态） | 代理解析响应里的 `timings`、`usage`、`finish_reason`；`达到上限` = finish_reason 为 length |
| 读取中 / 生成中 / 实时 t/s / 生成进度 | 代理统计流式块（≈ token）；进度 = 已输出 / 请求的 `max_tokens` |
| 排队中 | `/metrics` 的 `requests_deferred` |
| 上下文占用 | 优先 `/metrics` 的 `kv_cache_tokens`；没有则按上一个请求的 prompt+output 估算 |
| 模型名 | `/props` |
| GPU 功耗上限 / PCIe 链路 / 磁盘读写 | amdgpu sysfs（`power1_cap`、`current_link_speed`）与 `/proc/diskstats`，不依赖 LLAMA_URL |

**注意：**

- 只记录数字，**不保存 prompt 和回复内容**。历史存在 SQLite（`LLM_DB`，默认 `/var/lib/hwmon/llm.db`，保留最近 5000 条）。
- 绕过代理直连 llama-server 的请求，不会出现在请求表里，但状态和上下文占用仍会反映。
- 代理不做鉴权，与你现在直连 llama-server 一致；用防火墙限制来源。
- 请求表只覆盖 OpenAI 兼容接口和 llama.cpp 原生 `/completion`；其它接口只记录耗时。
- 不同版本的 llama-server 的 `/metrics`、`/slots` 字段可能略有差异，取不到的项会显示 `–`。
- 与 llama.cpp 无关的 Strata 专属指标（专家缓存命中率、PCIe 实时流量）没有对应数据源，因此没有做。

## 环境变量

| 变量 | 位置 | 说明 |
|---|---|---|
| `AGENT_TOKEN` | 两端 | 共享令牌；agent 为空则不校验 |
| `AGENT_URL` | web | agent 地址，如 `http://<AI_PC_IP>:9101` |
| `AGENT_LABEL` | agent | 页面右上角显示的主机名 |
| `GPU_NAME` | agent | 覆盖显卡显示名（sysfs 通常没有型号） |
| `INCLUDE_IGPU` | agent | 设为 1 则显示 9600X 核显（默认隐藏 VRAM < 2GB 的） |
| `LLAMA_URL` | agent | llama-server 地址，设置后启用 LLM 监控与代理，如 `http://<CT101_IP>:8080` |
| `LLM_PROXY_PORT` | agent | 代理监听端口，默认 8081 |
| `LLM_DB` | agent | 请求历史 SQLite 路径，默认 `/var/lib/hwmon/llm.db` |
| `PORT` | 两端 | 默认 agent 9101 / web 8080 |

## 采集来源与已知限制

- CPU：`/proc/stat`、`k10temp` hwmon；内存：`/proc/meminfo`。
- GPU（AMD）：`/sys/class/drm/card*/device/` 的 `gpu_busy_percent`、`mem_info_vram_*`、hwmon 温度/功耗。温度读数为空时检查之前的 BACO udev 修复是否生效。
- SSD/HDD：容量来自宿主机挂载点（LVM/分区会归到物理盘）；NVMe 温度读 hwmon `Composite`，SATA 需 `drivetemp` 模块。
- **看不到 LXC 内部的文件系统**（如 LXC 102 的 rootfs 在 LVM-thin 上，宿主机不挂载它），那一块容量需另加 `lvs` 采集。
- 磁盘容量每 10 秒刷新一次，避免频繁唤醒 HDD；其余每秒采集，页面每秒轮询。
- 页面无登录，仅限内网使用，不要直接暴露公网。
