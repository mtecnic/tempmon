# Third-party notices

This repository's [MIT licence](LICENSE) covers `tempmon.py`, `verify_vram.py`,
`enable_cpu_power.sh` and `setup_gddr6_sudo.sh`. It does **not** cover the vendored
third-party code described below.

## gddr6/ — olealgoritme/gddr6

- **Upstream:** https://github.com/olealgoritme/gddr6
- **Author:** olealgoritme
- **What it is:** a C library and CLI that reads GDDR6/GDDR6X VRAM junction temperatures by
  mmapping the GPU's PCI BAR, based on reverse engineering of the NVIDIA Linux driver.
- **How it's used here:** the `gddr6/` directory is a copy of that project. tempmon builds
  it and parses its stdout; none of tempmon's own code reimplements any of it. Every VRAM
  temperature this tool reports is the product of upstream's work.
- **Licence:** ⚠️ **Upstream publishes no licence file.** In the absence of an explicit
  licence, the work is under exclusive copyright by default — no permission to use, copy,
  modify or redistribute is granted. Its inclusion here therefore carries **no grant of
  rights from this repository's author**, and this repository's MIT licence does not and
  cannot extend to it.

  If you intend to use, redistribute or repackage that code, contact the upstream author
  for terms. If you only want to run tempmon, prefer building from
  [upstream](https://github.com/olealgoritme/gddr6) directly, or run `tempmon --no-vram`
  and skip it entirely.

  Upstream's own README additionally documents the `iomem=relaxed` kernel parameter and
  Secure Boot requirements, and is the authoritative source for its supported-GPU list.

## Runtime tools invoked as subprocesses

These are not bundled or redistributed here — tempmon only shells out to them if they are
already installed on the system.

| Tool | Provider | Used for |
|---|---|---|
| `nvidia-smi` | NVIDIA Corporation | GPU temps, fan speeds, clocks, memory, power draw |
| `sensors` | [lm-sensors](https://github.com/lm-sensors/lm-sensors) (LGPL-2.1) | CPU package and NVMe temperatures via `sensors -j` |
