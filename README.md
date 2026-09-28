<div align="center">

# tempmon

**Watch your inference box's temperatures — including the one `nvidia-smi` won't tell you.**

A single-file curses TUI for GPU/CPU temps, fans, clocks and power during LLM inference,
built to keep its logs intact when the machine doesn't.

[![Python](https://img.shields.io/badge/Python-3-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![stdlib only](https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen)](#requirements)
[![Platform](https://img.shields.io/badge/Platform-Linux-FCC624?logo=linux&logoColor=black)](#requirements)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

</div>

---

## Why

Two problems with watching a GPU box under sustained inference load.

**First: `nvidia-smi` cannot read VRAM temperature on GeForce cards.** `temperature.memory`
returns `N/A`. On a 3090 that's the metric that actually matters — the GDDR6X junction
throttles around **105 °C** while the core is sitting at a comfortable 70 °C. You can watch
`nvidia-smi` all day and never see the thing that's cooking the card. tempmon reports
**per-card GDDR6X junction temperature** alongside the core temp.

**Second: the interesting moment is the one where the box dies.** A thermal hard-lock takes
your terminal scrollback with it. tempmon `flush()`es *and* `os.fsync()`es every log line
every cycle, on the assumption that the machine it runs on can hard-lock at any instant —
so after a crash you still have proof of exactly how hot it got on the way down.
Durability is deliberately chosen over logging performance.

## What you see

```
GPU  NAME          CORE  ▁▂▃▅▆▇  VRAM   FAN   UTIL   SCLK    MCLK    MEMORY       POWER
─────────────────────────────────────────────────────────────────────────────────────────
[0]  RTX 3090       71°C  ██████   96°C   74%   99%   1830   9751   23.1/24.0G   338/350W
[1]  RTX 3090       68°C  █████▌   91°C   70%   98%   1815   9751   23.0/24.0G   331/350W
```

Core temp, **GDDR6X VRAM junction temp**, fan %, utilisation, graphics and memory clocks
(against each card's max), memory used, and power draw against limit — plus CPU package
temps from `lm-sensors`, NVMe temps, a system power line, and a session peak table with the
timestamp of each peak.

Colour thresholds are tuned per metric, with VRAM on its own scale
(`85 / 95 / 105 °C`) because 96 °C is unremarkable for a core and alarming for GDDR6X.

## Quick start

```bash
git clone https://github.com/mtecnic/tempmon.git
cd tempmon
./tempmon.py
```

That works immediately and gives you everything except VRAM temps. For those, build the
`gddr6` helper once — see [VRAM temperatures](#vram-temperatures-the-gddr6-dependency).

```bash
./tempmon.py                 # or: python3 tempmon.py
./tempmon.py -i 1            # refresh interval in seconds (default 2)
./tempmon.py -d /path/logs   # log directory (default ./logs)
./tempmon.py --keep 20       # keep only the N most recent sessions (default 0 = keep all)
./tempmon.py --no-vram       # skip GDDR6X VRAM temps entirely (no sudo needed)
./tempmon.py --gddr6-bin P   # path to the gddr6 binary
```

In the TUI: <kbd>q</kbd> quits, <kbd>p</kbd> resets the session peaks, <kbd>Ctrl-C</kbd>
also quits. Resizing the window is handled — output clips to the terminal rather than
erroring.

## Logs

Two files per session, named by start timestamp, both in the log directory:

| File | What's in it |
|---|---|
| `tempmon_<start>.csv` | One wide row per cycle — every GPU and sensor value, full timeline. The header is locked in on the first cycle from whatever hardware was present. |
| `tempmon_<start>_peak.txt` | Running peak of every metric with the time it happened. Rewritten each cycle. |

Both are `flush()` + `fsync()`ed every cycle. `--keep N` prunes all but the N most recent
sessions on startup.

## VRAM temperatures: the gddr6 dependency

GDDR6X junction temps come from **[olealgoritme/gddr6](https://github.com/olealgoritme/gddr6)**
— a C tool that mmaps the GPU's PCI BAR to read a register the driver doesn't expose. See
[Credits](#credits). It is **not** mine and it is **not** covered by this repo's licence.

Build it once:

```bash
cd gddr6 && ./build_install.sh     # needs cmake + libpci-dev
```

Answer **n** to its install prompt — tempmon uses the in-tree `gddr6/build/bin/gddr6`.
Upstream notes that some systems additionally need the `iomem=relaxed` kernel boot
parameter and Secure Boot disabled.

### The sudo caveat — read this one

`gddr6` requires root and has no one-shot mode; it loops forever, printing one
carriage-return-updated line per second. tempmon spawns it **once** as a persistent
subprocess and reads it on a daemon thread, which avoids a password prompt every cycle —
but it still needs to start it without an interactive prompt. That means a `NOPASSWD`
sudoers rule:

```
# /etc/sudoers.d/tempmon-gddr6
<your-user> ALL=(root) NOPASSWD: /absolute/path/to/tempmon/gddr6/build/bin/gddr6
```

**Understand what this grants.** Your user can rewrite that binary, so a NOPASSWD rule
pointing at a file you own is effectively passwordless root. That is an acceptable trade on
a single-user lab box and **not** acceptable on a shared or multi-user machine. Decide
accordingly — or just run with `--no-vram`, which needs no sudo at all.

Without the rule, `sudo -n` fails, the TUI shows `vram: gddr6 unavailable`, and everything
else runs normally.

`verify_vram.py` is a headless end-to-end check of this path — no curses, prints per-GPU
core vs VRAM temps and asserts a clean subprocess shutdown:

```bash
sudo python3 verify_vram.py
```

> One implementation detail worth knowing if you read the code: `gddr6` lists cards in
> **PCI-discovery order, which is not `nvidia-smi`'s index order**. tempmon maps temps onto
> the bus numbers parsed from gddr6's own `Device:` lines and joins them to each GPU by bus
> ID — never by row position. Joining by position mislabels every card on a box where the
> two orders differ, which is most of them.

## System power (optional)

The power line sums per-GPU `power.draw`, which needs no root and is exact. CPU **package**
power is read from the Intel-RAPL powercap framework (`energy_uj`), which backs AMD too
despite the name. Two honest limitations:

- `energy_uj` is root-only by default, so the display reads **"GPU only"** unless you relax
  it. `enable_cpu_power.sh` installs the udev rule that does:
  ```
  # /etc/udev/rules.d/99-rapl-readable.rules
  SUBSYSTEM=="powercap", ACTION=="add", MODE="0444"
  ```
- Many boards expose only `intel-rapl:0`. On a dual-socket machine that's **socket 0 only**
  — roughly half the real CPU draw.

There is no wall-outlet measurement here. No PSU sensor, no IPMI. The total is
"GPUs + whatever CPU packages are readable," and the UI says so rather than pretending
otherwise.

## Requirements

Pure Python 3 standard library — `curses`, `subprocess`, `json`, `threading`, `re`. No
`pip install`, no requirements file, no build step for tempmon itself.

What it shells out to:

| Binary | For | If missing |
|---|---|---|
| `nvidia-smi` | GPU temps, fans, clocks, memory, power | GPU section shows unavailable |
| `sensors` (lm-sensors) | CPU package + NVMe temps | sensor section shows unavailable |
| `gddr6` | GDDR6X VRAM junction temps | `vram: gddr6 unavailable` |

Every one of these failing is caught and degrades to an "unavailable" line. Sampling
functions return empty or `None` on any error — they never raise, because a monitor that
crashes when a sensor hiccups is worse than no monitor.

Developed and used on a 4× RTX 3090 dual-socket AMD box under sustained vLLM load.

## Supported GPUs

Core temps, fans, clocks and power work on **any** NVIDIA GPU `nvidia-smi` supports.

VRAM junction temps are limited to what upstream `gddr6` supports — RTX 3070/3080/3080 Ti/
3090/3090 Ti, RTX 4070 through 4090, RTX A2000/A4500/A5000/A6000, and A10/L4/L40S. Check
[upstream](https://github.com/olealgoritme/gddr6#supported-gpus) for the current list. On
anything else, run with `--no-vram`.

## Credits

- **[olealgoritme/gddr6](https://github.com/olealgoritme/gddr6)** — the GDDR6/GDDR6X VRAM
  temperature reader in `gddr6/`, based on reverse engineering of the NVIDIA Linux driver.
  All of the hard part of reading VRAM temps is their work, not mine. tempmon only parses
  its output. Upstream publishes no licence file, so that code carries no grant of rights
  from me and is **not** covered by this repo's MIT licence — see
  [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md), and contact the upstream author for terms.
- **[lm-sensors](https://github.com/lm-sensors/lm-sensors)** — CPU and NVMe temperatures via
  `sensors -j`.
- **NVIDIA** — `nvidia-smi`, which supplies everything else.

## License

[MIT](LICENSE), covering `tempmon.py`, `verify_vram.py` and the setup scripts. The vendored
`gddr6/` directory is third-party and is **not** MIT-licensed — see
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
