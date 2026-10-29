#!/bin/bash
# cpmm setup: stdlib-only, nothing to install.
set -e
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

command -v python3 >/dev/null || { echo "ERROR: python3 not found (need 3.10+)."; exit 1; }

python3 -m py_compile cpmm.py && echo "cpmm.py OK"
python3 -m unittest tests.test_cli

echo
echo "Optional: install p7zip and unrar for .7z/.rar archives (zip needs nothing):"
echo "  sudo pacman -S p7zip unrar   # Arch/CachyOS"
echo
echo "Run:  python3 cpmm.py        (TUI)   or   cpmm detect && cpmm list"
