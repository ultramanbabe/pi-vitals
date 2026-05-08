import glob
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.request

import psutil


def _read(path, default=None, cast=str):
    try:
        with open(path) as f:
            return cast(f.read().strip())
    except (OSError, ValueError):
        return default


def _find_amd_gpu():
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]*")):
        uevent = os.path.join(card, "device", "uevent")
        if os.path.exists(uevent) and "DRIVER=amdgpu" in open(uevent).read():
            return os.path.join(card, "device")
    return None


def _find_hwmon(name):
    for h in glob.glob("/sys/class/hwmon/hwmon*"):
        if _read(os.path.join(h, "name")) == name:
            return h
    return None


GPU_PATH = _find_amd_gpu()
GPU_HWMON = None
if GPU_PATH:
    cands = glob.glob(os.path.join(GPU_PATH, "hwmon/hwmon*"))
    GPU_HWMON = cands[0] if cands else None

CPU_HWMON = _find_hwmon("k10temp")
NVME_HWMONS = [h for h in glob.glob("/sys/class/hwmon/hwmon*") if _read(os.path.join(h, "name")) == "nvme"]
# Motherboard super-I/O sensor chip — exposes CPU fan RPM (fan1 on this board, see nct6687 driver)
MOBO_HWMON = _find_hwmon("nct6687") or _find_hwmon("nct6798") or _find_hwmon("nct6791") or _find_hwmon("nct6775")
CPU_FAN_INPUT = os.environ.get("CPU_FAN_INPUT", "fan1_input")

# CPU power via RAPL (powercap). Find the package-0 domain.
def _find_rapl_package():
    for d in sorted(glob.glob("/sys/class/powercap/intel-rapl:[0-9]*")):
        if _read(os.path.join(d, "name")) == "package-0":
            return os.path.join(d, "energy_uj")
    return None

CPU_RAPL_ENERGY = _find_rapl_package()
_prev_energy = None       # microjoules
_prev_energy_ts = None


def _detect_cpu_model():
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        return None
    return None


def _detect_gpu_model():
    override = os.environ.get("GPU_MODEL")
    if override:
        return override
    if not GPU_PATH:
        return None
    try:
        out = subprocess.check_output(["lspci", "-mm", "-d", "::0300"], text=True, timeout=2)
        for line in out.splitlines():
            parts = re.findall(r'"([^"]+)"', line)
            if len(parts) >= 3:
                name = parts[2]
                m = re.search(r"\[([^\]]+)\]", name)
                return m.group(1) if m else name
    except (OSError, subprocess.SubprocessError):
        pass
    return None


CPU_MODEL = _detect_cpu_model()
GPU_MODEL = _detect_gpu_model()
RAM_MODEL = os.environ.get("RAM_MODEL")  # static label, set in run.sh

_BOOT_TIME = psutil.boot_time()
_prev_net = None
_prev_net_ts = None
_prev_disk = None
_prev_disk_ts = None


def _net_speed():
    global _prev_net, _prev_net_ts
    c = psutil.net_io_counters()
    now = time.time()
    if _prev_net is None:
        _prev_net, _prev_net_ts = c, now
        return 0.0, 0.0
    dt = max(now - _prev_net_ts, 1e-3)
    up = (c.bytes_sent - _prev_net.bytes_sent) / dt
    down = (c.bytes_recv - _prev_net.bytes_recv) / dt
    _prev_net, _prev_net_ts = c, now
    return max(up, 0.0), max(down, 0.0)


def _disk_speed():
    global _prev_disk, _prev_disk_ts
    c = psutil.disk_io_counters()
    now = time.time()
    if _prev_disk is None:
        _prev_disk, _prev_disk_ts = c, now
        return 0.0, 0.0
    dt = max(now - _prev_disk_ts, 1e-3)
    r = (c.read_bytes - _prev_disk.read_bytes) / dt
    w = (c.write_bytes - _prev_disk.write_bytes) / dt
    _prev_disk, _prev_disk_ts = c, now
    return max(r, 0.0), max(w, 0.0)


def _cpu():
    freqs = psutil.cpu_freq(percpu=False)
    per_core = psutil.cpu_percent(percpu=True)
    temp = None
    if CPU_HWMON:
        # Tctl is what most AMD users care about
        for i in range(1, 10):
            label = _read(os.path.join(CPU_HWMON, f"temp{i}_label"))
            if label == "Tctl":
                temp = _read(os.path.join(CPU_HWMON, f"temp{i}_input"), cast=int)
                if temp is not None:
                    temp /= 1000
                break
    fan_rpm = None
    if MOBO_HWMON:
        v = _read(os.path.join(MOBO_HWMON, CPU_FAN_INPUT), cast=int)
        if v is not None:
            fan_rpm = v
    # Watts = ΔµJ / Δs / 1e6
    global _prev_energy, _prev_energy_ts
    power_w = None
    if CPU_RAPL_ENERGY:
        e = _read(CPU_RAPL_ENERGY, cast=int)
        now = time.time()
        if e is not None:
            if _prev_energy is not None and _prev_energy_ts is not None:
                de = e - _prev_energy
                # handle counter wrap (RAPL counters wrap at max_energy_range_uj, ~262 J on AMD)
                if de < 0:
                    de += 1 << 32
                dt = max(now - _prev_energy_ts, 1e-3)
                power_w = round(de / dt / 1_000_000, 1)
            _prev_energy = e
            _prev_energy_ts = now
    return {
        "model": CPU_MODEL,
        "usage": round(sum(per_core) / len(per_core), 1) if per_core else 0,
        "per_core": [round(p, 1) for p in per_core],
        "freq_mhz": round(freqs.current) if freqs else None,
        "temp_c": round(temp, 1) if temp is not None else None,
        "fan_rpm": fan_rpm,
        "power_w": power_w,
    }


def _gpu():
    if not GPU_PATH:
        return None
    out = {
        "model": GPU_MODEL,
        "usage": _read(os.path.join(GPU_PATH, "gpu_busy_percent"), cast=int),
        "vram_used": _read(os.path.join(GPU_PATH, "mem_info_vram_used"), cast=int),
        "vram_total": _read(os.path.join(GPU_PATH, "mem_info_vram_total"), cast=int),
    }
    if GPU_HWMON:
        for i in (1, 2, 3):
            label = _read(os.path.join(GPU_HWMON, f"temp{i}_label"))
            t = _read(os.path.join(GPU_HWMON, f"temp{i}_input"), cast=int)
            if label and t is not None:
                out[f"temp_{label}"] = round(t / 1000, 1)
        fan = _read(os.path.join(GPU_HWMON, "fan1_input"), cast=int)
        if fan is not None:
            out["fan_rpm"] = fan
        power = _read(os.path.join(GPU_HWMON, "power1_average"), cast=int)
        if power is not None:
            out["power_w"] = round(power / 1_000_000, 1)
        sclk = _read(os.path.join(GPU_HWMON, "freq1_input"), cast=int)
        if sclk is not None:
            out["sclk_mhz"] = round(sclk / 1_000_000)
        mclk = _read(os.path.join(GPU_HWMON, "freq2_input"), cast=int)
        if mclk is not None:
            out["mclk_mhz"] = round(mclk / 1_000_000)
    return out


def _ram():
    m = psutil.virtual_memory()
    s = psutil.swap_memory()
    return {
        "model": RAM_MODEL,
        "used": m.used,
        "total": m.total,
        "percent": m.percent,
        "swap_used": s.used,
        "swap_total": s.total,
        "swap_percent": s.percent,
    }


_smartctl_cache = {}   # name -> (temp_c, ts)
_SMARTCTL_TTL = 30.0


def _smartctl_temp(name):
    """Read SATA SSD/HDD temperature via smartctl (cached). Returns °C or None."""
    cached = _smartctl_cache.get(name)
    if cached and time.time() - cached[1] < _SMARTCTL_TTL:
        return cached[0]
    try:
        out = subprocess.check_output(
            ["sudo", "-n", "smartctl", "-A", "-j", f"/dev/{name}"],
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        data = json.loads(out)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        _smartctl_cache[name] = (None, time.time())
        return None
    temp = None
    # Preferred: top-level temperature.current (modern smartctl)
    t = data.get("temperature", {}).get("current")
    if isinstance(t, (int, float)):
        temp = float(t)
    else:
        # Fallback: SMART attribute 194 (Temperature_Celsius) or 190 (Airflow_Temperature)
        for attr in data.get("ata_smart_attributes", {}).get("table", []):
            if attr.get("id") in (194, 190):
                raw = attr.get("raw", {}).get("value")
                if isinstance(raw, (int, float)):
                    # raw is sometimes packed (e.g. min/max embedded); take the low byte
                    temp = float(raw & 0xFF) if raw > 0xFF else float(raw)
                    break
    _smartctl_cache[name] = (temp, time.time())
    return round(temp, 1) if temp is not None else None


def _drive_temp(block_path):
    try:
        dev = os.path.realpath(os.path.join(block_path, "device"))
    except OSError:
        return None
    candidates = []
    # Pattern A: hwmon dir grouped (e.g. amdgpu): dev/hwmon/hwmonN
    hd = os.path.join(dev, "hwmon")
    if os.path.isdir(hd):
        for h in sorted(os.listdir(hd)):
            candidates.append(os.path.join(hd, h, "temp1_input"))
    # Pattern B: hwmon directly under controller (e.g. NVMe): dev/hwmonN
    try:
        for entry in sorted(os.listdir(dev)):
            if entry.startswith("hwmon"):
                candidates.append(os.path.join(dev, entry, "temp1_input"))
    except OSError:
        pass
    for path in candidates:
        t = _read(path, cast=int)
        if t is not None:
            return round(t / 1000, 1)
    # Fallback: SATA drives need smartctl (no hwmon exposure)
    name = os.path.basename(block_path)
    if not name.startswith("nvme"):
        return _smartctl_temp(name)
    return None


def _drives():
    paths = sorted(
        glob.glob("/sys/block/sd[a-z]")
        + glob.glob("/sys/block/nvme[0-9]*n[0-9]*")
        + glob.glob("/sys/block/mmcblk[0-9]*")
    )
    parts_by_disk = {}
    for p in psutil.disk_partitions(all=False):
        dev = os.path.basename(p.device)
        parent = re.sub(r"p?\d+$", "", dev)
        try:
            u = psutil.disk_usage(p.mountpoint)
        except OSError:
            continue
        cur = parts_by_disk.get(parent)
        if cur is None or u.total > cur["total"]:
            parts_by_disk[parent] = {
                "mount": p.mountpoint,
                "used": u.used,
                "total": u.total,
                "percent": u.percent,
            }

    out = []
    for path in paths:
        name = os.path.basename(path)
        usage = parts_by_disk.get(name)
        if usage is None:
            continue  # hide unmounted drives
        model = _read(os.path.join(path, "device", "model")) or ""
        model = " ".join(model.split())
        sectors = _read(os.path.join(path, "size"), cast=int) or 0
        rot = _read(os.path.join(path, "queue", "rotational"), cast=int)
        if name.startswith("nvme"):
            kind = "nvme"
        elif rot:
            kind = "hdd"
        else:
            kind = "ssd"
        out.append({
            "name": name,
            "model": model,
            "size": sectors * 512,
            "kind": kind,
            "temp_c": _drive_temp(path),
            "usage": usage,
        })
    return out


def _disk():
    r, w = _disk_speed()
    return {"drives": _drives(), "read_bps": r, "write_bps": w}


_public_ip = None
_public_ip_lock = threading.Lock()


def _refresh_public_ip():
    """Background-refresh the public IP every 10 min — never blocks collect()."""
    global _public_ip
    while True:
        try:
            req = urllib.request.Request(
                "https://api.ipify.org",
                headers={"User-Agent": "pi-vitals/1.0"},
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                ip = r.read().decode().strip()
                if ip and len(ip) <= 45:  # sane bound (IPv6 is ≤45 chars)
                    with _public_ip_lock:
                        _public_ip = ip
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(600)


threading.Thread(target=_refresh_public_ip, daemon=True).start()


def _net():
    up, down = _net_speed()
    nvme_temps = []
    for h in NVME_HWMONS:
        t = _read(os.path.join(h, "temp1_input"), cast=int)
        if t is not None:
            nvme_temps.append(round(t / 1000, 1))
    with _public_ip_lock:
        ip = _public_ip
    return {"up_bps": up, "down_bps": down, "nvme_temps_c": nvme_temps, "public_ip": ip}


def collect():
    return {
        "ts": time.time(),
        "host": socket.gethostname(),
        "uptime_s": int(time.time() - _BOOT_TIME),
        "cpu": _cpu(),
        "gpu": _gpu(),
        "ram": _ram(),
        "disk": _disk(),
        "net": _net(),
    }


# Prime the deltas
psutil.cpu_percent(percpu=True)
_net_speed()
_disk_speed()
