#!/usr/bin/env bash
#
# setup_gddr6_sudo.sh -- install the NOPASSWD sudoers rule tempmon needs to read
# GDDR6X VRAM temps.
#
# tempmon runs the bundled gddr6 binary as `sudo -n` (non-interactive: it never
# prompts). Without a passwordless rule, sudo fails and the TUI shows
# "vram: gddr6 unavailable" with every VRAM cell as "-". This script (re)creates
# that rule at /etc/sudoers.d/tempmon-gddr6.
#
# Safe to re-run. The generated rule is syntax-checked with `visudo -c` BEFORE it
# is installed, so a mistake can never leave you locked out of sudo.
#
# Usage:
#   ./setup_gddr6_sudo.sh                # authorize the current user + bundled gddr6
#   ./setup_gddr6_sudo.sh -u alice       # authorize a different user
#   ./setup_gddr6_sudo.sh -b /path/gddr6 # authorize a non-default binary path
#   ./setup_gddr6_sudo.sh -h             # this help

set -euo pipefail

RULE_FILE=/etc/sudoers.d/tempmon-gddr6
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
TARGET_USER="$(id -un)"      # capture the real user before any sudo
GDDR6_BIN=""

usage() {
    sed -n '2,/^set /{/^set /d;s/^# \{0,1\}//;p}' "${BASH_SOURCE[0]}"
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        -u|--user) TARGET_USER="${2:?}"; shift 2 ;;
        -b|--bin)  GDDR6_BIN="${2:?}";   shift 2 ;;
        -h|--help) usage 0 ;;
        *) echo "error: unknown argument: $1" >&2; usage 1 ;;
    esac
done

# Default binary path = exactly what tempmon.py's --gddr6-bin default resolves to.
# Compute it with tempmon's own logic so the authorized path is byte-for-byte the
# command tempmon invokes (sudo matches the rule on the exact path).
if [ -z "$GDDR6_BIN" ]; then
    if [ -f "$SCRIPT_DIR/tempmon.py" ]; then
        GDDR6_BIN="$(python3 -c 'import os,sys; print(os.path.join(os.path.dirname(os.path.abspath(sys.argv[1])),"gddr6","build","bin","gddr6"))' "$SCRIPT_DIR/tempmon.py")"
    else
        GDDR6_BIN="$SCRIPT_DIR/gddr6/build/bin/gddr6"
    fi
fi

# --- preflight -------------------------------------------------------------
if ! id -u "$TARGET_USER" >/dev/null 2>&1; then
    echo "error: user '$TARGET_USER' does not exist" >&2
    exit 1
fi
if [ ! -x "$GDDR6_BIN" ]; then
    echo "error: gddr6 binary missing or not executable:" >&2
    echo "         $GDDR6_BIN" >&2
    echo "  build it first:  cd \"$SCRIPT_DIR/gddr6\" && ./build_install.sh" >&2
    echo "  (needs cmake + libpci-dev; answer 'n' to the install prompt)" >&2
    exit 1
fi

RULE="$TARGET_USER ALL=(root) NOPASSWD: $GDDR6_BIN"

echo "About to authorize passwordless sudo for:"
echo "  user   : $TARGET_USER"
echo "  binary : $GDDR6_BIN"
echo "  file   : $RULE_FILE"
[ -e "$RULE_FILE" ] && echo "  (replacing existing rule)"
echo

# --- validate, then install (all privileged steps go through sudo) ---------
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
printf '# installed by setup_gddr6_sudo.sh -- lets tempmon read GDDR6X VRAM temps.\n# Root-equivalent for %s (they can rewrite the binary); single-user boxes only.\n%s\n' \
    "$TARGET_USER" "$RULE" > "$TMP"

echo "Validating sudoers syntax (may prompt for your password)..."
if ! sudo visudo -c -f "$TMP" >/dev/null; then
    echo "error: visudo rejected the generated rule -- NOT installing" >&2
    exit 1
fi

sudo install -m 0440 -o root -g root "$TMP" "$RULE_FILE"
echo "installed $RULE_FILE"
echo

# --- verify the fix --------------------------------------------------------
# Drop any cached sudo credentials so this genuinely tests the NOPASSWD rule
# and not a still-warm timestamp from the install step above.
sudo -k
echo "Verifying passwordless authorization..."
if ! sudo -n -l "$GDDR6_BIN" >/dev/null 2>&1; then
    echo "error: 'sudo -n $GDDR6_BIN' is still not permitted without a password." >&2
    echo "       Check that $TARGET_USER matches who runs tempmon and the path is exact." >&2
    exit 1
fi
echo "  OK: $TARGET_USER may run gddr6 via 'sudo -n' without a password."
echo
echo "Done. Start tempmon -- the header should now read 'vram via gddr6'."
echo
echo "To eyeball the raw sensor yourself (Ctrl-C to stop -- gddr6 loops forever):"
echo "  sudo -n $GDDR6_BIN"
