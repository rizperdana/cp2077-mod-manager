# cpmm

Single-file, stdlib-only Cyberpunk 2077 mod manager for Linux (Wine/Heroic
installs). Interactive TUI plus scriptable CLI. No pip dependencies.

## Requirements
- Python 3.10+
- Optional: `p7zip` (`7z`) and `unrar` binaries for .7z/.rar archives
  (zip needs nothing extra)

## Install
```
git clone https://github.com/rizperdana/cp2077-mod-manager.git
cd cp2077-mod-manager
./setup.sh
```

## Usage
```
python3 cpmm.py                 # interactive TUI (also: cpmm tui)
cpmm detect                     # find game dir, save to settings
cpmm set --game '<gamedir>'     # override game dir
cpmm list                       # all mods, grouped by category
cpmm list --category misc       # filter
cpmm meta 'CET 1.38.1'          # full record: files, sizes, status
cpmm install mod.zip --category gameplay   # zip / .7z / .rar / folder
cpmm uninstall 'Mod Name' -y    # -y skips confirm
cpmm disable 'Mod Name'         # moves files to <game>/mods/_disabled/
cpmm enable 'Mod Name'          # restore exact paths
cpmm category 'Mod Name' romance
cpmm check                      # frameworks, plugins, compile, integrity
```

CLI mode works fully headless; `check` exits 0 when healthy, 1 on problems.

## State
`~/.local/share/cp2077-mod-manager/mods.json` (atomic writes, `.bak` backup)
plus `settings.json` (game path, overwrite_files). Override the state dir for
tests with `CPMM_STATE_DIR`; override the game dir with `CP2077_GAME`.

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
