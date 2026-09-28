# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`tempmon.py` is a single-file curses TUI that watches GPU/CPU temperatures, fans, and load during LLM inference on a 4x RTX 3090 home lab. Its distinguishing purpose is **crash-proof logging**: every cycle is `flush()`ed and `os.fsync()`ed to disk so that if the box thermally crashes, you still have proof of how hot it got right before it died. Assume the machine it runs on can hard-lock — logging durability matters more than performance.

It also reports **GDDR6X VRAM junction temperature** per card (the metric that actually cooks these 3090s — throttles ~105°C — and which `nvidia-smi` cannot read on GeForce; `temperature.memory` returns N/A). VRAM temp comes from the bundled `gddr6/` tool ([olealgoritme/gddr6](https://github.com/olealgoritme/gddr6)), a C binary that mmaps the GPU BAR and needs root.

## Run / develop

```bash
./tempmon.py                 # or: python3 tempmon.py
./tempmon.py -i 1            # refresh interval seconds (default 2)
./tempmon.py -d /path/logs   # log directory (default ./logs)
./tempmon.py --keep 20       # keep only N most recent sessions (default 0 = keep all)
./tempmon.py --no-vram       # skip GDDR6X VRAM temps (no sudo gddr6)
./tempmon.py --gddr6-bin P   # path to gddr6 binary (default gddr6/build/bin/gddr6)
```

In-TUI keys: `q`/`Ctrl-C` quit, `p` reset session peaks.

No build, no dependency file, no tests for `tempmon.py` itself. Pure stdlib Python 3 (`curses`, `subprocess`, `json`, `threading`, `re`). External runtime deps are the **binaries** it shells out to: `nvidia-smi`, `sensors` (lm-sensors), and `gddr6` (VRAM temp). Every one of these failing is caught and degrades to an "unavailable" line rather than crashing — preserve that: sampling functions must return `[]`/`None` on any error, never raise.

`verify_vram.py` is a headless end-to-end check for the VRAM path (run `sudo python3 verify_vram.py`) — no curses, prints per-GPU core vs VRAM temps and asserts clean subprocess shutdown.

### The gddr6 VRAM dependency

`gddr6/` is a git clone of olealgoritme/gddr6. Build once: `cd gddr6 && ./build_install.sh` (needs `cmake` + `libpci-dev`; answer "n" to the install prompt — tempmon uses the in-tree `build/bin/gddr6`). The binary **requires root** and has no one-shot mode (it loops forever, printing one `\r`-updated `VRAM Temps:` line per second). To avoid a password prompt every cycle, a NOPASSWD sudoers rule is expected:

```
# /etc/sudoers.d/tempmon-gddr6
waive5 ALL=(root) NOPASSWD: /home/waive5/tempmon/gddr6/build/bin/gddr6
```

Note this is effectively root-equivalent for the user (they can rewrite the binary) — acceptable on a single-user box only. If the rule is absent, `sudo -n` fails, `VramReader.available` goes False, and the TUI shows "vram: gddr6 unavailable" but otherwise runs normally.

### Unlocking CPU package power (optional)

`energy_uj` is root-only by default. To let tempmon read it without root, relax the powercap perms with a persistent udev rule:

```
# /etc/udev/rules.d/99-rapl-readable.rules
SUBSYSTEM=="powercap", ACTION=="add", MODE="0444"
```

Then `sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=powercap`. Without it, the Power line shows "GPU only" — no crash.

## Architecture

Single synchronous loop in `run()` (under `curses.wrapper`), each cycle: `read_gpus()` + `read_sensors()` → `update_peaks()` → `logger.write_cycle()` + `logger.write_peaks()` → `draw()`, then a busy-wait on `args.interval` that polls `getch()` every 50ms so keys stay responsive.

- **Sampling** — `read_gpus()` parses `nvidia-smi --query-gpu=... --format=csv,noheader,nounits` (column order fixed by `GPU_QUERY`, which includes `pci.bus_id` → parsed to an int by `pci_bus()`); `read_sensors()` parses `sensors -j` JSON once and returns **`(temps, fans)`** — `temp*_input` in degrees C and `fan*_input` in RPM, split because lm-sensors gives every reading the same `*_input` suffix regardless of unit (voltages and power land there too, and are dropped). Temps outside `TEMP_SANE` are dropped (unconnected thermistors report -55/-128); fans are returned for **every** channel including 0 RPM ones, so the frozen CSV header keeps matching row-for-row. Chips are tagged `k10temp/cb` so dual-socket instances stay distinct. Both return lists of dicts/tuples; missing numeric values are `None`.
- **System power** — `gpu_power_total()` sums per-GPU `power.draw` (exact, no root). `PowerMeter` adds CPU **package** power from Intel-RAPL-framework energy counters (`/sys/class/powercap/intel-rapl:N/energy_uj`; the framework backs AMD too — this box is dual-socket AMD despite the `intel-rapl` name) as energy-delta/time with per-package wrap handling. Two caveats: `energy_uj` is usually **root-only**, so `watts()` returns `None` and the UI shows "GPU only" unless a udev rule relaxes it (see below); and this board exposes only `intel-rapl:0`, so CPU power reflects **socket 0 only** (~half of a dual-socket draw). There is no true wall-outlet power source (no PSU sensor / IPMI). `draw()` shows a `Power` line (GPU + CPU + total); peak system power is tracked.
- **VRAM temp** — `VramReader` spawns `sudo -n gddr6` **once** as a persistent subprocess and reads it in a daemon thread, exposing the latest temp per PCI bus via `get(bus)`. Key detail: **gddr6 lists cards in PCI-discovery order, not nvidia-smi's index order**, so temps are mapped positionally onto the bus numbers parsed from gddr6's `Device:` lines, then joined to each GPU by `g["bus"]` — never by row position (that would mislabel every card; the two orders genuinely differ on this box). gddr6 delimits updates with a leading `\r` (not `\n`), so the reader treats CR/LF alike and parses complete segments. Cleanup relies on SIGPIPE: `close()` shuts the pipe, gddr6 dies on its next write (it catches SIGTERM but not SIGPIPE, so SIGTERM alone won't stop it).
- **Logging** — `Logger` writes two files per session, named by start timestamp: `tempmon_<start>.csv` (wide, one row/cycle, header locked in on first cycle from whatever GPUs/sensors were present) and `tempmon_<start>_peak.txt` (rewritten each cycle). Both fsync every cycle by design.
- **Peaks** — `peaks` dict maps metric name → `(value, HH:MM:SS)`, only bumped on a new max; `p` clears it. Fans are bumped as `rpm or None`, so a header that never spins stays out of the panel while a fan that spun and then stopped keeps its peak on record.
- **Rendering** — the `Fans` section lists only channels currently turning, with the rest as a trailing "N idle" count: an unpopulated header and a dead fan both read 0 RPM and a single sample can't tell them apart, so the CSV keeps all of them and only the display filters. `draw()` is defensive: `put()` clips every write to the terminal bounds, so a small window truncates rather than errors. Color is threshold-driven via `color_for(val, thr)`.

## Conventions when editing

- **Thresholds** live as `(warn, hot, crit)` tuples at the top: `GPU_TEMP`, `CPU_TEMP`, `GEN_TEMP`, `VRAM_TEMP` (GDDR6X, throttles ~105C), tuned for RTX 3090 (core throttle ~83C) and AMD EPYC/Threadripper (Tctl runs hot by design). Adjust these rather than hardcoding numbers in `draw()`.
- Adding a logged metric means touching these in lockstep: the sampling dict, `Logger._columns`, and `Logger.write_cycle` (column count must match header), plus `draw()` and `update_peaks()` if it should show. CSV header is frozen after the first cycle, so a metric appearing only later won't get a column. The VRAM column is the worked example — it threads through all of these and is gated on `Logger.vram` so `--no-vram` runs omit the columns entirely.
- Keep `None` propagation intact — `fmt()`/`ival()` render `None` as empty/`-`; don't assume a sample is present.
