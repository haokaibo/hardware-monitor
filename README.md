# hwmon — AI PC 远程硬件监控

## 架构

```
浏览器 ──► G3 容器 (web, :8080) ──► AI PC 宿主机 (agent, :9101) ──► /proc、/sys
```

- **agent**：跑在 AI PC 的 **PVE 宿主机**上（systemd，仅需 python3，无第三方依赖）。传感器和 amdgpu 的 sysfs 在宿主机上，容器里读不到全貌，所以不放容器。
- **web**：跑在 G3 的容器里，提供页面，并把 `/api/metrics` 转发给 agent（浏览器不直连 AI PC）。
- 两者是同一个 `app.py`，参数 `agent` / `web` 区分。

## 1. AI PC 宿主机：部署 agent

```bash
mkdir -p /opt/hwmon && cp app.py /opt/hwmon/
echo "drivetemp" > /etc/modules-load.d/drivetemp.conf && modprobe drivetemp   # SATA 盘 (4TB HDD) 温度
cat > /etc/hwmon-agent.env <<EOF
AGENT_TOKEN=$(openssl rand -hex 16)
AGENT_LABEL=AI PC
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

## 2. G3：部署 web 容器

在 `docker-compose.yml` 填好 `AGENT_URL` 和 `AGENT_TOKEN`（与上面相同），然后：

```bash
docker compose up -d --build
# 浏览器打开 http://<G3_IP>:8080
```

## 环境变量

| 变量 | 位置 | 说明 |
|---|---|---|
| `AGENT_TOKEN` | 两端 | 共享令牌；agent 为空则不校验 |
| `AGENT_URL` | web | agent 地址，如 `http://<EXAMPLE_IP>:9101` |
| `AGENT_LABEL` | agent | 页面右上角显示的主机名 |
| `GPU_NAME` | agent | 覆盖显卡显示名（sysfs 通常没有型号） |
| `INCLUDE_IGPU` | agent | 设为 1 则显示 9600X 核显（默认隐藏 VRAM < 2GB 的） |
| `PORT` | 两端 | 默认 agent 9101 / web 8080 |

## 采集来源与已知限制

- CPU：`/proc/stat`、`k10temp` hwmon；内存：`/proc/meminfo`。
- GPU（AMD）：`/sys/class/drm/card*/device/` 的 `gpu_busy_percent`、`mem_info_vram_*`、hwmon 温度/功耗。温度读数为空时检查之前的 BACO udev 修复是否生效。
- SSD/HDD：容量来自宿主机挂载点（LVM/分区会归到物理盘）；NVMe 温度读 hwmon `Composite`，SATA 需 `drivetemp` 模块。
- **看不到 LXC 内部的文件系统**（如 LXC 102 的 rootfs 在 LVM-thin 上，宿主机不挂载它），那一块容量需另加 `lvs` 采集。
- 磁盘每 10 秒刷新一次，避免频繁唤醒 HDD；其余每秒采集，页面每 2 秒轮询。
- 页面无登录，仅限内网使用，不要直接暴露公网。
