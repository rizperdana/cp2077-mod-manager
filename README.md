# cp2077-mod-manager

Linux mod manager for Cyberpunk 2077, built for Wine/Heroic installs. PyQt6 GUI
plus a stdlib-only web dashboard (`monitor.py`) and a headless CLI.

## Features
- Install/uninstall mods from archives or folders, FOMOD support.
- Mod load ordering, framework-first install order.
- Atomic state persistence (`mods.json`) with `.bak` backup.
- `monitor.py`: health checks, framework/log status, untracked-content scan.
- Headless CLI: `list`, `verify`, `enable`, `disable`.

## Requirements
- Python 3.10+
- PyQt6 (GUI)
- Optional: py7zr, rarfile (archive support)

## Install
```
git clone <repo> && cd cp2077-mod-manager
./setup.sh
./run_mod_manager
```

## Monitor
```
python3 monitor.py --game <gamedir> --port 8765
python3 monitor.py --check
```
Screenshot placeholder: `docs/screenshot.png`

## CLI examples
```
python3 cp2077_mod_manager.py list --game '<gamedir>'
python3 cp2077_mod_manager.py verify --game '<gamedir>'
python3 cp2077_mod_manager.py disable 'Mod Name'
python3 cp2077_mod_manager.py enable 'Mod Name'
```

## Wine/Heroic notes
- Launch game with `-modded`.
- WINEDLLOVERRIDES: `version=n,b;winmm=n,b` for CET+RED4ext coexistence.

## Folder map
| Framework | Path |
|---|---|
| CET | bin/x64/plugins/cyber_engine_tweaks/mods |
| RED4ext | red4ext/plugins |
| redscript | r6/scripts |
| TweakXL | r6/tweaks |
| ArchiveXL | archive/pc/mod |
| Input Loader | r6/config + r6/cache + engine/config/platform/pc |

## Troubleshooting
- ACU address-hash crash: ensure address library matches game patch.
- RED4ext "incompatible with current patch / requested runtime": framework version mismatch.
- .disabled-suffix trap: disabled dirs must leave scan roots (moved to mods/_disabled).
- Disabled mods whose files are scanned by extension must be moved OUT of scan roots.

## Mod install order
RED4ext → redscript → CET → ArchiveXL → TweakXL → Codeware → Equipment-EX/Virtual Atelier → others.
