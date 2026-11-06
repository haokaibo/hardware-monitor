# ---------------------------------------------------------------------------
# Legacy: the single-script version from before the agent+web refactor. Archived only,
# not maintained. The current implementation lives in app.py at the repo root
# (agent / web dual mode).
# ---------------------------------------------------------------------------
#!/usr/bin/env python3
"""
Hardware temperature / power monitor for a Proxmox (PVE) host
Purpose: on the PVE host itself (not inside a container), show temperatures for
      CPU / GPU (AMD) / memory / storage (NVMe + SATA/HDD), the motherboard / chassis
      ambient temperature, CPU and case fan speeds, and live CPU / GPU / system power
Dependencies: lm-sensors (sensors), rocm-smi, nvme-cli (nvme), smartmontools (smartctl)
      Fan speed / motherboard temp: consumer boards via lm-sensors (the sensors command);
      server boards (iDRAC/iLO) may optionally install ipmitool — the script then prefers
      IPMI readings. Install what you need; unavailable parts are skipped with a notice.
      Power adds: the kernel RAPL powercap interface (CPU; usually built in, no package)
      and rocm-smi (GPU power, installed above); optionally NUT / upsc to read measured
      wall power from a CyberPower UPS — see below.
Usage: sudo python3 hw_monitor.py
       sudo python3 hw_monitor.py --watch                  # keep monitoring, refresh every 5 s
       sudo python3 hw_monitor.py --watch --interval 10     # custom refresh interval (seconds)

About whole-system power (optional, more accurate):
  CPU + GPU added together is only an estimate and excludes the motherboard, memory,
  disks, fans and other peripherals. For real wall-plug power, read an existing
  CyberPower UPS through NUT (Network UPS Tools):
    apt install nut -y
    # edit /etc/nut/ups.conf and add:
    #   [redone]
    #   driver = usbhid-ups
    #   port = auto
    systemctl restart nut-driver nut-server
  Once configured, the script detects upsc and shows the measured UPS power — no code changes.
"""

import subprocess
import re
import os
import sys
import json
import time
import shutil


def run(cmd):
    """Run a command and return stdout; on error return an empty string instead of raising."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        return r.stdout.strip()
    except Exception:
        return ""


def have(binary):
    return shutil.which(binary) is not None


def read_energy_uj(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except Exception:
        return None


def get_cpu_power_watts(sample_seconds=0.2):
    """
    Estimate instantaneous CPU power by sampling the RAPL (Running Average Power Limit)
    sysfs interface. Recent kernels on modern AMD platforms expose amd-rapl nodes through
    the powercap framework, with the same layout as Intel RAPL:
    /sys/class/powercap/*/energy_uj (microjoules, a monotonically increasing counter).
    Take two counter readings and divide the delta by the elapsed time: steadier and more
    reliable than reading an instantaneous register.
    """
    base = "/sys/class/powercap"
    if not os.path.isdir(base):
        return None

    # find package-level domains (exclude core/uncore subdomains to avoid double counting)
    candidates = []
    for name in os.listdir(base):
        energy_path = os.path.join(base, name, "energy_uj")
        name_path = os.path.join(base, name, "name")
        if os.path.exists(energy_path) and os.path.exists(name_path):
            with open(name_path) as f:
                domain_name = f.read().strip().lower()
            # only top-level package domains, shaped like "package-0"; avoids stacking "core"/"uncore"
            if "package" in domain_name and ":" not in name:
                candidates.append(energy_path)

    if not candidates:
        return None

    e1 = {p: read_energy_uj(p) for p in candidates}
    if any(v is None for v in e1.values()):
        return None
    time.sleep(sample_seconds)
    e2 = {p: read_energy_uj(p) for p in candidates}
    if any(v is None for v in e2.values()):
        return None

    total_uj = 0
    for p in candidates:
        diff = e2[p] - e1[p]
        if diff < 0:
            # the counter may wrap around: skip this sample rather than report negative power
            return None
        total_uj += diff

    return total_uj / sample_seconds / 1_000_000  # uJ/s -> W


def get_gpu_power_watts():
    """
    Extract per-GPU power from rocm-smi --json.
    Field names differ slightly across rocm-smi versions (e.g. "Average Graphics Package
    Power (W)" vs "Current Socket Graphics Package Power (W)"), so match loosely and take
    any field containing "power".
    Returns: {card_id: watts}; the value is None when it cannot be read
    """
    result = {}
    if not have("rocm-smi"):
        return result

    raw = run("rocm-smi --showpower --json")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return result

    for card, info in data.items():
        watts = None
        for k, v in info.items():
            if "power" in k.lower():
                m = re.search(r"[\d.]+", str(v))
                if m:
                    watts = float(m.group())
                    break
        result[card] = watts

    return result


def get_ups_realpower():
    """
    Optional: when a CyberPower UPS is already configured through NUT (Network UPS Tools),
    read measured wall power / load directly — more accurate than CPU + GPU, since it covers
    the whole machine (motherboard, memory, fans, disks).
    Silent no-op when NUT is missing or unconfigured; the rest of the script is unaffected.
    """
    if not have("upsc"):
        return None
    names = run("upsc -l")
    if not names:
        return None
    ups_name = names.splitlines()[0].strip()
    if not ups_name:
        return None

    out = run(f"upsc {ups_name}")
    result = {}
    for line in out.splitlines():
        if line.startswith("ups.load:"):
            result["load_percent"] = line.split(":", 1)[1].strip()
        if line.startswith("ups.realpower:") or line.startswith("output.realpower:"):
            result["watts"] = line.split(":", 1)[1].strip()
    return result or None


def get_power_summary():
    print("\n--- Power ---")

    cpu_w = get_cpu_power_watts()
    if cpu_w is not None:
        print(f"  CPU (RAPL measured): {cpu_w:.1f} W")
    else:
        print("  CPU: no RAPL data (kernel/platform may lack amd-rapl, or root is required)")

    gpu_power = get_gpu_power_watts()
    gpu_total = 0.0
    gpu_any = False
    if gpu_power:
        for card, watts in gpu_power.items():
            if watts is not None:
                print(f"  GPU [{card}]: {watts:.1f} W")
                gpu_total += watts
                gpu_any = True
            else:
                print(f"  GPU [{card}]: could not parse a power field")
    else:
        print("  GPU: rocm-smi not installed, cannot read power")

    # estimated CPU + GPU total (excludes motherboard/memory/disks/fans; indicative only)
    if cpu_w is not None or gpu_any:
        estimate = (cpu_w or 0) + gpu_total
        print(f"  -- CPU+GPU estimate: {estimate:.1f} W (excludes peripherals such as the motherboard, memory and disks; indicative only)")

    # if NUT is configured, show the measured whole-system wall power (authoritative)
    ups = get_ups_realpower()
    if ups:
        watts = ups.get("watts")
        load = ups.get("load_percent")
        if watts:
            print(f"  -- UPS measured system power: {watts} W" + (f" (load {load}%)" if load else ""))
        elif load:
            print(f"  -- UPS load: {load}% (this UPS model exposes no wattage field)")


# ---------------- CPU ----------------
def get_cpu_temp():
    """
    Prefer lm-sensors, reading k10temp (AMD Ryzen platforms).
    Tctl: control temperature (drives the fan curve; usually a bit above the real junction
    temperature and the value people normally quote)
    Tccd1: actual CCD temperature (when present)
    """
    print("--- CPU ---")
    if not have("sensors"):
        print("  lm-sensors not installed; run: apt install lm-sensors && sensors-detect")
        return

    out = run("sensors")
    # sensors separates device blocks with blank lines; inside a block every line (title,
    # Adapter, readings) starts at column 0, so indentation cannot be used to split
    blocks = out.split("\n\n")
    found = False
    for block in blocks:
        if "k10temp" in block.lower():
            found = True
            tctl = re.search(r"Tctl:\s+\+([\d.]+)", block)
            tccd = re.findall(r"Tccd\d+:\s+\+([\d.]+)", block)
            if tctl:
                print(f"  Tctl (control temp): {tctl.group(1)}°C")
            for i, t in enumerate(tccd, 1):
                print(f"  Tccd{i} (CCD{i} actual temp): {t}°C")
            if not tctl and not tccd:
                print("  " + block.strip().split("\n")[0] + ": no fields parsed, run sensors manually")

    if not found:
        print("  k10temp not found in sensors output; confirm sensors-detect was run and "
              "the k10temp module is loaded")


# ---------------- GPU (AMD) ----------------
def get_gpu_pci_addresses():
    """Find the PCI address of every VGA/Display controller via lspci, so sensors/hwmon
    readings can be matched exactly."""
    out = run("lspci -D | grep -Ei 'VGA compatible controller|Display controller|3D controller'")
    devices = []
    for line in out.splitlines():
        m = re.match(r"(\S+)\s+.*?:\s*(.*)", line)
        if m:
            devices.append((m.group(1), m.group(2).strip()))
    return devices


def get_gpu_temp():
    print("\n--- GPU (AMD) ---")

    gpu_list = get_gpu_pci_addresses()
    if gpu_list:
        print("  Display devices found (PCI address : model):")
        for addr, name in gpu_list:
            print(f"    {addr} : {name}")
    else:
        print("  WARNING: lspci found no display controller; check that pciutils is installed")

    # prefer rocm-smi --json: structured fields are harder to misread
    if have("rocm-smi"):
        raw = run("rocm-smi --showtemp --showpower --json")
        try:
            data = json.loads(raw)
            for card, info in data.items():
                print(f"\n  [{card}]")
                for k, v in info.items():
                    if "temp" in k.lower() or "power" in k.lower():
                        print(f"    {k}: {v}")
        except (json.JSONDecodeError, ValueError):
            # on JSON parse failure fall back to plain output so the user still sees something
            print("\n  raw rocm-smi output (JSON parse failed, showing raw text):")
            print(run("rocm-smi --showtemp --showpower"))
        return

    # without rocm-smi fall back to sysfs, matching strictly by PCI address so unrelated
    # devices are never read
    print("\n  rocm-smi not installed; matching hwmon nodes by PCI address through sysfs...")
    if not gpu_list:
        print("  and no PCI address could be located, so a safe read is impossible; "
              "install rocm-smi or lm-sensors")
        return

    for addr, name in gpu_list:
        short_addr = addr.split(":", 1)[-1] if ":" in addr else addr  # 0000:03:00.0 -> 03:00.0
        hwmon_path = f"/sys/bus/pci/devices/{addr}/hwmon/"
        if not os.path.isdir(hwmon_path):
            print(f"    [{addr} {name}] no hwmon node found, skipping")
            continue
        for hw in os.listdir(hwmon_path):
            base = os.path.join(hwmon_path, hw)
            temps = sorted(f for f in os.listdir(base) if re.match(r"temp\d+_input$", f))
            if not temps:
                print(f"    [{addr} {name}] no temperature files under hwmon")
                continue
            print(f"    [{addr} {name}]")
            for t in temps:
                label_file = os.path.join(base, t.replace("_input", "_label"))
                label = ""
                if os.path.exists(label_file):
                    with open(label_file) as f:
                        label = f.read().strip()
                with open(os.path.join(base, t)) as f:
                    val = int(f.read().strip()) / 1000
                print(f"      {t} ({label or 'unlabelled'}): {val:.1f}°C")


# ---------------- Memory ----------------
def get_memory_temp():
    print("\n--- Memory ---")
    if not have("sensors"):
        print("  lm-sensors not installed, skipping")
        return
    out = run("sensors")
    # DIMMs with an SPD5118 sensor show up as an spd5118 block; most consumer platforms
    # cannot read it, which is normal
    blocks = out.split("\n\n")
    found = False
    for block in blocks:
        if "spd5118" in block.lower() or "jc42" in block.lower():
            found = True
            temp = re.search(r"temp1:\s+\+([\d.]+)", block)
            title = block.strip().split("\n")[0]
            print(f"  [{title}] {temp.group(1)}°C" if temp else f"  [{title}] no temperature parsed")
    if not found:
        print("  No memory temperature sensors (most consumer DDR5 does not expose SPD "
              "temps; that is normal and does not indicate a fault)")


# ---------------- System Temp & Fans ----------------
def _parse_sensors_blocks(out):
    """Split sensors output into blank-line separated blocks, returning
    [(block title, block text)]; the title is usually the chip/driver name."""
    blocks = []
    for chunk in out.split("\n\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        lines = chunk.splitlines()
        blocks.append((lines[0].strip(), chunk))
    return blocks


def _ipmi_lines(type_name):
    """Run ipmitool sdr type <name> and parse it into [(name, reading)];
    returns None when unavailable or empty."""
    if not have("ipmitool"):
        return None
    out = run(f"ipmitool sdr type {type_name}")
    if not out:
        return None
    items = []
    for line in out.splitlines():
        # shaped like "CPU_FAN1 | 1192 RPM | ok | ..." or "System_Inlet | 25 degrees C | ok"
        m = re.match(r"^\s*(.+?)\s+\|\s*\+?([\d.?]+)\s*(?:degrees|°)?\s*(C|RPM)\b", line, re.IGNORECASE)
        if m:
            items.append((m.group(1).strip(), m.group(2)))
    return items or None


def get_system_temp():
    """Motherboard / chassis ambient temperature. Prefers IPMI (server boards), otherwise
    the System/Motherboard style readings in sensors output."""
    print("\n--- System Temp (motherboard / chassis) ---")

    ipmi_temps = _ipmi_lines("Temperature")
    if ipmi_temps:
        sys_re = re.compile(r"(system|mainboard|motherboard|ambient|inlet|pcb|board|mb)", re.I)
        hits = [(n, v) for n, v in ipmi_temps if sys_re.search(n)]
        if hits:
            for name, val in hits:
                print(f"  {name}: {val}°C  (IPMI)")
            return

    if not have("sensors"):
        print("  lm-sensors not installed; run: apt install lm-sensors && sensors-detect")
        return

    out = run("sensors")
    if not out:
        print("  could not get sensors output")
        return

    # Tier 1: the label itself carries a full keyword such as system/mainboard/ambient
    #         (e.g. "System Temperature")
    # Tier 2: named Nuvoton/ITE probe labels (e.g. SYSTIN, AUXTIN0-3, PECI); some community
    #         and in-kernel drivers use this naming
    # Tier 3: the exact naming used by the nct6687 community driver: "Thermistor N" is a
    #         physical motherboard sensor, while "AMD TSI Addr.." / "Diode N (curr)" track
    #         k10temp's CPU Tctl closely, so they are a second CPU reading rather than
    #         system/chassis temperature and are excluded to avoid misreporting
    # Tier 4 (fallback): generic numeric temp1/temp2 labels inside super-IO/EC chips
    sys_re = re.compile(r"(system|mainboard|motherboard|ambient|pcb|chassis|board)", re.I)
    named_probe_re = re.compile(r"^(systin|auxtin\d*|peci\d*|smbusmaster\d*|thermistor\d*)$", re.I)
    cpu_dup_re = re.compile(r"(tsi|diode)", re.I)  # confirmed by value comparison to duplicate the CPU temperature, not system/chassis
    superio = ("it8", "w83", "nct6", "f71888", "asus", "superio", "-ec")  # nct6 also covers nct6687/nct6775
    found = []
    for title, block in _parse_sensors_blocks(out):
        t_lower = title.lower()
        is_superio = any(c in t_lower for c in superio)
        for line in block.splitlines():
            m = re.match(r"^\s*(.+?):\s+\+?([\d.]+)\s*°?C", line)
            if not m:
                continue
            label, val = m.group(1).strip(), m.group(2)
            # strip suffixes like "(curr)" and collapse spaces to get an exact-match key
            label_key = re.sub(r"\s*\(.*?\)\s*$", "", label).strip()
            label_key = re.sub(r"\s+", "", label_key).lower()
            if sys_re.search(label):
                found.append((title, label, val, False))
            elif is_superio and cpu_dup_re.search(label):
                # duplicate reading close to CPU Tctl: skip it (not system/chassis temperature)
                continue
            elif is_superio and named_probe_re.match(label_key):
                if float(val) > 0:
                    found.append((title, label, val, False))
            elif is_superio and re.match(r"temp\d+$", label, re.I):
                # an unconnected sensor usually reads 0; filter those out to avoid noise
                if float(val) > 0:
                    found.append((title, label, val, True))

    if not found:
        print("  No system/motherboard temperature sensors detected (some boards do not "
              "expose this reading; that is normal and does not indicate a fault)")
        return
    for title, label, val, guessed in found:
        note = " (inferred motherboard temperature)" if guessed else ""
        print(f"  {label} [{title}]: {val}°C {note}")


def get_fan_speeds():
    """CPU and case fan speeds. Prefers IPMI (server boards), otherwise classifies
    sensors output by label."""
    print("\n--- Fans ---")

    ipmi_fans = _ipmi_lines("Fan")
    if ipmi_fans:
        cpu_fans = [(n, v) for n, v in ipmi_fans if re.search(r"cpu", n, re.I)]
        case_fans = [(n, v) for n, v in ipmi_fans if not re.search(r"cpu", n, re.I)]
        for tag, items in (("CPU fans: ", cpu_fans), ("Case/other fans: ", case_fans)):
            if not items:
                continue
            print(f"  {tag}")
            for name, rpm in items:
                note = "  (0 RPM: unplugged or stopped)" if rpm == "0" else ""
                print(f"    {name}: {rpm} RPM  (IPMI){note}")
        return

    if not have("sensors"):
        print("  lm-sensors not installed; run: apt install lm-sensors && sensors-detect")
        return

    out = run("sensors")
    if not out:
        print("  could not get sensors output")
        return

    cpu_fans, gpu_fans, case_fans = [], [], []
    for title, block in _parse_sensors_blocks(out):
        for line in block.splitlines():
            # matches "fan1: 1200 RPM" and labelled "CPU Fan: 1200 RPM"
            m = re.match(r"^\s*(.+?):\s+([\d.]+)\s*RPM\b", line, re.IGNORECASE)
            if not m:
                continue
            label, rpm = m.group(1).strip(), m.group(2)
            low = (label + " " + title).lower()
            if "amdgpu" in low or "gpu" in low or "radeon" in low or "geforce" in low or "nvme" in low:
                gpu_fans.append((title, label, rpm))
            elif "cpu" in low:
                cpu_fans.append((title, label, rpm))
            else:
                case_fans.append((title, label, rpm))

    if not cpu_fans and not gpu_fans and not case_fans:
        print("  No fan speed sensors detected (many consumer boards do not expose fan "
              "speeds; that is normal - run sensors manually to confirm)")
        return

    for tag, items in (("CPU fans: ", cpu_fans), ("GPU fans: ", gpu_fans), ("Case/other fans: ", case_fans)):
        if not items:
            continue
        print(f"  {tag}")
        for title, label, rpm in items:
            note = "  (0 RPM: unplugged or stopped)" if float(rpm) == 0 else ""
            print(f"    {label} [{title}]: {rpm} RPM{note}")


# ---------------- Storage ----------------
def get_storage_temp():
    print("\n--- Storage (NVMe / SATA-SSD / HDD) ---")
    disks = [d for d in run("lsblk -d -n -o NAME").splitlines() if d and not d.startswith("loop") and not d.startswith("zd")]

    if not disks:
        print("  No disk devices detected")
        return

    for disk in disks:
        path = f"/dev/{disk}"

        if disk.startswith("nvme"):
            if not have("nvme"):
                print(f"  [{disk}] nvme-cli not installed; run: apt install nvme-cli")
                continue
            out = run(f"nvme smart-log {path}")
            # the temperature field unit varies across nvme-cli versions and firmware (°F or
            # °C), but the Kelvin value in parentheses is standardized — parsing that and
            # converting is the most reliable option
            m = re.search(r"temperature\s*:\s*[\d.]+\s*°?[FC]?\s*\((\d+)\s*K\)", out, re.IGNORECASE)
            if m:
                celsius = int(m.group(1)) - 273.15
                print(f"  [{disk}] NVMe temp: {celsius:.1f}°C")
            else:
                print(f"  [{disk}] could not parse a temperature from nvme smart-log")
        else:
            if not have("smartctl"):
                print(f"  [{disk}] smartmontools not installed; run: apt install smartmontools")
                continue
            out = run(f"smartctl -A {path}")
            m = re.search(r"(?:Temperature_Celsius|Airflow_Temperature_Cel).*?\s(\d+)\s*(?:\(|$)", out)
            if m:
                print(f"  [{disk}] SATA temp: {m.group(1)}°C")
            else:
                print(f"  [{disk}] no temperature parsed (possibly a virtual disk or no SMART support; ignorable)")


def main():
    if os.geteuid() != 0:
        print("Run with root privileges: sudo python3 hw_monitor.py")
        sys.exit(1)

    watch = "--watch" in sys.argv

    # --interval N sets the refresh period in seconds; defaults to 5
    interval = 5
    if "--interval" in sys.argv:
        idx = sys.argv.index("--interval")
        if idx + 1 < len(sys.argv):
            try:
                interval = float(sys.argv[idx + 1])
            except ValueError:
                print(f"--interval must be a number; ignored, using the default {interval} s")

    while True:
        os.system("clear")
        print("=" * 55)
        print(" PVE HOST HARDWARE MONITOR")
        print(" " + time.strftime("%Y-%m-%d %H:%M:%S"))
        print("=" * 55)
        get_cpu_temp()
        get_gpu_temp()
        get_memory_temp()
        get_system_temp()
        get_storage_temp()
        get_fan_speeds()
        get_power_summary()
        print("=" * 55)

        if not watch:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()