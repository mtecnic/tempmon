#!/usr/bin/env python3
"""End-to-end check for tempmon's GDDR6X VRAM temp integration.

Exercises the real live path (no curses): spawns the actual `sudo -n gddr6`
subprocess via VramReader, lets the reader thread populate temps, prints them
joined onto nvidia-smi's GPUs by PCI bus, then verifies clean shutdown (the
root gddr6 process must exit on its own when the pipe closes).

Run one of:
    sudo python3 verify_vram.py       # works with no sudoers rule
    python3 verify_vram.py            # needs the NOPASSWD /etc/sudoers.d rule
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tempmon

BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "gddr6", "build", "bin", "gddr6")


def main():
    gpus = tempmon.read_gpus()
    if not gpus:
        print("FAIL: nvidia-smi returned no GPUs")
        return 1
    print(f"nvidia-smi sees {len(gpus)} GPU(s)\n")

    if not os.path.exists(BIN):
        print(f"FAIL: gddr6 binary not found at {BIN}")
        return 1

    reader = tempmon.VramReader(BIN)
    if not reader.available:
        print("FAIL: could not start `sudo -n gddr6` (no root / no sudoers "
              "rule?). Re-run with `sudo python3 verify_vram.py`.")
        return 1

    # gddr6 emits Device lines immediately, then one temp line per second.
    print("waiting for first VRAM reading...", flush=True)
    deadline = time.time() + 8
    while time.time() < deadline:
        if reader.temps and all(reader.get(g["bus"]) is not None for g in gpus):
            break
        if not reader.available:
            print("FAIL: gddr6 subprocess died before producing readings")
            return 1
        time.sleep(0.2)

    print()
    print(f"{'idx':>3}  {'bus':>4}  {'name':<12}  {'core':>5}  {'vram':>5}")
    print("-" * 40)
    missing = []
    for g in gpus:
        vram = reader.get(g["bus"])
        if vram is None:
            missing.append(g["idx"])
        core = "-" if g["temp"] is None else f"{g['temp']:.0f}C"
        vs = "MISSING" if vram is None else f"{vram:.0f}C"
        print(f"{g['idx']:>3}  {g['bus']:#04x}  {g['name']:<12}  "
              f"{core:>5}  {vs:>5}")

    print()
    ok = True
    if missing:
        print(f"FAIL: no VRAM reading for GPU index(es) {missing} "
              f"(gddr6 detected buses {[hex(b) for b in reader.bus_order]})")
        ok = False
    else:
        print("PASS: every GPU got a VRAM reading, joined by PCI bus")

    # Shutdown: closing the pipe should SIGPIPE gddr6 and it should exit.
    reader.close()
    for _ in range(30):                 # up to ~3s (gddr6 writes once a second)
        if reader.proc.poll() is not None:
            break
        time.sleep(0.1)
    if reader.proc.poll() is None:
        print("WARN: gddr6 process still alive after close() -- a root process "
              "may be left behind. `sudo pkill -f gddr6/build/bin/gddr6`.")
        ok = False
    else:
        print("PASS: gddr6 subprocess exited cleanly on shutdown")

    print()
    print("ALL GOOD" if ok else "SOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
