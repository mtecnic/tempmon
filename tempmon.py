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
import json
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

# colour pair ids
C_OK, C_WARN, C_HOT, C_CRIT, C_HEAD, C_DIM, C_LABEL = 1, 2, 3, 4, 5, 6, 7


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
             "memory.used,memory.total,power.draw,power.limit,pci.bus_id")


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
        if len(f) < 10:
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
        # Closing the pipe makes gddr6's next write SIGPIPE (it doesn't catch
        # it) so the root process exits; SIGTERM alone won't stop it.
        try:
            self.proc.stdout.close()
        except OSError:
            pass
        try:
            self.proc.terminate()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
class Logger:
    def __init__(self, logdir, start_str, vram=False):
        os.makedirs(logdir, exist_ok=True)
        self.csv_path = os.path.join(logdir, f"tempmon_{start_str}.csv")
        self.peak_path = os.path.join(logdir, f"tempmon_{start_str}_peak.txt")
        self.csv = open(self.csv_path, "w", buffering=1)
        self.header = None
        self.vram = vram

    def _columns(self, gpus, sensors):
        cols = ["timestamp"]
        for g in gpus:
            i = g["idx"]
            cols += [f"gpu{i}_temp", f"gpu{i}_fan", f"gpu{i}_util",
                     f"gpu{i}_mem_used_mb", f"gpu{i}_power_w"]
            if self.vram:
                cols.append(f"gpu{i}_vram_temp")
        for name, _ in sensors:
            cols.append("cpu_" + name.replace(" ", "_"))
        cols += ["gpu_power_total_w", "cpu_package_w", "system_power_w"]
        return cols

    def write_cycle(self, ts_str, gpus, sensors, power_info=None):
        if self.header is None:
            self.header = self._columns(gpus, sensors)
            self.csv.write(",".join(self.header) + "\n")
        row = [ts_str]
        for g in gpus:
            row += [fmt(g["temp"]), fmt(g["fan"]), fmt(g["util"]),
                    fmt(g["mem_used"]), fmt(g["power"])]
            if self.vram:
                row.append(fmt(g.get("vram")))
        for _, v in sensors:
            row.append(fmt(v))
        pi = power_info or {}
        row += [fmt(pi.get("gpu_w")), fmt(pi.get("cpu_w")), fmt(pi.get("sys_w"))]
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


def fmt(v):
    return "" if v is None else f"{v:.1f}"


# ---------------------------------------------------------------------------
# Peak tracking
# ---------------------------------------------------------------------------
def update_peaks(peaks, gpus, sensors, power_info=None):
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


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def draw(stdscr, gpus, sensors, peaks, interval, logger, start_str, uptime,
         vram_status="", power_info=None):
    h, w = stdscr.getmaxyx()
    stdscr.erase()

    def put(y, x, text, attr=0):
        if 0 <= y < h and 0 <= x < w:
            stdscr.addnstr(y, x, text, max(0, w - x - 1), attr)

    bold = curses.A_BOLD
    row = 0
    title = " tempmon "
    put(row, 0, title, curses.color_pair(C_HEAD) | bold)
    put(row, len(title) + 1,
        f"refresh {interval:g}s   up {uptime}   {time.strftime('%H:%M:%S')}   "
        f"[q]uit [p]eak-reset", curses.color_pair(C_DIM))
    row += 2

    # ---- GPUs ----
    put(row, 0, "GPUs", curses.color_pair(C_LABEL) | bold)
    if vram_status:
        put(row, 6, vram_status, curses.color_pair(C_DIM))
    row += 1
    if not gpus:
        put(row, 2, "nvidia-smi unavailable", curses.color_pair(C_CRIT)); row += 1
    else:
        hdr = (f"{'#':<2} {'core':>5} {'vram':>5} {'fan':>5} {'util':>5} "
               f"{'memory':>15} {'power':>13}  bar")
        put(row, 2, hdr, curses.color_pair(C_DIM)); row += 1
        for g in gpus:
            x = 2
            put(row, x, f"{g['idx']:<2} "); x += 3
            tcol = curses.color_pair(color_for(g["temp"], GPU_TEMP)) | bold if g["temp"] is not None else 0
            put(row, x, f"{ival(g['temp'])+'C':>5}", tcol); x += 6
            vram = g.get("vram")
            vcol = (curses.color_pair(color_for(vram, VRAM_TEMP)) | bold
                    if vram is not None else curses.color_pair(C_DIM))
            put(row, x, f"{ival(vram)+'C':>5}", vcol); x += 6
            put(row, x, f"{ival(g['fan'])+'%':>5}",
                curses.color_pair(fan_color(g["fan"]))); x += 6
            put(row, x, f"{ival(g['util'])+'%':>5}"); x += 6
            mem = "-"
            if g["mem_used"] is not None and g["mem_total"] is not None:
                mem = f"{g['mem_used']/1024:.1f}/{g['mem_total']/1024:.0f}G"
            put(row, x, f"{mem:>15}"); x += 16
            pw = "-"
            if g["power"] is not None and g["power_lim"] is not None:
                pw = f"{g['power']:.0f}/{g['power_lim']:.0f}W"
            put(row, x, f"{pw:>13}"); x += 15
            # temp bar (30..90C mapped)
            put(row, x, tempbar(g["temp"], GPU_TEMP))
            row += 1
    row += 1

    # ---- Power ----
    pi = power_info or {}
    if pi.get("sys_w") is not None:
        put(row, 0, "Power", curses.color_pair(C_LABEL) | bold)
        parts = []
        if pi.get("gpu_w") is not None:
            parts.append(f"GPU {pi['gpu_w']:.0f}W")
        if pi.get("cpu_w") is not None:
            parts.append(f"CPU {pi['cpu_w']:.0f}W")
        note = "" if pi.get("cpu_w") is not None else "  (GPU only)"
        put(row, 8, f"{'   '.join(parts)}    total {pi['sys_w']:.0f}W{note}")
        row += 2

    # ---- CPU / system ----
    put(row, 0, "CPU / system", curses.color_pair(C_LABEL) | bold); row += 1
    if not sensors:
        put(row, 2, "sensors unavailable", curses.color_pair(C_CRIT)); row += 1
    else:
        col_w = 30
        ncols = max(1, w // col_w)
        for i, (name, v) in enumerate(sensors):
            cy = row + i // ncols
            cx = 2 + (i % ncols) * col_w
            thr = CPU_TEMP if name.startswith(("Tctl", "Tdie", "Tccd", "k10")) else GEN_TEMP
            put(cy, cx, f"{name:<18}")
            put(cy, cx + 18, f"{v:5.1f}C",
                curses.color_pair(color_for(v, thr)) | bold)
        row += (len(sensors) + ncols - 1) // ncols
    row += 1

    # ---- Peaks ----
    put(row, 0, "Session peaks", curses.color_pair(C_LABEL) | bold)
    put(row, 16, f"logging -> {os.path.relpath(logger.csv_path)}",
        curses.color_pair(C_DIM))
    row += 1
    items = list(peaks.items())
    col_w = 26
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
        show = f"{name:<13}{val:5.0f} @{when}"
        attr = curses.color_pair(color_for(val, thr)) if is_temp else 0
        put(cy, cx, show, attr)

    stdscr.refresh()


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


def tempbar(v, thr, width=20):
    if v is None:
        return ""
    lo, hi = 30, 90
    frac = max(0.0, min(1.0, (v - lo) / (hi - lo)))
    n = int(frac * width)
    return "[" + "#" * n + "-" * (width - n) + "]"


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


def run(stdscr, args):
    curses.curs_set(0)
    init_colors()
    stdscr.nodelay(True)
    start = time.time()
    start_str = time.strftime("%Y%m%d_%H%M%S")
    vram = None if args.no_vram else VramReader(args.gddr6_bin)
    power = PowerMeter()
    logger = Logger(args.logdir, start_str, vram=vram is not None)
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
            update_peaks(peaks, gpus, sensors, power_info)
            uptime = fmt_dur(time.time() - start)
            logger.write_cycle(time.strftime("%Y-%m-%d %H:%M:%S"), gpus, sensors,
                               power_info)
            logger.write_peaks(peaks, start_str, uptime)
            draw(stdscr, gpus, sensors, peaks, args.interval, logger,
                 start_str, uptime, vram_status, power_info)
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
    ap.add_argument("--gddr6-bin",
                    default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "gddr6", "build", "bin", "gddr6"),
                    help="gddr6 binary for GDDR6X VRAM temps; run via `sudo -n` "
                         "(default: bundled build)")
    ap.add_argument("--no-vram", action="store_true",
                    help="disable GDDR6X VRAM temp reading (skips sudo gddr6)")
    args = ap.parse_args()
    try:
        curses.wrapper(run, args)
    except KeyboardInterrupt:
        pass
    print(f"logs written to {args.logdir}/")


if __name__ == "__main__":
    main()
