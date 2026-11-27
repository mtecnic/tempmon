#!/usr/bin/env python3
"""tempmon - a simple TUI to watch GPU/CPU temps, fans, and load during inference.

Refreshes every N seconds (default 2s) from nvidia-smi + lm-sensors, colour-codes
temperatures against throttle thresholds, and logs to disk every cycle so that if
the box crashes you keep proof of how hot it got right before it went down.

Two log files are written and fsync'd every cycle (so they survive a hard crash):
  logs/tempmon_<start>.csv        full per-cycle timeline (wide CSV, one row/cycle)
  logs/tempmon_<start>_peak.txt   running peak of every metric + when it happened

Keys:  q / Ctrl-C = quit    p = reset peaks
"""

import argparse
import curses
import glob
import json
import locale
import os
import re
import subprocess
import sys
import threading
import time

# ---------------------------------------------------------------------------
# Thresholds (deg C). (warn, hot, crit) -> colour steps. Tuned for RTX 3090 +
# AMD (k10temp). GPUs throttle ~83C; EPYC/Threadripper Tctl runs hot by design.
# ---------------------------------------------------------------------------
GPU_TEMP = (60, 75, 84)
CPU_TEMP = (70, 85, 95)
GEN_TEMP = (55, 70, 82)   # nvme / misc
VRAM_TEMP = (85, 95, 105)  # GDDR6X junction: throttles ~105C, hard limit ~110C

# Non-temperature thresholds, same (warn, hot, crit) shape.
CPU_UTIL = (50, 80, 95)    # % busy across all threads (and per thread)
MEM_USED = (75, 90, 95)    # % of RAM in use (MemTotal - MemAvailable) / swap use
PSI_STALL = (5, 20, 40)    # % of wall time tasks stalled (PSI "some" avg10)

# colour pair ids
C_OK, C_WARN, C_HOT, C_CRIT, C_HEAD, C_DIM, C_LABEL = 1, 2, 3, 4, 5, 6, 7
C_ACCENT, C_TRACK = 8, 9


def color_for(val, thr):
    warn, hot, crit = thr
    if val >= crit:
        return C_CRIT
    if val >= hot:
        return C_HOT
    if val >= warn:
        return C_WARN
    return C_OK


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------
GPU_QUERY = ("index,name,temperature.gpu,fan.speed,utilization.gpu,"
             "memory.used,memory.total,power.draw,power.limit,pci.bus_id,"
             "clocks.gr,clocks.mem,clocks.max.gr,clocks.max.mem")


def read_gpus():
    """Return list of dicts, one per GPU. Empty list on failure."""
    try:
        out = subprocess.run(
            ["nvidia-smi", f"--query-gpu={GPU_QUERY}",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    gpus = []
    for line in out.strip().splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) < 14:
            continue
        def num(x):
            try:
                return float(x)
            except ValueError:
                return None
        gpus.append({
            "idx": f[0], "name": f[1].replace("NVIDIA ", "").replace("GeForce ", ""),
            "temp": num(f[2]), "fan": num(f[3]), "util": num(f[4]),
            "mem_used": num(f[5]), "mem_total": num(f[6]),
            "power": num(f[7]), "power_lim": num(f[8]),
            "bus": pci_bus(f[9]), "vram": None,
            "sclk": num(f[10]), "mclk": num(f[11]),
            "sclk_max": num(f[12]), "mclk_max": num(f[13]),
        })
    return gpus


def pci_bus(bus_id):
    """nvidia-smi pci.bus_id '00000000:08:00.0' -> bus number int (0x08).

    Used to join nvidia-smi's per-index rows with gddr6's PCI-ordered output,
    since the two tools enumerate GPUs in different orders. None on any oddity.
    """
    try:
        return int(bus_id.split(":")[1], 16)
    except (IndexError, ValueError, AttributeError):
        return None


def read_sensors():
    """Return list of (label, value_c) temps from lm-sensors. Empty on failure."""
    try:
        out = subprocess.run(["sensors", "-j"], capture_output=True,
                             text=True, timeout=5, check=True).stdout
        data = json.loads(out)
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
        return []
    temps = []
    for chip, feats in data.items():
        # short chip tag, e.g. k10temp-pci-00cb -> k10temp/cb, so multiple
        # instances of the same chip (dual socket) stay distinguishable.
        tag = chip.split("-")[0]
        suffix = chip.split("-")[-1][-2:] if "-" in chip else ""
        label = f"{tag}/{suffix}" if suffix else tag
        for feat, sub in feats.items():
            if not isinstance(sub, dict):
                continue
            for k, v in sub.items():
                if k.endswith("_input") and isinstance(v, (int, float)):
                    temps.append((f"{label}:{feat}", float(v)))
                    break
    return temps


# ---------------------------------------------------------------------------
# System power (CPU package via Intel RAPL; GPUs summed from nvidia-smi)
# ---------------------------------------------------------------------------
class PowerMeter:
    """CPU package power (watts) from Intel RAPL energy counters.

    RAPL exposes a monotonically-increasing microjoule counter per socket at
    /sys/class/powercap/intel-rapl:N/energy_uj; power = delta-energy / delta-t
    across cycles. The counter wraps at max_energy_range_uj (~65 kJ here, so
    every few minutes), handled per-package. energy_uj is often root-only
    (a side-channel mitigation); if unreadable, watts() just returns None and
    the caller shows GPU-only power. Needs two samples before it reports.
    """

    def __init__(self):
        self.pkgs = []          # (energy_uj path, max_range_uj) per socket
        self.prev = None        # aligned list of last microjoule readings
        self.prev_t = None
        base = "/sys/class/powercap"
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            entries = []
        for e in entries:
            if not re.fullmatch(r"intel-rapl:\d+", e):  # top-level packages only
                continue
            d = os.path.join(base, e)
            try:
                with open(os.path.join(d, "name")) as f:
                    if not f.read().strip().startswith("package"):
                        continue
                with open(os.path.join(d, "max_energy_range_uj")) as f:
                    mr = int(f.read())
            except (OSError, ValueError):
                continue
            self.pkgs.append((os.path.join(d, "energy_uj"), mr))

    def watts(self):
        now = time.monotonic()
        cur = []
        for path, _ in self.pkgs:
            try:
                with open(path) as f:
                    cur.append(int(f.read()))
            except (OSError, ValueError):
                self.prev = None
                return None
        if not cur:
            return None
        if self.prev is None or len(self.prev) != len(cur) or self.prev_t is None:
            self.prev, self.prev_t = cur, now
            return None
        dt = now - self.prev_t
        self.prev, prev = cur, self.prev
        self.prev_t = now
        if dt <= 0:
            return None
        total = 0.0
        for i, (_, mr) in enumerate(self.pkgs):
            d = cur[i] - prev[i]
            if d < 0:            # counter wrapped
                d += mr
            total += (d / 1e6) / dt
        return total


def _read(path):
    """Stripped contents of a small sysfs/procfs file, or None."""
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def short_cpu_name(name):
    """'AMD Ryzen Threadripper PRO 3955WX 16-Cores' -> 'Ryzen Threadripper PRO
    3955WX'; strips vendor, (R)/(TM), core-count suffixes and Intel '@ x GHz'."""
    s = re.sub(r"\((R|TM|tm)\)", "", name)
    s = re.sub(r"\s+@.*$", "", s)
    s = re.sub(r"\b\d+-Core(s)?\b", "", s, flags=re.I)
    s = re.sub(r"\bwith Radeon.*$", "", s)
    s = re.sub(r"\b(AMD|Intel|CPU|Processor)\b", "", s)
    return " ".join(s.split()) or name


class SysStats:
    """CPU / RAM / pressure / top-process stats from /proc and /sys.

    Pure file reads, no subprocesses, no root needed. Rates (CPU util, per-
    process CPU%) are deltas between successive sample() calls, so the first
    sample reports them as None. Never raises: an unreadable source just leaves
    its keys absent (consumers use .get), per the degrade-don't-crash contract.
    """

    _SYS_CPU = "/sys/devices/system/cpu"

    def __init__(self):
        self.prev_stat = None
        self.prev_procs = {}     # (pid, starttime) -> utime+stime ticks
        self.prev_t = None
        try:
            self.hz = os.sysconf("SC_CLK_TCK")
            self.page_kb = os.sysconf("SC_PAGE_SIZE") // 1024
        except (OSError, ValueError):
            self.hz, self.page_kb = 100, 4
        self.freq_paths = glob.glob(
            f"{self._SYS_CPU}/cpu[0-9]*/cpufreq/scaling_cur_freq")
        try:
            self.info = self._static()
        except Exception:
            self.info = {}

    def _static(self):
        """Fixed facts shown in the section header: model, topology, freq range."""
        info = {}
        txt = _read("/proc/cpuinfo") or ""
        phys, cores, threads, pid = set(), set(), 0, None
        for line in txt.splitlines():
            k, _, v = line.partition(":")
            k, v = k.strip(), v.strip()
            if k == "processor":
                threads += 1
            elif k == "model name" and "model" not in info:
                info["model"] = short_cpu_name(v)
            elif k == "physical id":
                pid = v
                phys.add(v)
            elif k == "core id":
                cores.add((pid, v))
        info["threads"] = threads or os.cpu_count()
        info["cores"] = len(cores) or None
        info["sockets"] = len(phys) or None
        for key, fn in (("fmin", "cpuinfo_min_freq"), ("fmax", "cpuinfo_max_freq")):
            v = _read(f"{self._SYS_CPU}/cpu0/cpufreq/{fn}")
            info[key] = int(v) / 1000 if v and v.isdigit() else None
        info["boost"] = _read(f"{self._SYS_CPU}/cpufreq/boost") == "1"
        info["governor"] = _read(f"{self._SYS_CPU}/cpu0/cpufreq/scaling_governor")
        return info

    def sample(self):
        now = time.monotonic()
        dt = None if self.prev_t is None else now - self.prev_t
        self.prev_t = now
        s = {}
        for fn in (self._cpu, self._mem, self._load, self._freq, self._psi):
            try:
                s.update(fn())
            except Exception:
                pass  # one bad source must not take out the others
        try:
            s["top_cpu"], s["top_mem"] = self._procs(dt)
        except Exception:
            s["top_cpu"], s["top_mem"] = [], []
        return s

    def _cpu(self):
        cur = {}
        with open("/proc/stat") as f:
            for line in f:
                if not line.startswith("cpu"):
                    continue
                p = line.split()
                try:
                    v = [int(x) for x in p[1:9]]
                except ValueError:
                    continue
                v += [0] * (8 - len(v))
                user, nice, system, idle, iowait, irq, softirq, steal = v
                # total, idle-ish, iowait, user-ish, system-ish
                cur[p[0]] = (sum(v), idle + iowait, iowait,
                             user + nice, system + irq + softirq)
        prev, self.prev_stat = self.prev_stat, cur
        if not prev:
            return {}

        def pct(name):
            a, b = cur.get(name), prev.get(name)
            if not a or not b or a[0] - b[0] <= 0:
                return None
            d = a[0] - b[0]
            busy = 100 * (d - (a[1] - b[1])) / d
            return (max(0.0, min(100.0, busy)), 100 * (a[2] - b[2]) / d,
                    100 * (a[3] - b[3]) / d, 100 * (a[4] - b[4]) / d)

        out = {}
        agg = pct("cpu")
        if agg:
            out.update(cpu_util=agg[0], cpu_iowait=agg[1],
                       cpu_usr=agg[2], cpu_sys=agg[3])
        names = sorted((k for k in cur if k != "cpu"), key=lambda k: int(k[3:]))
        out["per_core"] = [(pct(k) or (None,))[0] for k in names]
        return out

    def _mem(self):
        m = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                try:
                    m[k] = int(v.split()[0])  # kB (HugePages_* are counts)
                except (ValueError, IndexError):
                    pass
        out = {}
        tot, av = m.get("MemTotal"), m.get("MemAvailable")
        if tot:
            out["mem_total"] = tot / 1024
            if av is not None:
                out["mem_used"] = (tot - av) / 1024
                out["mem_avail"] = av / 1024
        shm = m.get("Shmem", 0)
        if "Cached" in m:
            # page cache minus shmem (tmpfs / /dev/shm lives in Cached but
            # can't be dropped), so "cache" ~= what the kernel can reclaim
            out["mem_cache"] = max(0, m["Cached"] + m.get("Buffers", 0) - shm) / 1024
        if "Shmem" in m:
            out["mem_shmem"] = shm / 1024
        if "Dirty" in m:
            out["mem_dirty"] = (m["Dirty"] + m.get("Writeback", 0)) / 1024
        if "SwapTotal" in m:
            out["swap_total"] = m["SwapTotal"] / 1024
            out["swap_used"] = (m["SwapTotal"] - m.get("SwapFree", 0)) / 1024
        huge = m.get("HugePages_Total", 0) * m.get("Hugepagesize", 0)
        if huge:
            out["mem_huge"] = huge / 1024
        return out

    def _load(self):
        p = (_read("/proc/loadavg") or "").split()
        out = {"load1": float(p[0]), "load5": float(p[1]), "load15": float(p[2])}
        run, _, tasks = p[3].partition("/")
        out["running"], out["tasks"] = int(run), int(tasks)
        return out

    def _freq(self):
        vals = []
        for path in self.freq_paths:
            v = _read(path)
            if v and v.isdigit():
                vals.append(int(v) / 1000)
        if not vals:
            return {}
        return {"freq_avg": sum(vals) / len(vals), "freq_max": max(vals)}

    def _psi(self):
        out = {}
        for res in ("cpu", "memory", "io"):
            for line in (_read(f"/proc/pressure/{res}") or "").splitlines():
                kind, *fields = line.split()
                for fld in fields:
                    if fld.startswith("avg10="):
                        out[f"psi_{res}_{kind}"] = float(fld[6:])
        return out

    def _procs(self, dt):
        """Top 3 processes by CPU% (since last sample) and by RSS."""
        cur, rows = {}, []
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/stat", "rb") as f:
                    raw = f.read()
                lp, rp = raw.index(b"("), raw.rindex(b")")
                comm = raw[lp + 1:rp].decode(errors="replace")
                fld = raw[rp + 2:].split()
                ticks = int(fld[11]) + int(fld[12])    # utime + stime
                key = (pid, fld[19])                   # starttime guards pid reuse
                rss = int(fld[21]) * self.page_kb / 1024  # MB
            except (OSError, ValueError, IndexError):
                continue  # process exited mid-read, or odd stat line
            cur[key] = ticks
            prev = self.prev_procs.get(key)
            cpu = (100 * (ticks - prev) / self.hz / dt
                   if prev is not None and dt else None)
            rows.append((comm, cpu, rss))
        self.prev_procs = cur
        top_cpu = sorted((r for r in rows if r[1]), key=lambda r: -r[1])[:3]
        top_mem = sorted(rows, key=lambda r: -r[2])[:3]
        return top_cpu, top_mem


# CSV columns for system stats: (column name, SysStats key). One list so the
# header and row stay in lockstep. Appended after the power columns so older
# CSV consumers' column positions don't move.
SYS_LOG = [
    ("cpu_util_pct", "cpu_util"), ("cpu_iowait_pct", "cpu_iowait"),
    ("cpu_freq_avg_mhz", "freq_avg"), ("cpu_freq_max_mhz", "freq_max"),
    ("load_1m", "load1"),
    ("mem_used_mb", "mem_used"), ("mem_avail_mb", "mem_avail"),
    ("mem_cache_mb", "mem_cache"), ("mem_shmem_mb", "mem_shmem"),
    ("mem_dirty_mb", "mem_dirty"), ("swap_used_mb", "swap_used"),
    ("psi_cpu_some", "psi_cpu_some"), ("psi_mem_some", "psi_memory_some"),
    ("psi_mem_full", "psi_memory_full"), ("psi_io_some", "psi_io_some"),
]


def gpu_power_total(gpus):
    """Sum of per-GPU power draw (W), or None if no GPU reported power."""
    vals = [g["power"] for g in gpus if g["power"] is not None]
    return sum(vals) if vals else None


# ---------------------------------------------------------------------------
# GDDR6X VRAM temps (via olealgoritme/gddr6, which needs root)
# ---------------------------------------------------------------------------
class VramReader:
    """Latest GDDR6/GDDR6X VRAM junction temps, keyed by PCI bus number.

    nvidia-smi can't read VRAM temp on GeForce cards, so we lean on the `gddr6`
    binary (mmaps the GPU BAR; requires root). It has no one-shot mode -- it
    prints one carriage-return-updated line per second forever:

        Device: RTX 3090 GDDR6X (GA102 / 0x2204) pci=8:0:0     (once, per card)
        \\rVRAM Temps: |  32C |  30C | ...                       (every second)

    So we spawn it once under `sudo -n`, read continuously in a daemon thread,
    and expose the newest reading. gddr6 lists cards in PCI-discovery order
    (not nvidia-smi's index order), so temps are mapped positionally onto the
    bus numbers parsed from its Device lines, then looked up by bus.

    Never raises: a missing binary, missing sudo rights, or a dead subprocess
    just leaves available=False and get() returning None -- matching tempmon's
    degrade-don't-crash contract. Cleanup is automatic: when tempmon exits the
    read end of the pipe closes, gddr6 takes SIGPIPE on its next write (it
    installs no SIGPIPE handler) and dies, so no root process is left behind.
    """

    _DEV_RE = re.compile(rb"pci=([0-9a-fA-F]+):")
    _TEMP_RE = re.compile(rb"(\d+)\xc2\xb0C")  # NN°C, degree sign is UTF-8 c2 b0

    def __init__(self, bin_path):
        self.bus_order = []   # PCI bus ints, in gddr6's device order
        self.temps = {}       # bus int -> latest temp (float C)
        self.available = False
        self.proc = None
        if not os.path.exists(bin_path):
            return
        try:
            self.proc = subprocess.Popen(
                ["sudo", "-n", bin_path],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        except OSError:
            return
        self.available = True
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        buf = b""
        try:
            while True:
                chunk = self.proc.stdout.read(256)
                if not chunk:
                    break
                # gddr6 separates updates with \r (a prefix on the next line);
                # treat CR and LF alike, parse complete segments, keep the tail.
                buf = (buf + chunk).replace(b"\r", b"\n")
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    if line.startswith(b"Device:"):
                        m = self._DEV_RE.search(line)
                        if m:
                            self.bus_order.append(int(m.group(1), 16))
                    elif b"VRAM Temps:" in line:
                        vals = self._TEMP_RE.findall(line)
                        for i, v in enumerate(vals):
                            if i < len(self.bus_order):
                                self.temps[self.bus_order[i]] = float(v)
        except (OSError, ValueError):
            pass
        finally:
            self.available = False

    def get(self, bus):
        return self.temps.get(bus)

    def close(self):
        if not self.proc:
            return
        # gddr6 runs as root under `sudo -n`, so we can't signal it directly and
        # it ignores SIGTERM. The real kill switch is closing the read end of its
        # pipe: gddr6 takes SIGPIPE on its next write (~1s; it installs no
        # SIGPIPE handler) and exits, which lets the sudo wrapper exit too. We
        # then wait so we don't return while a half-dead gddr6 could still be
        # holding the GPU BAR -- a lingering instance corrupts the next run's
        # readings (0C / garbage from BAR contention).
        try:
            self.proc.stdout.close()
        except OSError:
            pass
        try:
            self.proc.terminate()  # nudge the sudo wrapper; harmless to gddr6
        except OSError:
            pass
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            # SIGPIPE didn't take within 2s. SIGKILL only reaps the sudo wrapper
            # (it can't reach the root child, and would orphan it) -- but the
            # pipe is already closed, so gddr6 still dies on its next write. This
            # just ensures we don't leave the wrapper hanging at exit.
            try:
                self.proc.kill()
                self.proc.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
class Logger:
    def __init__(self, logdir, start_str, vram=False, keep=0):
        os.makedirs(logdir, exist_ok=True)
        self.logdir = logdir
        self.csv_path = os.path.join(logdir, f"tempmon_{start_str}.csv")
        self.peak_path = os.path.join(logdir, f"tempmon_{start_str}_peak.txt")
        self.csv = open(self.csv_path, "w", buffering=1)
        self.header = None
        self.vram = vram
        # Rotate old sessions now that this run's files exist and are newest.
        self.pruned = prune_logs(logdir, keep) if keep and keep > 0 else 0

    def _columns(self, gpus, sensors):
        cols = ["timestamp"]
        for g in gpus:
            i = g["idx"]
            cols += [f"gpu{i}_temp", f"gpu{i}_fan", f"gpu{i}_util",
                     f"gpu{i}_mem_used_mb", f"gpu{i}_power_w",
                     f"gpu{i}_core_clock_mhz", f"gpu{i}_mem_clock_mhz"]
            if self.vram:
                cols.append(f"gpu{i}_vram_temp")
        for name, _ in sensors:
            cols.append("cpu_" + name.replace(" ", "_"))
        cols += ["gpu_power_total_w", "cpu_package_w", "system_power_w"]
        cols += [c for c, _ in SYS_LOG]
        return cols

    def write_cycle(self, ts_str, gpus, sensors, power_info=None, sysstats=None):
        if self.header is None:
            self.header = self._columns(gpus, sensors)
            self.csv.write(",".join(self.header) + "\n")
        row = [ts_str]
        for g in gpus:
            row += [fmt(g["temp"]), fmt(g["fan"]), fmt(g["util"]),
                    fmt(g["mem_used"]), fmt(g["power"]),
                    fmt(g["sclk"]), fmt(g["mclk"])]
            if self.vram:
                row.append(fmt(g.get("vram")))
        for _, v in sensors:
            row.append(fmt(v))
        pi = power_info or {}
        row += [fmt(pi.get("gpu_w")), fmt(pi.get("cpu_w")), fmt(pi.get("sys_w"))]
        ss = sysstats or {}
        row += [fmt(ss.get(k)) for _, k in SYS_LOG]
        self.csv.write(",".join(row) + "\n")
        self.csv.flush()
        os.fsync(self.csv.fileno())

    def write_peaks(self, peaks, start_str, uptime):
        with open(self.peak_path, "w") as f:
            f.write(f"tempmon peak report\n")
            f.write(f"session start : {start_str}\n")
            f.write(f"last update   : {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"monitored for : {uptime}\n")
            f.write("-" * 52 + "\n")
            f.write(f"{'metric':<24}{'peak':>10}   {'reached at'}\n")
            for name, (val, when) in peaks.items():
                f.write(f"{name:<24}{val:>10.1f}   {when}\n")
            f.flush()
            os.fsync(f.fileno())

    def close(self):
        try:
            self.csv.close()
        except OSError:
            pass


def prune_logs(logdir, keep):
    """Keep only the `keep` most recent sessions in `logdir`, deleting older
    ones' .csv + _peak.txt files. A session is one tempmon_<start>.csv; the
    start timestamp sorts chronologically, so the newest `keep` names win
    (the just-created current session is newest, so it is always retained).

    Never raises: a file we can't unlink (e.g. an old root-owned log) is
    skipped, matching tempmon's degrade-don't-crash contract. Returns the
    number of files removed.
    """
    try:
        sessions = sorted(glob.glob(os.path.join(logdir, "tempmon_*.csv")))
    except OSError:
        return 0
    removed = 0
    for csv_path in sessions[:-keep]:  # everything but the newest `keep`
        peak_path = csv_path[:-len(".csv")] + "_peak.txt"
        for path in (csv_path, peak_path):
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass  # missing peak file, or no permission (root-owned) -- skip
    return removed


def fmt(v):
    return "" if v is None else f"{v:.1f}"


# ---------------------------------------------------------------------------
# Peak tracking
# ---------------------------------------------------------------------------
def update_peaks(peaks, gpus, sensors, power_info=None, sysstats=None):
    now = time.strftime("%H:%M:%S")
    def bump(name, val):
        if val is None:
            return
        if name not in peaks or val > peaks[name][0]:
            peaks[name] = (val, now)
    for g in gpus:
        bump(f"GPU{g['idx']} temp", g["temp"])
        bump(f"GPU{g['idx']} vram", g.get("vram"))
        bump(f"GPU{g['idx']} fan%", g["fan"])
        bump(f"GPU{g['idx']} power", g["power"])
    for name, v in sensors:
        bump(name, v)
    if power_info:
        bump("system power", power_info.get("sys_w"))
    if sysstats:
        gib = lambda mb: None if mb is None else mb / 1024
        bump("CPU util%", sysstats.get("cpu_util"))
        bump("load 1m", sysstats.get("load1"))
        bump("RAM used G", gib(sysstats.get("mem_used")))
        bump("swap used G", gib(sysstats.get("swap_used")))
        bump("mem stall%", sysstats.get("psi_memory_some"))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
# GPU table column layout: (x offset, width). Values are right-aligned into
# each cell; the temp bar is drawn separately in the gap after `core`.
GPU_COLS = {
    "idx":  (1, 3),
    "core": (4, 4),
    "bar":  (9, 12),
    "vram": (22, 5),
    "fan":  (28, 5),
    "util": (34, 5),
    "sclk": (40, 6),
    "mclk": (47, 6),
    "mem":  (54, 12),
    "pwr":  (67, 11),
}


def draw(stdscr, gpus, sensors, peaks, interval, logger, start_str, uptime,
         vram_status="", power_info=None, sysstats=None, sysinfo=None):
    h, w = stdscr.getmaxyx()
    stdscr.erase()

    def put(y, x, text, attr=0):
        if 0 <= y < h and 0 <= x < w:
            try:
                stdscr.addnstr(y, x, text, max(0, w - x - 1), attr)
            except curses.error:
                pass  # wide glyph at the last cell etc. -- never crash on draw

    def cell(y, key, text, attr=0):
        x, cw = GPU_COLS[key]
        put(y, x, text[:cw].rjust(cw), attr)

    def rule(y):
        put(y, 0, "─" * max(0, w - 1), curses.color_pair(C_DIM))

    def section(y, label, note="", note_col=C_DIM):
        put(y, 0, "▌", curses.color_pair(C_ACCENT) | bold)
        put(y, 2, label, curses.color_pair(C_ACCENT) | bold)
        if note:
            put(y, 4 + len(label), note, curses.color_pair(note_col))

    def seq(y, x, parts):
        """Write (text, attr) parts left-to-right; put() clips at the edge, so
        order parts most-important-first and narrow terminals drop the tail."""
        for text, attr in parts:
            put(y, x, text, attr)
            x += len(text)
        return x

    bold = curses.A_BOLD
    row = 0

    # ---- title bar ----
    put(row, 0, " tempmon ", curses.color_pair(C_HEAD) | bold)
    put(row, 10,
        f"refresh {interval:g}s  ·  up {uptime}  ·  {time.strftime('%H:%M:%S')}",
        curses.color_pair(C_DIM))
    put(row, w - 22, "[q]uit  [p]eak-reset", curses.color_pair(C_DIM))
    row += 1
    rule(row); row += 1

    # ---- GPUs ----
    section(row, "GPUs", vram_status,
            C_OK if "unavailable" not in vram_status else C_WARN)
    row += 1
    if not gpus:
        put(row, 2, "nvidia-smi unavailable", curses.color_pair(C_CRIT)); row += 1
    else:
        hcol = curses.color_pair(C_DIM)
        for key, label in (("idx", "#"), ("core", "core"), ("vram", "vram"),
                           ("fan", "fan"), ("util", "util"), ("sclk", "sclk"),
                           ("mclk", "mclk"), ("mem", "memory"), ("pwr", "power")):
            cell(row, key, label, hcol)
        put(row, GPU_COLS["bar"][0], "temp", hcol)
        row += 1
        for g in gpus:
            cell(row, "idx", g["idx"], curses.color_pair(C_LABEL) | bold)
            tcol = (curses.color_pair(color_for(g["temp"], GPU_TEMP)) | bold
                    if g["temp"] is not None else curses.color_pair(C_DIM))
            cell(row, "core", ival(g["temp"]) + "°", tcol)
            draw_tempbar(put, row, GPU_COLS["bar"][0], g["temp"], GPU_TEMP,
                         GPU_COLS["bar"][1])
            vram = g.get("vram")
            vcol = (curses.color_pair(color_for(vram, VRAM_TEMP)) | bold
                    if vram is not None else curses.color_pair(C_DIM))
            cell(row, "vram", ival(vram) + "°", vcol)
            cell(row, "fan", ival(g["fan"]) + "%",
                 curses.color_pair(fan_color(g["fan"])))
            cell(row, "util", ival(g["util"]) + "%",
                 curses.color_pair(C_OK if (g["util"] or 0) >= 5 else C_DIM))
            cell(row, "sclk", ival(g["sclk"]),
                 curses.color_pair(clock_color(g["sclk"], g["sclk_max"])))
            cell(row, "mclk", ival(g["mclk"]),
                 curses.color_pair(clock_color(g["mclk"], g["mclk_max"])))
            mem = "-"
            if g["mem_used"] is not None and g["mem_total"] is not None:
                mem = f"{g['mem_used']/1024:.1f}/{g['mem_total']/1024:.0f}G"
            cell(row, "mem", mem)
            pw = "-"
            if g["power"] is not None and g["power_lim"] is not None:
                pw = f"{g['power']:.0f}/{g['power_lim']:.0f}W"
            pcol = C_OK
            if g["power"] is not None and g["power_lim"]:
                pcol = fan_color(100 * g["power"] / g["power_lim"])
            cell(row, "pwr", pw, curses.color_pair(pcol))
            row += 1
    row += 1

    # ---- Power ----
    pi = power_info or {}
    if pi.get("sys_w") is not None:
        section(row, "Power")
        x = 10
        for lbl, key in (("GPU", "gpu_w"), ("CPU", "cpu_w")):
            if pi.get(key) is not None:
                put(row, x, f"{lbl} ", curses.color_pair(C_DIM))
                put(row, x + 4, f"{pi[key]:.0f}W", bold); x += 12
        put(row, x, "total ", curses.color_pair(C_DIM))
        put(row, x + 6, f"{pi['sys_w']:.0f}W", curses.color_pair(C_ACCENT) | bold)
        if pi.get("cpu_w") is None:
            put(row, x + 6 + len(f"{pi['sys_w']:.0f}W") + 2, "(GPU only)",
                curses.color_pair(C_DIM))
        row += 2

    # ---- CPU + Memory ----
    row = draw_system(put, seq, section, row, w, sysstats or {}, sysinfo or {})

    # ---- Sensors ----
    section(row, "Sensors"); row += 1
    if not sensors:
        put(row, 2, "sensors unavailable", curses.color_pair(C_CRIT)); row += 1
    else:
        col_w = 34
        ncols = max(1, w // col_w)
        for i, (name, v) in enumerate(sensors):
            cy = row + i // ncols
            cx = 2 + (i % ncols) * col_w
            thr = (CPU_TEMP if name.startswith(("Tctl", "Tdie", "Tccd", "k10"))
                   else GEN_TEMP)
            put(cy, cx, f"{name:<18}", curses.color_pair(C_DIM))
            put(cy, cx + 18, f"{v:4.0f}°",
                curses.color_pair(color_for(v, thr)) | bold)
            lo, hi = 30, thr[2] + 5
            draw_meter(put, cy, cx + 24, (v - lo) / (hi - lo), 8,
                       color_for(v, thr))
        row += (len(sensors) + ncols - 1) // ncols
    row += 1

    # ---- Peaks ----
    section(row, "Session peaks",
            f"logging → {os.path.relpath(logger.csv_path)}")
    row += 1
    items = list(peaks.items())
    col_w = 30
    ncols = max(1, w // col_w)
    for i, (name, (val, when)) in enumerate(items):
        cy = row + i // ncols
        cx = 2 + (i % ncols) * col_w
        is_temp = "temp" in name or "vram" in name
        if "vram" in name:
            thr = VRAM_TEMP
        elif "temp" in name:
            thr = GPU_TEMP
        else:
            thr = GEN_TEMP
        put(cy, cx, f"{name:<13}", curses.color_pair(C_DIM))
        attr = curses.color_pair(color_for(val, thr)) | bold if is_temp else bold
        put(cy, cx + 13, f"{val:5.0f}", attr)
        put(cy, cx + 19, f" @{when}", curses.color_pair(C_DIM))

    stdscr.refresh()


_SPARK = "▁▂▃▄▅▆▇█"


def draw_system(put, seq, section, row, w, ss, si):
    """CPU and Memory sections. Returns the next free row."""
    bold = curses.A_BOLD
    dim = curses.color_pair(C_DIM)
    val = lambda v, thr: (curses.color_pair(color_for(v, thr)) | bold
                          if v is not None else dim)
    sep = ("  ·  ", dim)

    # ---- CPU ----
    topo = None
    if si.get("threads"):
        topo = f"{si['threads']}t"
        if si.get("cores"):
            topo = f"{si['cores']}c/{topo}"
        if (si.get("sockets") or 1) > 1:
            topo = f"{si['sockets']}× {topo}"
    rng = None
    if si.get("fmax"):
        rng = f"{si['fmax']/1000:.1f} GHz"
        if si.get("fmin"):
            rng = f"{si['fmin']/1000:.1f}–{rng}"
        if si.get("boost"):
            rng += " +boost"
    note = " · ".join(x for x in (si.get("model"), topo, rng, si.get("governor")) if x)
    section(row, "CPU", note); row += 1

    util = ss.get("cpu_util")
    put(row, 2, "util", dim)
    draw_meter(put, row, 8, (util or 0) / 100, 16,
               color_for(util, CPU_UTIL) if util is not None else C_DIM)
    parts = [(f" {ival(util):>3}%", val(util, CPU_UTIL))]
    if util is not None:
        iow = ss.get("cpu_iowait")
        parts += [("  usr ", dim), (ival(ss.get("cpu_usr")), 0),
                  (" sys ", dim), (ival(ss.get("cpu_sys")), 0),
                  (" iow ", dim), (ival(iow), val(iow, (5, 15, 30)))]
    if ss.get("freq_avg") is not None:
        fcol = curses.color_pair(clock_color(ss["freq_max"], si.get("fmax")))
        parts += [sep, ("clk ", dim), (f"{ss['freq_avg']/1000:.2f}", fcol),
                  (" avg ", dim), (f"{ss['freq_max']/1000:.2f}", fcol),
                  (" max GHz", dim)]
    if ss.get("load1") is not None:
        # colour load relative to thread count: >=1 load per thread is saturated
        n = si.get("threads") or 1
        lcol = val(100 * ss["load1"] / n, (70, 100, 150))
        parts += [sep, ("load ", dim), (f"{ss['load1']:.1f}", lcol),
                  (f" {ss['load5']:.1f} {ss['load15']:.1f}", 0),
                  (f"  run {ss['running']}/{ss['tasks']}", dim)]
    seq(row, 24, parts)
    row += 1

    per = ss.get("per_core") or []
    if per:
        put(row, 2, "cores", dim)
        x = 8
        for i, v in enumerate(per):
            if i and i % 8 == 0:
                x += 1  # gap every 8 threads keeps it countable
                if x + 8 >= w - 1:   # wrap whole groups on narrow terminals
                    row += 1
                    x = 8
            if v is None or v < 5:
                put(row, x, _SPARK[0], curses.color_pair(C_TRACK))
            else:
                put(row, x, _SPARK[min(7, int(v / 12.5))],
                    curses.color_pair(color_for(v, CPU_UTIL)) | bold)
            x += 1
        row += 1

    top = ss.get("top_cpu") or []
    if top:
        put(row, 2, "top", dim)
        parts = []
        for i, (comm, cpu, _) in enumerate(top):
            if i:
                parts.append(sep)
            parts += [(comm[:15] + " ", 0), (f"{cpu:.0f}%", val(cpu, (100, 400, 800)))]
        seq(row, 8, parts)
        row += 1
    row += 1

    # ---- Memory ----
    tot = ss.get("mem_total")
    mnote = f"{gb(tot)} total" if tot else ""
    if ss.get("mem_huge"):
        mnote += f" · hugepages {gb(ss['mem_huge'])}"
    section(row, "Memory", mnote); row += 1
    if tot is None:
        put(row, 2, "/proc/meminfo unavailable", curses.color_pair(C_CRIT))
        return row + 2

    used = ss.get("mem_used")
    upct = None if used is None else 100 * used / tot
    put(row, 2, "ram", dim)
    draw_meter(put, row, 8, (upct or 0) / 100, 16,
               color_for(upct, MEM_USED) if upct is not None else C_DIM)
    seq(row, 24, [(f" {ival(upct):>3}%", val(upct, MEM_USED)),
                  (f"  {gb(used)}", bold), (f"/{gb(tot)}", dim),
                  sep, ("avail ", dim), (gb(ss.get("mem_avail")), 0),
                  ("  cache ", dim), (gb(ss.get("mem_cache")), 0),
                  ("  shm ", dim), (gb(ss.get("mem_shmem")), 0),
                  ("  dirty ", dim), (gb(ss.get("mem_dirty")), 0)])
    row += 1

    put(row, 2, "swap", dim)
    st, su = ss.get("swap_total"), ss.get("swap_used")
    if st:
        spct = 100 * (su or 0) / st
        draw_meter(put, row, 8, spct / 100, 16, color_for(spct, MEM_USED))
        parts = [(f" {spct:>3.0f}%", val(spct, MEM_USED)),
                 (f"  {gb(su)}", bold), (f"/{gb(st)}", dim)]
    else:
        parts = [("none", dim)]
        put(row, 8, "░" * 16, curses.color_pair(C_TRACK))
    psi = [(lbl, ss.get(f"psi_{res}_some"))
           for lbl, res in (("cpu", "cpu"), ("mem", "memory"), ("io", "io"))]
    if any(v is not None for _, v in psi):
        parts += [sep, ("stall ", dim)]
        for lbl, v in psi:
            parts += [(f"{lbl} ", dim), (f"{v:.1f}%" if v is not None else "-",
                                         val(v, PSI_STALL)), ("  ", 0)]
    seq(row, 24, parts)
    row += 1

    top = ss.get("top_mem") or []
    if top:
        put(row, 2, "top", dim)
        parts = []
        for i, (comm, _, rss) in enumerate(top):
            if i:
                parts.append(sep)
            parts += [(comm[:15] + " ", 0), (gb(rss), bold)]
        seq(row, 8, parts)
        row += 1
    return row + 1


def gb(mb):
    """MB -> compact '48.4G' / '312M'; '-' for None."""
    if mb is None:
        return "-"
    return f"{mb/1024:.1f}G" if mb >= 1024 else f"{mb:.0f}M"


def ival(v):
    return "-" if v is None else f"{v:.0f}"


def fan_color(v):
    if v is None:
        return C_DIM
    if v >= 80:
        return C_HOT
    if v >= 50:
        return C_WARN
    return C_OK


def clock_color(cur, mx):
    """Colour a clock by how close it is to its max: bright when boosting,
    dim when parked. Purely informational (a high clock is not a warning)."""
    if cur is None:
        return C_DIM
    if mx is None or mx <= 0:
        return C_LABEL
    frac = cur / mx
    if frac >= 0.7:
        return C_OK
    if frac >= 0.35:
        return C_LABEL
    return C_DIM


# Eighth-width left blocks, for sub-cell bar resolution (1..7 eighths full).
_FRAC_BLOCKS = "▏▎▍▌▋▊▉"


def bar_parts(frac, width):
    """Split a 0..1 fraction into (filled_str, track_str) of total `width`,
    using partial block glyphs so the boundary cell is sub-character accurate."""
    frac = max(0.0, min(1.0, frac))
    eighths = int(round(frac * width * 8))
    full, part = divmod(eighths, 8)
    fill = "█" * full
    if part and full < width:
        fill += _FRAC_BLOCKS[part - 1]
    return fill, "░" * (width - len(fill))


def draw_meter(put, y, x, frac, width, color):
    """Draw a filled bar: coloured fill over a muted track."""
    fill, track = bar_parts(frac, width)
    put(y, x, fill, curses.color_pair(color) | curses.A_BOLD)
    put(y, x + len(fill), track, curses.color_pair(C_TRACK))


def draw_tempbar(put, y, x, v, thr, width=12):
    """Temperature meter, scaled 30..95C and coloured by threshold."""
    if v is None:
        put(y, x, "░" * width, curses.color_pair(C_TRACK))
        return
    lo, hi = 30, 95
    draw_meter(put, y, x, (v - lo) / (hi - lo), width, color_for(v, thr))


# ---------------------------------------------------------------------------
def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(C_OK, curses.COLOR_GREEN, -1)
    curses.init_pair(C_WARN, curses.COLOR_YELLOW, -1)
    curses.init_pair(C_HOT, curses.COLOR_MAGENTA, -1)
    curses.init_pair(C_CRIT, curses.COLOR_RED, -1)
    curses.init_pair(C_HEAD, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(C_DIM, curses.COLOR_CYAN, -1)
    curses.init_pair(C_LABEL, curses.COLOR_WHITE, -1)
    curses.init_pair(C_ACCENT, curses.COLOR_CYAN, -1)
    curses.init_pair(C_TRACK, curses.COLOR_BLUE, -1)


def run(stdscr, args):
    curses.curs_set(0)
    init_colors()
    stdscr.nodelay(True)
    start = time.time()
    start_str = time.strftime("%Y%m%d_%H%M%S")
    vram = None if args.no_vram else VramReader(args.gddr6_bin)
    power = PowerMeter()
    sysmon = SysStats()
    logger = Logger(args.logdir, start_str, vram=vram is not None, keep=args.keep)
    peaks = {}
    try:
        while True:
            gpus = read_gpus()
            if vram is not None:
                for g in gpus:
                    g["vram"] = vram.get(g["bus"])
                vram_status = ("vram via gddr6" if vram.available
                               else "vram: gddr6 unavailable")
            else:
                vram_status = ""
            sensors = read_sensors()
            gpu_w = gpu_power_total(gpus)
            cpu_w = power.watts()
            sys_w = None if gpu_w is None and cpu_w is None else \
                (gpu_w or 0) + (cpu_w or 0)
            power_info = {"gpu_w": gpu_w, "cpu_w": cpu_w, "sys_w": sys_w}
            sysstats = sysmon.sample()
            update_peaks(peaks, gpus, sensors, power_info, sysstats)
            uptime = fmt_dur(time.time() - start)
            logger.write_cycle(time.strftime("%Y-%m-%d %H:%M:%S"), gpus, sensors,
                               power_info, sysstats)
            logger.write_peaks(peaks, start_str, uptime)
            draw(stdscr, gpus, sensors, peaks, args.interval, logger,
                 start_str, uptime, vram_status, power_info,
                 sysstats, sysmon.info)
            # wait for interval, but stay responsive to keys
            deadline = time.time() + args.interval
            while time.time() < deadline:
                ch = stdscr.getch()
                if ch in (ord("q"), ord("Q")):
                    return
                if ch in (ord("p"), ord("P")):
                    peaks.clear()
                    break
                if ch == curses.KEY_RESIZE:
                    break
                time.sleep(0.05)
    finally:
        logger.close()
        if vram is not None:
            vram.close()


def fmt_dur(secs):
    secs = int(secs)
    h, r = divmod(secs, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def main():
    ap = argparse.ArgumentParser(description="TUI GPU/CPU temp + load monitor with crash-proof logging")
    ap.add_argument("-i", "--interval", type=float, default=2.0,
                    help="refresh seconds (default 2)")
    ap.add_argument("-d", "--logdir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"),
                    help="directory for CSV + peak logs (default ./logs)")
    ap.add_argument("--keep", type=int, default=0, metavar="N",
                    help="keep only the N most recent sessions in the log dir, "
                         "deleting older CSV + peak files at startup "
                         "(default 0 = keep all)")
    ap.add_argument("--gddr6-bin",
                    default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "gddr6", "build", "bin", "gddr6"),
                    help="gddr6 binary for GDDR6X VRAM temps; run via `sudo -n` "
                         "(default: bundled build)")
    ap.add_argument("--no-vram", action="store_true",
                    help="disable GDDR6X VRAM temp reading (skips sudo gddr6)")
    args = ap.parse_args()
    # curses needs the locale set for wide/Unicode glyphs (block bars, °) to
    # render instead of turning into garbage.
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(run, args)
    except KeyboardInterrupt:
        pass
    print(f"logs written to {args.logdir}/")


if __name__ == "__main__":
    main()
