# ---------------------------------------------------------------------------
# 历史遗留：重构为 agent+web 架构前的单脚本版本，仅存档，不再维护。
# 当前实现见仓库根目录 app.py（agent / web 双模式）。
# ---------------------------------------------------------------------------
#!/usr/bin/env python3
"""
PVE Host 硬件温度/功耗监控脚本
用途：在 PVE host（不是容器内）查看 CPU / GPU(AMD) / 内存 / 存储(NVMe+SATA/HDD) 的温度，
      主板/机箱环境温度、CPU 风扇与机箱风扇转速，以及 CPU / GPU / 整机的实时功耗
依赖：lm-sensors (sensors), rocm-smi, nvme-cli (nvme), smartmontools (smartctl)
      风扇转速/主板温度：消费级主板走 lm-sensors（sensors 命令）；
      服务器主板（iDRAC/iLO）可选安装 ipmitool，脚本会自动优先使用 IPMI 读数
      缺哪个装哪个，脚本会自动跳过不可用的部分并给出提示
      功耗部分额外依赖：内核 RAPL powercap 接口（CPU，通常内核自带无需装包）、
      rocm-smi（GPU 功耗，前面步骤应该已装）、
      可选 NUT / upsc（读取 CyberPower UPS 实测整机墙插功耗，见下方说明）
用法：sudo python3 hw_monitor.py
      sudo python3 hw_monitor.py --watch                  # 持续监控，默认每5秒刷新一次
      sudo python3 hw_monitor.py --watch --interval 10     # 自定义刷新间隔（秒）

关于整机功耗（可选，更精确）：
  CPU+GPU 相加只是估算值，不包含主板/内存/硬盘/风扇等外围功耗。
  如果想看整机真实墙插功耗，可以用 NUT (Network UPS Tools) 读取已有的 CyberPower UPS：
    apt install nut -y
    # 编辑 /etc/nut/ups.conf，添加：
    #   [redone]
    #   driver = usbhid-ups
    #   port = auto
    systemctl restart nut-driver nut-server
  配置好之后脚本会自动检测到 upsc 命令并展示 UPS 实测功耗，无需改代码。
"""

import subprocess
import re
import os
import sys
import json
import time
import shutil


def run(cmd):
    """执行命令，返回 stdout；出错返回空字符串，不抛异常"""
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
    通过 RAPL (Running Average Power Limit) sysfs 接口采样计算 CPU 瞬时功耗。
    9600X 属于 Zen5 平台，内核较新时会通过 powercap 框架暴露 amd-rapl 节点，
    路径结构和 Intel RAPL 一致：/sys/class/powercap/*/energy_uj（微焦耳，单调递增计数器）。
    做法：采样两次能量计数值，除以时间差得到瞬时功率，比读取瞬时寄存器更稳定可靠。
    """
    base = "/sys/class/powercap"
    if not os.path.isdir(base):
        return None

    # 找出 package 级别的域（排除 core/uncore 等子域，避免重复计算）
    candidates = []
    for name in os.listdir(base):
        energy_path = os.path.join(base, name, "energy_uj")
        name_path = os.path.join(base, name, "name")
        if os.path.exists(energy_path) and os.path.exists(name_path):
            with open(name_path) as f:
                domain_name = f.read().strip().lower()
            # 只取顶层 package 域，形如 "package-0"，avoid 子域 "core"/"uncore" 重复叠加
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
            # 计数器可能溢出回绕，直接跳过这次采样，避免算出负功耗
            return None
        total_uj += diff

    return total_uj / sample_seconds / 1_000_000  # uJ/s -> W


def get_gpu_power_watts():
    """
    通过 rocm-smi --json 提取各 GPU 的功耗字段。
    不同 rocm-smi 版本字段命名略有差异（如 "Average Graphics Package Power (W)"、
    "Current Socket Graphics Package Power (W)"），这里做模糊匹配，取包含 power 的字段。
    返回: {card_id: watts} 的字典，取不到时对应值为 None
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
    可选：如果已经通过 NUT (Network UPS Tools) 配置了 CyberPower UPS，
    直接读取墙插实测功耗/负载百分比，比 CPU+GPU 相加估算更准确
    （包含了主板、内存、风扇、硬盘等全部整机功耗）。
    未安装/未配置 NUT 时静默跳过，不影响脚本其余功能。
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
    print("\n--- Power (功耗) ---")

    cpu_w = get_cpu_power_watts()
    if cpu_w is not None:
        print(f"  CPU (RAPL 实测): {cpu_w:.1f} W")
    else:
        print("  CPU: 未能读取 RAPL 数据（内核/平台可能不支持 amd-rapl，或需要 root 权限）")

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
                print(f"  GPU [{card}]: 未能解析功耗字段")
    else:
        print("  GPU: 未安装 rocm-smi，无法读取功耗")

    # CPU + GPU 估算总功耗（不含主板/内存/硬盘/风扇等外围功耗，仅供参考）
    if cpu_w is not None or gpu_any:
        estimate = (cpu_w or 0) + gpu_total
        print(f"  ── CPU+GPU 估算合计: {estimate:.1f} W （不含主板/内存/硬盘等外围功耗，仅供参考）")

    # 如果配置了 NUT，展示 UPS 实测的整机墙插功耗（更权威）
    ups = get_ups_realpower()
    if ups:
        watts = ups.get("watts")
        load = ups.get("load_percent")
        if watts:
            print(f"  ── UPS 实测整机功耗: {watts} W" + (f"（负载 {load}%）" if load else ""))
        elif load:
            print(f"  ── UPS 负载率: {load}%（该 UPS 型号未暴露具体瓦数字段）")


# ---------------- CPU ----------------
def get_cpu_temp():
    """
    优先用 lm-sensors 读取 k10temp（AMD Ryzen 平台）。
    Tctl: 控制温度（用于风扇曲线，通常比实际结温略高，是最常用的参考值）
    Tccd1: CCD 实际温度（如果有）
    """
    print("--- CPU ---")
    if not have("sensors"):
        print("  未安装 lm-sensors，运行: apt install lm-sensors && sensors-detect")
        return

    out = run("sensors")
    # sensors 输出里每个设备块之间用空行分隔，块内部（标题/Adapter/数值行）全部顶格，不能靠缩进切分
    blocks = out.split("\n\n")
    found = False
    for block in blocks:
        if "k10temp" in block.lower():
            found = True
            tctl = re.search(r"Tctl:\s+\+([\d.]+)", block)
            tccd = re.findall(r"Tccd\d+:\s+\+([\d.]+)", block)
            if tctl:
                print(f"  Tctl (控制温度): {tctl.group(1)}°C")
            for i, t in enumerate(tccd, 1):
                print(f"  Tccd{i} (CCD{i} 实际温度): {t}°C")
            if not tctl and not tccd:
                print("  " + block.strip().split("\n")[0] + "：未解析到具体字段，请手动运行 sensors 查看")

    if not found:
        print("  未在 sensors 输出中找到 k10temp，请确认已运行 sensors-detect 并加载了 k10temp 模块")


# ---------------- GPU (AMD) ----------------
def get_gpu_pci_addresses():
    """通过 lspci 找到所有 VGA/Display 控制器的 PCI 地址，用于和 sensors/hwmon 精确匹配"""
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
        print("  检测到的显示设备 (PCI地址 : 型号)：")
        for addr, name in gpu_list:
            print(f"    {addr} : {name}")
    else:
        print("  警告：lspci 未找到任何显示控制器，请检查是否安装了 pciutils")

    # 优先用 rocm-smi --json，字段结构化，不容易读错
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
            # json 解析失败就退回普通输出，至少让用户看到原始信息
            print("\n  rocm-smi 原始输出（JSON 解析失败，展示原始文本）：")
            print(run("rocm-smi --showtemp --showpower"))
        return

    # 没有 rocm-smi 时，退回 sysfs，但严格按 PCI 地址匹配，避免读到无关设备
    print("\n  未安装 rocm-smi，尝试通过 sysfs 按 PCI 地址精确匹配 hwmon...")
    if not gpu_list:
        print("  且未能定位 PCI 地址，无法安全读取，建议安装 rocm-smi 或 lm-sensors")
        return

    for addr, name in gpu_list:
        short_addr = addr.split(":", 1)[-1] if ":" in addr else addr  # 形如 0000:03:00.0 -> 03:00.0
        hwmon_path = f"/sys/bus/pci/devices/{addr}/hwmon/"
        if not os.path.isdir(hwmon_path):
            print(f"    [{addr} {name}] 未找到 hwmon 节点，跳过")
            continue
        for hw in os.listdir(hwmon_path):
            base = os.path.join(hwmon_path, hw)
            temps = sorted(f for f in os.listdir(base) if re.match(r"temp\d+_input$", f))
            if not temps:
                print(f"    [{addr} {name}] hwmon 下无温度文件")
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
                print(f"      {t} ({label or '未标注'}): {val:.1f}°C")


# ---------------- Memory ----------------
def get_memory_temp():
    print("\n--- Memory ---")
    if not have("sensors"):
        print("  未安装 lm-sensors，跳过")
        return
    out = run("sensors")
    # DDR5 内存条自带 SPD5118 温度传感器时会出现 spd5118 block；多数消费级平台读不到，属正常
    blocks = out.split("\n\n")
    found = False
    for block in blocks:
        if "spd5118" in block.lower() or "jc42" in block.lower():
            found = True
            temp = re.search(r"temp1:\s+\+([\d.]+)", block)
            title = block.strip().split("\n")[0]
            print(f"  [{title}] {temp.group(1)}°C" if temp else f"  [{title}] 未解析到温度")
    if not found:
        print("  未检测到内存温度传感器（多数消费级 DDR5 不暴露 SPD 温度，属正常现象，不代表内存有问题）")


# ---------------- System Temp & Fans ----------------
def _parse_sensors_blocks(out):
    """把 sensors 输出按空行切块，返回 [(块标题, 块文本)]，标题通常是传感器芯片/驱动名"""
    blocks = []
    for chunk in out.split("\n\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        lines = chunk.splitlines()
        blocks.append((lines[0].strip(), chunk))
    return blocks


def _ipmi_lines(type_name):
    """执行 ipmitool sdr type <name>，解析成 [(名称, 读数)]；不可用/无数据时返回 None"""
    if not have("ipmitool"):
        return None
    out = run(f"ipmitool sdr type {type_name}")
    if not out:
        return None
    items = []
    for line in out.splitlines():
        # 形如 "CPU_FAN1 | 1192 RPM | ok | ..." 或 "System_Inlet | 25 degrees C | ok"
        m = re.match(r"^\s*(.+?)\s+\|\s*\+?([\d.?]+)\s*(?:degrees|°)?\s*(C|RPM)\b", line, re.IGNORECASE)
        if m:
            items.append((m.group(1).strip(), m.group(2)))
    return items or None


def get_system_temp():
    """主板/机箱环境温度。优先 IPMI（服务器板），否则用 sensors 里的 System/Motherboard 类读数"""
    print("\n--- System Temp (主板/机箱温度) ---")

    ipmi_temps = _ipmi_lines("Temperature")
    if ipmi_temps:
        sys_re = re.compile(r"(system|mainboard|motherboard|ambient|inlet|pcb|board|mb)", re.I)
        hits = [(n, v) for n, v in ipmi_temps if sys_re.search(n)]
        if hits:
            for name, val in hits:
                print(f"  {name}: {val}°C  (IPMI)")
            return

    if not have("sensors"):
        print("  未安装 lm-sensors，运行: apt install lm-sensors && sensors-detect")
        return

    out = run("sensors")
    if not out:
        print("  未能获取 sensors 输出")
        return

    # 一级匹配：label 本身带 system/mainboard/ambient 等完整关键字（如 "System Temperature"）
    # 二级匹配：Nuvoton/ITE 具名探针标签（如 SYSTIN、AUXTIN0-3、PECI）——部分社区/内核驱动会用这套命名
    # 三级匹配：本机 nct6687 社区增强驱动的实际命名——"Thermistor N" 是主板物理测温点（非CPU相关），
    #           而 "AMD TSI Addr.." / "Diode N (curr)" 经比对数值和 k10temp 的 CPU Tctl 高度接近，
    #           判断是 CPU 温度的另一路读数（不是系统/机箱温度），排除掉避免误判
    # 四级匹配（后备）：super-IO/EC 芯片块里的通用 temp1/temp2 纯数字标签
    sys_re = re.compile(r"(system|mainboard|motherboard|ambient|pcb|chassis|board)", re.I)
    named_probe_re = re.compile(r"^(systin|auxtin\d*|peci\d*|smbusmaster\d*|thermistor\d*)$", re.I)
    cpu_dup_re = re.compile(r"(tsi|diode)", re.I)  # 经数值比对确认是 CPU 温度的重复读数，非系统/机箱温度
    superio = ("it8", "w83", "nct6", "f71888", "asus", "superio", "-ec")  # nct6 同时覆盖 nct6687/nct6775
    found = []
    for title, block in _parse_sensors_blocks(out):
        t_lower = title.lower()
        is_superio = any(c in t_lower for c in superio)
        for line in block.splitlines():
            m = re.match(r"^\s*(.+?):\s+\+?([\d.]+)\s*°?C", line)
            if not m:
                continue
            label, val = m.group(1).strip(), m.group(2)
            # 去掉 "(curr)" 这类后缀、压缩空格，得到用于精确匹配的 key
            label_key = re.sub(r"\s*\(.*?\)\s*$", "", label).strip()
            label_key = re.sub(r"\s+", "", label_key).lower()
            if sys_re.search(label):
                found.append((title, label, val, False))
            elif is_superio and cpu_dup_re.search(label):
                # 数值上和 CPU Tctl 高度接近的重复读数，跳过（不计入系统/机箱温度）
                continue
            elif is_superio and named_probe_re.match(label_key):
                if float(val) > 0:
                    found.append((title, label, val, False))
            elif is_superio and re.match(r"temp\d+$", label, re.I):
                # 未接线的传感器通常读数为 0，过滤掉避免刷屏
                if float(val) > 0:
                    found.append((title, label, val, True))

    if not found:
        print("  未检测到系统/主板温度传感器（部分主板不向系统暴露该读数，属正常现象，不代表主板有问题）")
        return
    for title, label, val, guessed in found:
        note = "（推测为主板温度）" if guessed else ""
        print(f"  {label} [{title}]: {val}°C {note}")


def get_fan_speeds():
    """CPU 风扇和机箱风扇转速。优先 IPMI（服务器板），否则用 sensors 输出按名称归类"""
    print("\n--- Fans (风扇转速) ---")

    ipmi_fans = _ipmi_lines("Fan")
    if ipmi_fans:
        cpu_fans = [(n, v) for n, v in ipmi_fans if re.search(r"cpu", n, re.I)]
        case_fans = [(n, v) for n, v in ipmi_fans if not re.search(r"cpu", n, re.I)]
        for tag, items in (("CPU 风扇：", cpu_fans), ("机箱/其他风扇：", case_fans)):
            if not items:
                continue
            print(f"  {tag}")
            for name, rpm in items:
                note = "  (0 RPM 可能未接线或停转)" if rpm == "0" else ""
                print(f"    {name}: {rpm} RPM  (IPMI){note}")
        return

    if not have("sensors"):
        print("  未安装 lm-sensors，运行: apt install lm-sensors && sensors-detect")
        return

    out = run("sensors")
    if not out:
        print("  未能获取 sensors 输出")
        return

    cpu_fans, gpu_fans, case_fans = [], [], []
    for title, block in _parse_sensors_blocks(out):
        for line in block.splitlines():
            # 匹配 "fan1: 1200 RPM" 或带 label 的 "CPU Fan: 1200 RPM"
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
        print("  未检测到风扇转速传感器（很多消费级主板不向系统暴露风扇转速，属正常现象；可手动运行 sensors 确认）")
        return

    for tag, items in (("CPU 风扇：", cpu_fans), ("GPU 风扇：", gpu_fans), ("机箱/其他风扇：", case_fans)):
        if not items:
            continue
        print(f"  {tag}")
        for title, label, rpm in items:
            note = "  (0 RPM 可能未接线或停转)" if float(rpm) == 0 else ""
            print(f"    {label} [{title}]: {rpm} RPM{note}")


# ---------------- Storage ----------------
def get_storage_temp():
    print("\n--- Storage (NVMe / SATA-SSD / HDD) ---")
    disks = [d for d in run("lsblk -d -n -o NAME").splitlines() if d and not d.startswith("loop") and not d.startswith("zd")]

    if not disks:
        print("  未检测到磁盘设备")
        return

    for disk in disks:
        path = f"/dev/{disk}"

        if disk.startswith("nvme"):
            if not have("nvme"):
                print(f"  [{disk}] 未安装 nvme-cli，运行: apt install nvme-cli")
                continue
            out = run(f"nvme smart-log {path}")
            # 不同 nvme-cli/固件的 temperature 字段单位不统一（有的是 °F，有的是 °C），
            # 但括号里的开尔文（K）值是固定标准单位，抓它来换算最可靠
            m = re.search(r"temperature\s*:\s*[\d.]+\s*°?[FC]?\s*\((\d+)\s*K\)", out, re.IGNORECASE)
            if m:
                celsius = int(m.group(1)) - 273.15
                print(f"  [{disk}] NVMe 温度: {celsius:.1f}°C")
            else:
                print(f"  [{disk}] 未能从 nvme smart-log 解析出温度")
        else:
            if not have("smartctl"):
                print(f"  [{disk}] 未安装 smartmontools，运行: apt install smartmontools")
                continue
            out = run(f"smartctl -A {path}")
            m = re.search(r"(?:Temperature_Celsius|Airflow_Temperature_Cel).*?\s(\d+)\s*(?:\(|$)", out)
            if m:
                print(f"  [{disk}] SATA 温度: {m.group(1)}°C")
            else:
                print(f"  [{disk}] 未能解析出温度（可能是虚拟磁盘/不支持 SMART，可忽略）")


def main():
    if os.geteuid() != 0:
        print("请使用 root 权限运行: sudo python3 hw_monitor.py")
        sys.exit(1)

    watch = "--watch" in sys.argv

    # 支持 --interval N 自定义刷新秒数，不传则默认 5 秒
    interval = 5
    if "--interval" in sys.argv:
        idx = sys.argv.index("--interval")
        if idx + 1 < len(sys.argv):
            try:
                interval = float(sys.argv[idx + 1])
            except ValueError:
                print(f"--interval 参数必须是数字，已忽略，使用默认值 {interval} 秒")

    while True:
        os.system("clear")
        print("=" * 55)
        print(" PVE HOST 硬件温度监控")
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