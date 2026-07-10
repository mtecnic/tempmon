#!/usr/bin/env bash
# Enable tempmon's CPU package power reading by making Intel-RAPL-framework
# energy counters readable without root, then verify it worked end to end.
#
# RAPL energy_uj is root-only by default (a side-channel mitigation). We install
# a persistent udev rule that sets the powercap attributes to mode 0444, reload
# udev, then confirm the invoking (non-root) user can read the counter and that
# PowerMeter reports a sane wattage.
#
# Run:  sudo ./enable_cpu_power.sh
set -u

RULE=/etc/udev/rules.d/99-rapl-readable.rules
HERE="$(cd "$(dirname "$0")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
    echo "This script must run as root (it writes $RULE)."
    echo "Re-running with sudo..."
    exec sudo "$0" "$@"
fi

# The unprivileged user to test readability as (the human who ran sudo).
TARGET_USER="${SUDO_USER:-}"

echo "==> Installing udev rule: $RULE"
cat > "$RULE" <<'EOF'
# Make Intel-RAPL-framework energy counters world-readable so non-root tools
# (tempmon) can compute CPU package power. Single-user host only.
SUBSYSTEM=="powercap", ACTION=="add", MODE="0444"
EOF
chmod 0644 "$RULE"

echo "==> Reloading udev and re-triggering powercap devices"
udevadm control --reload
udevadm trigger --subsystem-match=powercap
udevadm settle

PKG=/sys/class/powercap/intel-rapl:0
ENERGY="$PKG/energy_uj"
if [ ! -e "$ENERGY" ]; then
    echo "FAIL: $ENERGY not found -- no RAPL package domain on this host."
    exit 1
fi

echo
echo "==> Permissions now:"
ls -l "$ENERGY"

echo
echo "==> Read test as unprivileged user"
if [ -n "$TARGET_USER" ]; then
    if sudo -u "$TARGET_USER" cat "$ENERGY" >/dev/null 2>&1; then
        echo "PASS: user '$TARGET_USER' can read energy_uj (no root needed)"
    else
        echo "FAIL: user '$TARGET_USER' still cannot read energy_uj."
        echo "      A reboot may be required for the rule to fully apply."
        exit 1
    fi
else
    # Run directly as root; fall back to checking the mode bits.
    if [ -r "$ENERGY" ] && [ "$(stat -c '%a' "$ENERGY")" = "444" ]; then
        echo "PASS: energy_uj is mode 0444 (world-readable)"
    else
        echo "WARN: could not confirm world-readable; re-run as: sudo ./enable_cpu_power.sh"
    fi
fi

echo
echo "==> Live CPU package power via tempmon.PowerMeter (2 samples, ~1s apart)"
RUN_AS="${TARGET_USER:-root}"
sudo -u "$RUN_AS" python3 - "$HERE" <<'PY'
import sys, time
sys.path.insert(0, sys.argv[1])
import tempmon
pm = tempmon.PowerMeter()
if not pm.pkgs:
    print("FAIL: PowerMeter found no RAPL package domains"); sys.exit(1)
pm.watts()          # prime (needs two samples)
time.sleep(1.0)
w = pm.watts()
if w is None:
    print("FAIL: still unreadable as this user (reboot may be needed)"); sys.exit(1)
print(f"PASS: CPU package power = {w:.1f} W  "
      f"(socket 0 only on dual-socket boards)")
PY
rc=$?

echo
if [ $rc -eq 0 ]; then
    echo "ALL GOOD -- tempmon's Power line will now show CPU watts. Launch: ./tempmon.py"
else
    echo "CPU power not yet readable; a reboot usually applies the udev rule fully."
fi
exit $rc
