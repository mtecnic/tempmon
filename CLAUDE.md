# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`tempmon.py` is a single-file curses TUI that watches GPU/CPU temperatures, fans, and load during LLM inference. Its distinguishing purpose is **crash-proof logging**: every cycle is `flush()`ed and `os.fsync()`ed to disk so that if the box thermally crashes, you still have proof of how hot it got right before it died. Assume the machine it runs on can hard-lock — logging durability matters more than performance.

**This branch (`dgx-spark`) is adapted for waive4, an NVIDIA DGX Spark** (GB10 Grace Blackwell SoC, aarch64, 128 GB unified memory). Differences from the 4x RTX 3090 `master` branch:

- **CPU/SoC temps come straight from sysfs** (`/sys/class/hwmon`) — `lm-sensors` is not installed and not needed. On this box that yields 7 unlabeled `acpitz` SoC thermal zones, NVMe Composite/Sensor 1, and the mt7925 wifi radio (which reports an empty value while asleep — skipped, not an error).
- **GPU memory falls back to `/proc/meminfo`** — the GB10's CPU and GPU share one coherent 128 GB pool, so `nvidia-smi` reports `memory.used/total` as `[N/A]`; when every GPU reports N/A memory, `read_gpus()` fills used/total from `MemTotal`/`MemAvailable` instead. Fan speed, `power.limit`, and memory clock are also N/A on GB10 and simply render as `-` (power shows draw-only without a `/limit`).
- **The GDDR6X VRAM path is dormant** — unified LPDDR5X has no GDDR6X junction sensor and the gddr6 tool doesn't apply. A missing `gddr6` binary now *silently* disables VRAM temps (no warning, no CSV columns) instead of showing "gddr6 unavailable". The `gddr6/` clone, `VramReader`, `verify_vram.py`, and the `--vram` flags are kept intact so the branch can merge back cleanly.
- **No RAPL** — `/sys/class/powercap` doesn't exist on this ARM platform, so `PowerMeter` finds no packages and the Power line shows GPU only. Note the GB10 `power.draw` is module power for the whole SoC.
- **Thresholds retuned** for the GB10 (throttles ~90°C, vs ~83°C on the 3090) and its ARM clusters; `acpitz`-prefixed sensors are colored against `CPU_TEMP`.

## Run / develop

```bash
./tempmon.py                 # or: python3 tempmon.py
./tempmon.py -i 1            # refresh interval seconds (default 2)
./tempmon.py -d /path/logs   # log directory (default ./logs)
./tempmon.py --keep 20       # keep only N most recent sessions (default 0 = keep all)
./tempmon.py --no-vram       # skip GDDR6X VRAM temps (moot here: auto-disabled when
./tempmon.py --gddr6-bin P   #   the gddr6 binary is absent, as it is on this box)
```

In-TUI keys: `q`/`Ctrl-C` quit, `p` reset session peaks.

No build, no dependency file, no tests for `tempmon.py` itself. Pure stdlib Python 3 (`curses`, `subprocess`, `threading`, `re`). The only external runtime dep on this branch is the `nvidia-smi` binary; everything else is sysfs/procfs. Anything failing is caught and degrades to an "unavailable" line rather than crashing — preserve that: sampling functions must return `[]`/`None` (or `(None, None)`) on any error, never raise.

`verify_vram.py` is a headless end-to-end check for the VRAM path from the 3090 branch — not applicable on this hardware (no gddr6 binary), kept for merge cleanliness.

Quick headless smoke test of the samplers on this box:

```bash
python3 -c "import tempmon; print(tempmon.read_gpus()); print(tempmon.read_sensors()); print(tempmon.read_unified_mem())"
```

## Architecture

Single synchronous loop in `run()` (under `curses.wrapper`), each cycle: `read_gpus()` + `read_sensors()` → `update_peaks()` → `logger.write_cycle()` + `logger.write_peaks()` → `draw()`, then a busy-wait on `args.interval` that polls `getch()` every 50ms so keys stay responsive.

- **Sampling** — `read_gpus()` parses `nvidia-smi --query-gpu=... --format=csv,noheader,nounits` (column order fixed by `GPU_QUERY`, which includes `pci.bus_id` → parsed to an int by `pci_bus()`; the GB10's extended-domain `0000000F:01:00.0` form parses fine), then applies the unified-memory fallback via `read_unified_mem()`. `read_sensors()` walks `/sys/class/hwmon/hwmon*/temp*_input`, labeling each as `<chip name>:<temp*_label or tempN>` (e.g. `acpitz:temp3`, `nvme:Composite`). Both return lists of dicts/tuples; missing numeric values are `None`.
- **System power** — `gpu_power_total()` sums per-GPU `power.draw` (on GB10 this is whole-module power, ~11 W idle). `PowerMeter` (Intel-RAPL powercap) is retained but finds no packages on this ARM platform — `watts()` returns `None` and the Power line shows "GPU only". There is no wall-outlet power source.
- **VRAM temp** — dormant on this branch. `run()` only constructs a `VramReader` when the gddr6 binary exists on disk; otherwise no warning, no columns. The class itself is unchanged from `master` (persistent `sudo -n gddr6` subprocess, PCI-bus-keyed join, SIGPIPE cleanup) — see the `master` branch CLAUDE.md for the details if reviving it on GDDR6X hardware.
- **Logging** — `Logger` writes two files per session, named by start timestamp: `tempmon_<start>.csv` (wide, one row/cycle, header locked in on first cycle from whatever GPUs/sensors were present) and `tempmon_<start>_peak.txt` (rewritten each cycle). Both fsync every cycle by design.
- **Peaks** — `peaks` dict maps metric name → `(value, HH:MM:SS)`, only bumped on a new max; `p` clears it.
- **Rendering** — `draw()` is defensive: `put()` clips every write to the terminal bounds, so a small window truncates rather than errors. Color is threshold-driven via `color_for(val, thr)`.

## Conventions when editing

- **Thresholds** live as `(warn, hot, crit)` tuples at the top: `GPU_TEMP`, `CPU_TEMP`, `GEN_TEMP`, `VRAM_TEMP`, tuned on this branch for the GB10 (throttles ~90C) and its ARM Cortex clusters (`acpitz` zones idle ~40C). Adjust these rather than hardcoding numbers in `draw()`.
- Adding a logged metric means touching these in lockstep: the sampling dict, `Logger._columns`, and `Logger.write_cycle` (column count must match header), plus `draw()` and `update_peaks()` if it should show. CSV header is frozen after the first cycle, so a metric appearing only later won't get a column. The VRAM column is the worked example — it threads through all of these and is gated on `Logger.vram` so `--no-vram` runs omit the columns entirely.
- Keep `None` propagation intact — `fmt()`/`ival()` render `None` as empty/`-`; don't assume a sample is present.
