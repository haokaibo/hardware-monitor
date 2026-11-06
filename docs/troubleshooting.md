# Troubleshooting

Symptoms first, causes second. Each entry states how to confirm the diagnosis
before changing anything.

## The dashboard shows "Cannot reach agent"

The web client proxies `/api/metrics` and turns any upstream failure into a 502
with this prefix. Confirm from the web host:

```bash
curl -sS -m 3 http://<agent-host>:9101/healthz           # expect: ok
curl -sS -m 3 -H "X-Token: $AGENT_TOKEN" http://<agent-host>:9101/api/metrics | head -c 200
```

| Result | Cause |
|---|---|
| Connection refused | agent not running: `systemctl status hwmon-agent`, `journalctl -u hwmon-agent -n 50` |
| `{"error":"unauthorized"}` | `AGENT_TOKEN` differs between web and agent |
| timeout | firewall: `ufw status` / `firewall-cmd --list-all` on the agent host |
| works from the host, not from the container | the container cannot resolve the hostname; use an IP or add `extra_hosts` |

## Python version errors

The project floor is **3.10**. Rocky 8 ships 3.6 and Rocky 9 ships 3.9, so a
plain `python3` there is too old:

```bash
python3 -c "import sys; print(sys.version_info[:2])"
```

Install the pinned interpreter instead of fighting the system Python:

```bash
uv python install 3.12
uv sync --frozen
uv run hwmon agent
```

For the RPM, `hwmon-setup` does this into `/opt/hwmon/venv`. If it failed during
`%post`, rerun it: the package is installed but the venv is not provisioned
until that succeeds.

## Temperatures are missing

| Metric | Source | Check |
|---|---|---|
| CPU | `k10temp` / `coretemp` / `zenpower` | `sensors` — if `Tctl` is absent, the module is not loaded |
| NVMe | hwmon `Composite` | `ls /sys/block/nvme0n1/device/hwmon*` |
| SATA | `drivetemp` | `lsmod \| grep drivetemp`; enable with `modprobe drivetemp` |
| GPU | `edge`/`junction` hwmon node | `ls /sys/class/drm/card0/device/hwmon/*/temp*_input` |

If the GPU reports no temperature after a BACO power-state transition, the
hwmon node may only appear once the card is active. Reading
`gpu_busy_percent` first usually wakes it; a udev rule that triggers a sysfs
read on device add is the durable fix.

## GPU is not listed at all

The collector requires `vendor == 0x1002` **and** a non-zero
`mem_info_vram_total`.

```bash
cat /sys/class/drm/card*/device/vendor
cat /sys/class/drm/card*/device/mem_info_vram_total
```

* NVIDIA and Intel GPUs are skipped by design.
* A card with < 2 GB VRAM is treated as an iGPU and hidden unless
  `INCLUDE_IGPU=1` is set.
* The display name comes from `product_name`, which many cards do not expose;
  set `GPU_NAME` to override it.

## Disks disappear or duplicate

* A drive mounted twice (bind mounts) is counted once — this is intentional.
* LVM and dm-crypt volumes are folded onto the physical drive via
  `/sys/class/block/*/slaves`. If a volume is not attached to a physical drive
  (e.g. an iSCSI or NFS-backed device), it will not appear.
* Filesystems inside LXC/VM guests are invisible from the host, because their
  rootfs is usually on LVM-thin and never mounted on the host.
* Disks refresh every 10 s, not every second. A newly plugged drive takes up to
  10 s to appear; this delay is deliberate to avoid waking spinning drives.

## "Data stalled" / values stopped updating

The page decides staleness from the sample timestamp. `ts` intentionally does
**not** advance when a sample fails, so a broken collector shows as stalled
rather than as plausible-looking old numbers.

Look at the payload's `error` field:

```bash
curl -s -H "X-Token: $AGENT_TOKEN" http://<agent>:9101/api/metrics | python3 -m json.tool | head -20
```

A `KeyError('MemTotal')` means `/proc/meminfo` was unreadable; a `PermissionError`
means the agent lost root. The sampler recovers on its own once the read works
again.

## Version mismatch announced in amber

The footer shows `v1.0.123 · agent v1.0.120` when the agent and the web build
differ. The number is `major.minor.<total commits>`, so a mismatch means one side
was not redeployed. Redeploy the agent or the web container; the highlight clears
on the next sample.

## Docker build fails at `uv sync --frozen`

The lock file is out of date relative to `pyproject.toml`:

```bash
uv lock --check     # exits non-zero when a refresh is needed
uv lock
```

Commit the regenerated `uv.lock`. CI runs with `--frozen` on purpose so a stale
lock fails loudly instead of silently resolving different versions.

## RPM installs but the service will not start

```bash
systemctl status hwmon-agent
journalctl -u hwmon-agent -n 50 --no-pager
ls -l /opt/hwmon/venv/bin/hwmon          # missing => %post did not provision
```

The unit sets `ProtectSystem=strict` and `ProtectHome=yes`; it can read `/proc`
and `/sys` but may only write `/var/lib/hwmon`. If a custom history database
path is configured outside that directory, the write fails there.
