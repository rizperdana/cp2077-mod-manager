#!/usr/bin/env python3
from __future__ import annotations
import sys, os, json, shutil, tempfile, time, hashlib, zipfile
import xml.etree.ElementTree as ET
from enum import Enum
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime
# --- DZ---
# --- External libs ---
HAS_PY7ZR = False
HAS_RARFILE = False

try:
    import py7zr
    HAS_PY7ZR = True
except ImportError as e:
    print("Warning: py7zr not available, .7z support disabled:", e)

try:
    import rarfile
    HAS_RARFILE = True
except ImportError as e:
    print("Warning: rarfile not available, .rar support disabled:", e)

# --- PyQt6 ---
try:
    from PyQt6 import QtCore, QtGui, QtWidgets
    from PyQt6.QtCore import Qt
except ImportError as e:
    print("\n" + "="*60)
    print("ERROR: PyQt6 not found!")
    print("="*60)
    print("\nPlease install PyQt6 using one of these methods:\n")
    print("  Fedora/RHEL:  sudo dnf install python3-pyqt6")
    print("  Ubuntu/Debian: sudo apt install python3-pyqt6")
    print("  Arch:         sudo pacman -S python-pyqt6")
    print("  pip:          pip install PyQt6")
    print("\n" + "="*60 + "\n")
    sys.exit(1)

# --- Constants ---
APP_NAME = "cp2077-mod-manager"
STATE_DIR = Path.home() / ".local" / "share" / APP_NAME
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / "mods.json"
SETTINGS_FILE = STATE_DIR / "settings.json"

# Main game folders
MAIN_GAME_FOLDERS = ["archive", "bin", "engine", "r6", "red4ext", "mods"]

# Subfolder patterns for when no main folders detected
SUBFOLDER_PATTERNS = [
    {"path": "pc/mod", "type": "Archive", "dest": "archive/pc/mod"},
    {"path": "x64/plugins/cyber_engine_tweaks/mods", "type": "CET", "dest": "bin/x64/plugins/cyber_engine_tweaks/mods"},
    {"path": "plugins/cyber_engine_tweaks/mods", "type": "CET", "dest": "bin/x64/plugins/cyber_engine_tweaks/mods"},
    {"path": "scripts", "type": "REDscript", "dest": "r6/scripts"},
    {"path": "tweaks", "type": "TweakXL", "dest": "r6/tweaks"},
    {"path": "config", "type": "Config", "dest": "r6/config"},
    {"path": "plugins", "type": "RED4ext", "dest": "red4ext/plugins"},
]

COMMON_GAME_DIRS = [
    Path.home() / ".steam" / "steam" / "steamapps" / "common" / "Cyberpunk 2077",
    Path.home() / ".local" / "share" / "Steam" / "steamapps" / "common" / "Cyberpunk 2077",
    Path.home() / ".var" / "app" / "com.valvesoftware.Steam" / "data" / "Steam" / "steamapps" / "common" / "Cyberpunk 2077",
]

# Folder names that are documentation/extras rather than installable content.
# Used only to decide whether a lone wrapper folder can be safely descended
# into -- never to decide what gets installed.
IGNORABLE_SIBLING_DIRS = {
    "docs", "doc", "documentation", "screenshots", "screenshot",
    "images", "img", "preview", "previews", "extras", "extra",
    "readme", "__macosx", "optional files", "optionals",
}


def _unwrap_mod_wrapper(folder: Path, max_depth: int = 10) -> Path:
    """Descend past mod-named wrapper folders to the real mod root.

    Stops as soon as a level contains a recognised game folder (archive/,
    bin/, r6/, red4ext/, ...) -- that level IS the mod root.

    Otherwise, if the level holds exactly one meaningful subfolder (ignoring
    docs/screenshots and any loose files), that folder is the wrapper and we
    descend into it. This is what stops "Cool Mod Name/" from being copied
    into the game directory verbatim.
    """
    current = Path(folder)
    for _ in range(max_depth):
        try:
            entries = list(current.iterdir())
        except OSError:
            return current

        # A recognised game folder here means we have arrived.
        if any((current / name).exists() for name in MAIN_GAME_FOLDERS):
            return current

        subdirs = [c for c in entries if c.is_dir()]
        meaningful = [d for d in subdirs if d.name.lower() not in IGNORABLE_SIBLING_DIRS]

        if len(meaningful) == 1:
            current = meaningful[0]
            continue

        return current

    return current


# --- Data ---
@dataclass
class ModRecord:
    name: str
    mod_id: str
    mod_type: str
    installed_at: float
    order: int = 0
    files: list[str] = field(default_factory=list)
    archive_path: str = ""

def generate_mod_id(folder: Path) -> str:
    names = [f.name.lower() for f in folder.rglob("*") if f.is_file()]
    names.sort()
    return hashlib.sha256("".join(names).encode("utf-8")).hexdigest()[:12]

def load_state() -> dict[str, ModRecord]:
    """Load installed-mod records, tolerating a corrupt file and bad records."""
    def _parse(path: Path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    raw = None
    if STATE_FILE.exists():
        raw = _parse(STATE_FILE)
    if raw is None and (STATE_DIR / "mods.json.bak").exists():
        raw = _parse(STATE_DIR / "mods.json.bak")
        if raw is not None:
            print("Warning: mods.json corrupt; restored from mods.json.bak")
    if not isinstance(raw, dict):
        return {}
    mods = {}
    for k, v in raw.items():
        if not isinstance(v, dict):
            print(f"Warning: skipping bad record {k!r}: not an object")
            continue
        try:
            valid_fields = {"name", "mod_id", "mod_type", "installed_at", "order", "files", "archive_path"}
            filtered_v = {key: val for key, val in v.items() if key in valid_fields}
            rec = ModRecord(**filtered_v)
            if not isinstance(rec.name, str) or not rec.name:
                raise ValueError("name missing")
            if not isinstance(rec.files, list) or not all(isinstance(x, str) for x in rec.files):
                raise ValueError("files must be list[str]")
            if rec.order is None:
                rec.order = 0
            mods[k] = rec
        except (TypeError, ValueError, KeyError) as e:
            print(f"Warning: skipping bad record {k!r}: {e}")
    return mods

def save_state(state: dict[str, ModRecord]):
    # Back up previous file first (real incident: crashed run overwrote mods.json).
    try:
        if STATE_FILE.exists():
            shutil.copy2(STATE_FILE, STATE_DIR / "mods.json.bak")
    except OSError as e:
        print(f"Warning: could not back up mods.json: {e}")
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({k: v.__dict__ for k, v in state.items()}, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)

def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    return {
        "game_path": "",
        "mod_library": str(Path.home() / "Documents" / "CP2077_Mods"),
        # Empty by default: monitoring a folder MOVES completed downloads
        # out of it into the mod library, so defaulting to ~/Downloads would
        # silently relocate every file a user downloads. Opt-in only -- the
        # user must set this folder before any monitoring happens.
        "downloads_folder": "",
        "overwrite_files": False,
        "archive_folder": ""
    }

def save_settings(settings: dict):
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")

def detect_mod_type(folder: Path) -> tuple[str, Path, Path, str]:
    """Returns (mod_type, source_root, dest_path_relative, reason)"""

    print(f"\n[DEBUG] Detecting mod type for: {folder}")

    # Check for main game folders
    found_main_folders = []
    for main_folder in MAIN_GAME_FOLDERS:
        if (folder / main_folder).exists():
            found_main_folders.append(main_folder)
            print(f"[DEBUG] Found main folder: {main_folder}")

    # If we have main folders, install from root to game root
    if found_main_folders:
        if "red4ext" in found_main_folders:
            mod_type = "RED4ext"
        elif "bin" in found_main_folders:
            mod_type = "CET"
        elif "r6" in found_main_folders:
            mod_type = "REDscript/TweakXL"
        elif "archive" in found_main_folders:
            mod_type = "Archive"
        elif "mods" in found_main_folders:
            mod_type = "REDmod"
        elif "engine" in found_main_folders:
            mod_type = "Engine"
        else:
            mod_type = "Multi-Component"

        reason = f"Found main folders: {', '.join(found_main_folders)}"
        print(f"[DEBUG] Mod type: {mod_type}, installing from root to game root")
        print(f"[DEBUG] Reason: {reason}")
        return (mod_type, folder, Path("."), reason)

    # No main folders - check subfolder patterns
    print("[DEBUG] No main folders found, checking subfolder patterns...")

    for pattern in SUBFOLDER_PATTERNS:
        pattern_path = folder / pattern["path"]
        if pattern_path.exists():
            reason = f"Found subfolder: {pattern['path']}"
            print(f"[DEBUG] Matched pattern: {pattern['path']} -> {pattern['dest']}")
            print(f"[DEBUG] Reason: {reason}")
            return (pattern["type"], folder, Path(pattern["dest"]), reason)

    # Check file extensions as last resort
    print("[DEBUG] No subfolder patterns matched, checking file extensions...")

    if list(folder.rglob("*.lua")):
        reason = "Found .lua files"
        print(f"[DEBUG] {reason} -> CET")
        return ("CET", folder, Path("bin/x64/plugins/cyber_engine_tweaks/mods"), reason)

    if list(folder.rglob("*.reds")):
        reason = "Found .reds files"
        print(f"[DEBUG] {reason} -> REDscript")
        return ("REDscript", folder, Path("r6/scripts"), reason)

    if list(folder.rglob("*.xl")):
        reason = "Found .xl files"
        print(f"[DEBUG] {reason} -> TweakXL")
        return ("TweakXL", folder, Path("r6/tweaks"), reason)

    if list(folder.rglob("*.tweak")):
        reason = "Found .tweak files"
        print(f"[DEBUG] {reason} -> TweakXL")
        return ("TweakXL", folder, Path("r6/tweaks"), reason)

    if list(folder.rglob("*.archive")):
        reason = "Found .archive files"
        print(f"[DEBUG] {reason} -> Archive")
        return ("Archive", folder, Path("archive/pc/mod"), reason)

    # Unknown
    reason = "No recognizable mod structure or file types found"
    print(f"[DEBUG] {reason}")
    return ("Unknown", folder, Path("archive/pc/mod"), reason)


# ============================================================================
# FRAMEWORK / DEPENDENCY DETECTION AND ORDERING
# ----------------------------------------------------------------------------
# A handful of mods are frameworks that other mods depend on. They must be
# installed BEFORE regular mods, and in dependency order, because a mod that
# needs (say) ArchiveXL won't work if ArchiveXL isn't already present.
#
# Each framework is identified by a distinctive file it installs -- the
# loader DLL, the compiler exe, or the plugin's own folder. That signature
# is reliable enough to recognise a framework from its extracted contents
# regardless of how the archive was named.
#
# Order matters: RED4ext is the loader everything else rides on; RedScript
# and CET are the two script runtimes; ArchiveXL/TweakXL/Codeware are
# RED4ext plugins that mods commonly depend on. This is the community's
# standard install order.
# ============================================================================

# (display_name, [signature checks]) in required install order. Each check is
# a relative path; if ANY of a framework's signature paths is present in the
# extracted mod, it's identified as that framework. Paths are matched
# case-insensitively because archives vary, but nothing here affects where
# files install -- it's detection only.
FRAMEWORK_ORDER = [
    ("RED4ext", [
        "red4ext/RED4ext.dll",
        "bin/x64/winmm.dll",
    ]),
    ("RedScript", [
        "engine/tools/scc.exe",
        "engine/config/base/scripts.ini",
    ]),
    ("Cyber Engine Tweaks", [
        "bin/x64/plugins/cyber_engine_tweaks.asi",
        "bin/x64/plugins/cyber_engine_tweaks",
    ]),
    ("ArchiveXL", [
        "red4ext/plugins/ArchiveXL/ArchiveXL.dll",
        "red4ext/plugins/ArchiveXL",
    ]),
    ("TweakXL", [
        "red4ext/plugins/TweakXL/TweakXL.dll",
        "red4ext/plugins/TweakXL",
    ]),
    ("Codeware", [
        "red4ext/plugins/Codeware/Codeware.dll",
        "red4ext/plugins/Codeware",
    ]),
    # Equipment-EX and Virtual Atelier are higher-level frameworks that build
    # on all of the above, so they install last of the frameworks -- after
    # RED4ext/RedScript/CET and the three plugin loaders they depend on, but
    # still before ordinary mods. Both are identified by the named script
    # folder they ship under r6/scripts/ (plus, for Equipment-EX, its
    # signature .archive).
    ("Equipment-EX", [
        "r6/scripts/EquipmentEx",
        "archive/pc/mod/EquipmentEx.archive",
    ]),
    ("Virtual Atelier", [
        "r6/scripts/virtual-atelier",
    ]),
]

# Fast lookup of a framework's rank (0 = install first).
FRAMEWORK_RANK = {name: i for i, (name, _sigs) in enumerate(FRAMEWORK_ORDER)}


def _path_exists_ci(root: Path, rel: str) -> bool:
    """Does `rel` exist under `root`, matching each path segment
    case-insensitively? Archives are inconsistent about casing, and this is
    detection only -- it never decides where anything installs."""
    current = root
    for segment in rel.replace("\\", "/").split("/"):
        if not segment:
            continue
        try:
            entries = list(current.iterdir())
        except OSError:
            return False
        match = None
        for e in entries:
            if e.name.lower() == segment.lower():
                match = e
                break
        if match is None:
            return False
        current = match
    return True


def detect_framework(folder: Path):
    """Return the framework name if `folder` is one of the known frameworks,
    else None. Identified by the distinctive files each framework installs.

    A CET framework install ships the loader (cyber_engine_tweaks.asi / the
    framework folder with its dll and fonts); a CET *mod* only ships files
    under .../cyber_engine_tweaks/mods/. The signature checks target the
    framework files specifically, so a CET mod is NOT misread as the
    framework itself.
    """
    for name, signatures in FRAMEWORK_ORDER:
        for sig in signatures:
            # CET's framework folder check needs care: the 'mods' subfolder
            # alone (a CET mod) must not count as the framework. Only treat
            # the cyber_engine_tweaks folder as the framework if it contains
            # framework files, not just a mods/ dir.
            if name == "Cyber Engine Tweaks" and sig.endswith("cyber_engine_tweaks"):
                cet_dir = _resolve_ci_dir(folder, sig)
                if cet_dir is not None and _cet_dir_is_framework(cet_dir):
                    return name
                continue
            if _path_exists_ci(folder, sig):
                return name
    return None


def _resolve_ci_dir(root: Path, rel: str):
    """Resolve `rel` under `root` case-insensitively, returning the Path if it
    exists as a directory, else None."""
    current = root
    for segment in rel.replace("\\", "/").split("/"):
        if not segment:
            continue
        try:
            entries = list(current.iterdir())
        except OSError:
            return None
        match = next((e for e in entries if e.name.lower() == segment.lower()), None)
        if match is None:
            return None
        current = match
    return current if current.is_dir() else None


def _cet_dir_is_framework(cet_dir: Path) -> bool:
    """True if a cyber_engine_tweaks/ folder holds the framework itself
    (its dll/fonts/scripts) rather than only a mods/ subfolder (a CET mod)."""
    try:
        entries = [e.name.lower() for e in cet_dir.iterdir()]
    except OSError:
        return False
    # Framework ships these; a bare mod does not.
    framework_markers = {"fonts", "scripts", "cyber_engine_tweaks.dll", "init.lua"}
    if framework_markers & set(entries):
        return True
    # If the ONLY thing inside is 'mods', it's a mod, not the framework.
    non_mods = [e for e in entries if e != "mods"]
    return len(non_mods) > 0


def framework_is_installed(name: str, game_path: Path) -> bool:
    """Has framework `name` already been installed into the game folder?
    Checks for the framework's signature files under the real game path."""
    for fname, signatures in FRAMEWORK_ORDER:
        if fname != name:
            continue
        for sig in signatures:
            if fname == "Cyber Engine Tweaks" and sig.endswith("cyber_engine_tweaks"):
                cet_dir = _resolve_ci_dir(game_path, sig)
                if cet_dir is not None and _cet_dir_is_framework(cet_dir):
                    return True
                continue
            if _path_exists_ci(game_path, sig):
                return True
    return False


def _list_archive_names(archive: Path):
    """Return the list of member paths inside an archive without extracting
    it, or None if the archive can't be read. Used to classify frameworks
    cheaply before committing to a full extraction."""
    suffix = archive.suffix.lower()
    try:
        if suffix == ".zip":
            with zipfile.ZipFile(archive) as z:
                return z.namelist()
        elif suffix == ".rar" and HAS_RARFILE:
            import rarfile
            with rarfile.RarFile(archive) as r:
                return r.namelist()
        elif suffix == ".7z" and HAS_PY7ZR:
            with py7zr.SevenZipFile(archive) as z:
                return z.getnames()
    except Exception as e:
        print(f"[FRAMEWORK] Couldn't read {archive.name} for classification: {e}")
        return None
    return None


def classify_archive_framework(archive: Path):
    """Identify which framework an archive is, by matching its member paths
    against the framework signatures -- WITHOUT extracting it. Returns the
    framework name or None.

    Works on the archive listing (member paths as written inside the zip/7z),
    normalising separators and casing for the match only. This lets
    "Install All" order frameworks first without paying to extract every
    archive up front.
    """
    names = _list_archive_names(archive)
    if not names:
        return None

    normalised = [n.replace("\\", "/").lower().lstrip("/") for n in names]

    def has(sig: str) -> bool:
        s = sig.lower()
        # Match the signature as a path segment sequence anywhere in a member
        # path (archives often wrap everything in a top-level folder).
        for n in normalised:
            if n == s or n.endswith("/" + s) or ("/" + s + "/") in ("/" + n + "/"):
                return True
            # also allow the signature to appear as a prefix after a wrapper
            if ("/" + s) in ("/" + n):
                return True
        return False

    for name, signatures in FRAMEWORK_ORDER:
        for sig in signatures:
            # For CET, the framework .asi is the unambiguous signal; the bare
            # cyber_engine_tweaks folder can also be a CET mod, so only the
            # .asi (or framework files) count.
            if name == "Cyber Engine Tweaks":
                if has("bin/x64/plugins/cyber_engine_tweaks.asi"):
                    return name
                # framework files inside the folder
                if any("cyber_engine_tweaks/fonts" in n or
                       "cyber_engine_tweaks/scripts" in n or
                       n.endswith("cyber_engine_tweaks/cyber_engine_tweaks.dll")
                       for n in normalised):
                    return name
                continue
            if has(sig):
                return name
    return None


class InstallWorker(QtCore.QThread):
    installComplete = QtCore.pyqtSignal(str, list, str)
    installError = QtCore.pyqtSignal(str)
    # Emitted when the extracted archive turns out to be a FOMOD. Carries
    # (extracted_folder, original_archive) so the GUI thread can run the
    # wizard -- Qt dialogs cannot be created from a worker thread.
    fomodDetected = QtCore.pyqtSignal(object, object)
    # Emitted for every non-FOMOD archive once extracted, so the GUI thread
    # runs install_from_folder -- which is where the destination picker and
    # the overwrite prompts live. The worker can't show those dialogs
    # itself (background thread), and duplicating that logic here is exactly
    # what caused single-file mods to install without ever asking.
    needsInstall = QtCore.pyqtSignal(object, object)

    def __init__(self, archive: Path, game_path: Path, mod_library: Path, overwrite: bool):
        super().__init__()
        self.archive = archive
        self.game_path = game_path
        self.mod_library = mod_library
        self.overwrite = overwrite
        self.tmpdir = None

    def run(self):
        """Extract and process the archive"""
        try:
            self.tmpdir = Path(tempfile.mkdtemp(prefix="cp2077_install_"))
            self._extract_archive()

            # A FOMOD needs the wizard, which is a Qt dialog and therefore
            # must run on the GUI thread -- creating widgets from a worker
            # thread is undefined behaviour in Qt. So stop here, hand the
            # extracted folder to the main thread, and let it take over.
            # Cleanup becomes the main thread's job in that case.
            if is_fomod_folder(self.tmpdir):
                print(f"[FOMOD] Detected in worker, handing to GUI thread: {self.tmpdir}")
                self.fomodDetected.emit(self.tmpdir, self.archive)
                return

            # Everything else also goes back to the GUI thread, so it flows
            # through install_from_folder -- the one place with the
            # destination picker (for loose/unknown files) and the overwrite
            # prompts. The worker's own _install() is no longer used for the
            # normal path; it stayed silent and bypassed both.
            print(f"[INSTALL] Extracted, handing to GUI thread: {self.tmpdir}")
            self.needsInstall.emit(self.tmpdir, self.archive)
            return
        except Exception as e:
            self.installError.emit(f"Installation failed: {str(e)}")
            self._cleanup()

    def _extract_archive(self):
        """Extract the archive to temp directory"""
        try:
            suffix = self.archive.suffix.lower()

            if suffix == ".zip":
                with zipfile.ZipFile(self.archive) as z:
                    z.extractall(self.tmpdir)
            elif suffix == ".rar" and HAS_RARFILE:
                with rarfile.RarFile(self.archive) as r:
                    r.extractall(self.tmpdir)
            elif suffix == ".7z" and HAS_PY7ZR:
                with py7zr.SevenZipFile(self.archive) as z:
                    z.extractall(self.tmpdir)
            else:
                raise Exception(f"Unsupported archive format: {suffix}")

        except Exception as e:
            raise Exception(f"Failed to extract archive: {e}")

    def _install(self):
        """Install mod"""
        mod_name = self.archive.stem
        source_root = self._unwrap_wrapper(self.tmpdir)

        mod_type, actual_source, dest_relative, reason = detect_mod_type(source_root)

        print(f"[INSTALL] Mod: {mod_name}")
        print(f"[INSTALL] Type: {mod_type}")
        print(f"[INSTALL] Source: {actual_source}")
        print(f"[INSTALL] Destination: {dest_relative}")
        print(f"[INSTALL] Reason: {reason}")

        files_copied = []

        if str(dest_relative) == ".":
            for root, dirs, files in os.walk(actual_source):
                rel_root = Path(root).relative_to(actual_source)
                dest_root = self.game_path / rel_root
                dest_root.mkdir(parents=True, exist_ok=True)

                for f in files:
                    src_file = Path(root) / f
                    dest_file = dest_root / f

                    if dest_file.exists() and not self.overwrite:
                        continue

                    try:
                        shutil.copy2(src_file, dest_file)
                        files_copied.append(str(dest_file.relative_to(self.game_path)))
                        print(f"[COPY] {src_file.name} -> {dest_file.relative_to(self.game_path)}")
                    except Exception as e:
                        print(f"[ERROR] Failed to copy {src_file}: {e}")
        else:
            dest_base = self.game_path / dest_relative
            dest_base.mkdir(parents=True, exist_ok=True)

            for root, dirs, files in os.walk(actual_source):
                rel_root = Path(root).relative_to(actual_source)
                dest_root = dest_base / rel_root
                dest_root.mkdir(parents=True, exist_ok=True)

                for f in files:
                    src_file = Path(root) / f
                    dest_file = dest_root / f

                    if dest_file.exists() and not self.overwrite:
                        continue

                    try:
                        shutil.copy2(src_file, dest_file)
                        files_copied.append(str(dest_file.relative_to(self.game_path)))
                        print(f"[COPY] {src_file.name} -> {dest_file.relative_to(self.game_path)}")
                    except Exception as e:
                        print(f"[ERROR] Failed to copy {src_file}: {e}")

        if not files_copied:
            raise Exception("No files were installed")

        self.installComplete.emit(mod_name, files_copied, str(self.archive))
        self._cleanup()

    def _unwrap_wrapper(self, folder: Path) -> Path:
        """Descend past wrapper folders to the real mod root.

        Mod archives are routinely wrapped in a folder named after the mod
        ("Cool Mod Name/red4ext/..."). Installing that folder as-is puts
        <game>/Cool Mod Name/red4ext/ on disk, where the game never looks.

        The previous version only descended when a folder had EXACTLY one
        child directory, so a wrapper sitting next to a README.txt or a
        Docs/ folder was left in place. This version ignores documentation
        siblings and loose files when deciding, so those common layouts
        unwrap correctly too.
        """
        return _unwrap_mod_wrapper(folder)

    def _cleanup(self):
        """Clean up temp directory"""
        if self.tmpdir and self.tmpdir.exists():
            try:
                shutil.rmtree(self.tmpdir)
            except:
                pass

# ============================================================================
# FOMOD SUPPORT
# ----------------------------------------------------------------------------
# FOMOD is the XML installer format used by Nexus mods (fomod/ModuleConfig.xml).
# An archive using it ships several *named* option folders -- "Main Files",
# "Optional - 4K", "1.6 Patch" -- and the XML says which of them to install and
# where their contents go.
#
# Installing such an archive naively copies every option in, plus the fomod/
# metadata folder, plus the option folder names themselves. That is the
# "named folder ends up in the game directory" problem: a FOMOD saying
# `<folder source="Cool Mod Name" destination=""/>` means "copy the CONTENTS
# of Cool Mod Name into the game root", not "copy Cool Mod Name itself".
# ============================================================================



class SelectionType(Enum):
    SELECT_AT_LEAST_ONE = "SelectAtLeastOne"
    SELECT_AT_MOST_ONE = "SelectAtMostOne"
    SELECT_EXACTLY_ONE = "SelectExactlyOne"
    SELECT_ALL = "SelectAll"
    SELECT_ANY = "SelectAny"

    @classmethod
    def from_xml(cls, value):
        try:
            return cls(value)
        except ValueError:
            return cls.SELECT_ANY  # permissive fallback for unknown/omitted


@dataclass
class FomodFileMapping:
    """One <file> or <folder> entry. is_folder=True means the *contents* of
    source are copied into destination -- the source folder's own name is
    deliberately not preserved. That single rule is what stops mod-named
    wrapper folders leaking into the game directory."""
    source: str
    destination: str
    is_folder: bool
    priority: int = 0


@dataclass
class FomodFlag:
    name: str
    value: str


@dataclass
class FomodOption:
    name: str
    description: str = ""
    image: str = ""
    type_descriptor: str = "Optional"
    files: list = field(default_factory=list)
    flags_set: list = field(default_factory=list)


@dataclass
class FomodGroup:
    name: str
    selection_type: SelectionType = SelectionType.SELECT_ANY
    options: list = field(default_factory=list)


@dataclass
class FomodDependency:
    """A <dependencies>/<visible> tree. Empty evaluates True (an absent
    condition block is vacuously satisfied)."""
    operator: str = "And"
    flag_checks: list = field(default_factory=list)
    children: list = field(default_factory=list)
    unevaluated: int = 0  # file/gameDependency: parsed but always True

    def evaluate(self, flags: dict) -> bool:
        results = [flags.get(fc.name, "") == fc.value for fc in self.flag_checks]
        results += [True] * self.unevaluated
        results += [c.evaluate(flags) for c in self.children]
        if not results:
            return True
        if self.operator.lower() == "or":
            return any(results)
        return all(results)


@dataclass
class FomodStep:
    name: str
    groups: list = field(default_factory=list)
    visible: FomodDependency | None = None


@dataclass
class FomodConditional:
    dependencies: FomodDependency
    files: list = field(default_factory=list)


@dataclass
class FomodConfig:
    module_name: str
    required_files: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    conditionals: list = field(default_factory=list)


def _fomod_tag(el) -> str:
    """Local tag name with any XML namespace prefix stripped."""
    return el.tag.split("}")[-1]


def find_fomod_config(root: Path):
    """Locate fomod/ModuleConfig.xml anywhere in an extracted tree, matching
    both names case-insensitively (mod authors are inconsistent about it)."""
    for current, _dirs, files in os.walk(root):
        if Path(current).name.lower() == "fomod":
            for f in files:
                if f.lower() == "moduleconfig.xml":
                    return Path(current) / f
    return None


def is_fomod_folder(folder: Path) -> bool:
    return find_fomod_config(folder) is not None


def _parse_fomod_files(el) -> list:
    out = []
    if el is None:
        return out
    for child in el:
        tag = _fomod_tag(child)
        if tag == "file":
            src = child.get("source", "")
            out.append(FomodFileMapping(
                source=src,
                destination=child.get("destination", src),
                is_folder=False,
                priority=int(child.get("priority", "0") or 0),
            ))
        elif tag == "folder":
            out.append(FomodFileMapping(
                source=child.get("source", ""),
                destination=child.get("destination", ""),
                is_folder=True,
                priority=int(child.get("priority", "0") or 0),
            ))
    return out


def _parse_fomod_flags(el) -> list:
    out = []
    if el is None:
        return out
    for f in el:
        if _fomod_tag(f) == "flag":
            out.append(FomodFlag(name=f.get("name", ""), value=(f.text or "").strip()))
    return out


def _parse_fomod_deps(el) -> FomodDependency:
    if el is None:
        return FomodDependency()
    node = FomodDependency(operator=el.get("operator", "And"))
    for child in el:
        tag = _fomod_tag(child)
        if tag == "flagDependency":
            node.flag_checks.append(
                FomodFlag(name=child.get("flag", ""), value=child.get("value", ""))
            )
        elif tag in ("fileDependency", "gameDependency"):
            node.unevaluated += 1
        elif tag in ("dependencies", "moduleDependencies"):
            node.children.append(_parse_fomod_deps(child))
    return node


def _parse_fomod_option(el) -> FomodOption:
    desc_el = el.find("description")
    img_el = el.find("image")
    type_el = el.find("typeDescriptor")

    type_desc = "Optional"
    if type_el is not None:
        default_el = type_el.find("type")
        if default_el is not None:
            type_desc = default_el.get("name", "Optional")

    return FomodOption(
        name=el.get("name", ""),
        description=(desc_el.text or "").strip() if desc_el is not None else "",
        image=img_el.get("path", "") if img_el is not None else "",
        type_descriptor=type_desc,
        files=_parse_fomod_files(el.find("files")),
        flags_set=_parse_fomod_flags(el.find("conditionFlags")),
    )


def _parse_fomod_group(el) -> FomodGroup:
    options = []
    plugins = el.find("plugins")
    if plugins is not None:
        for p in plugins.findall("plugin"):
            options.append(_parse_fomod_option(p))
    return FomodGroup(
        name=el.get("name", ""),
        selection_type=SelectionType.from_xml(el.get("type")),
        options=options,
    )


def _parse_fomod_step(el) -> FomodStep:
    groups = []
    container = el.find("optionalFileGroups")
    if container is not None:
        for g in container.findall("group"):
            groups.append(_parse_fomod_group(g))
    vis = el.find("visible")
    return FomodStep(
        name=el.get("name", ""),
        groups=groups,
        visible=_parse_fomod_deps(vis) if vis is not None else None,
    )


def parse_fomod(config_path: Path) -> FomodConfig:
    """Parse fomod/ModuleConfig.xml into a FomodConfig.

    ModuleConfig.xml files are frequently written by Windows tools and turn
    up as UTF-16 (usually with a BOM), sometimes while still declaring
    utf-8 in the XML header. ElementTree handles a plain BOM fine, but a
    declaration that contradicts the actual bytes makes it raise. So on a
    parse failure we decode the bytes ourselves, strip the misleading
    declaration, and retry -- otherwise a perfectly good installer is
    rejected over a header the author never looked at.
    """
    try:
        tree = ET.parse(str(config_path))
        root = tree.getroot()
    except ET.ParseError:
        raw = Path(config_path).read_bytes()

        text = None
        for encoding in ("utf-16", "utf-8-sig", "utf-8", "latin-1"):
            try:
                candidate = raw.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
            # A successful decode should look like XML, not mojibake.
            if "<" in candidate and ">" in candidate:
                text = candidate
                break

        if text is None:
            raise  # genuinely unparseable; let the caller report it

        # Drop the <?xml ...?> declaration: its encoding attribute is the
        # thing that just contradicted reality, and it's optional anyway.
        stripped = text.lstrip("\ufeff").lstrip()
        if stripped.startswith("<?xml"):
            end = stripped.find("?>")
            if end != -1:
                stripped = stripped[end + 2:].lstrip()

        root = ET.fromstring(stripped)

    name_el = root.find("moduleName")
    module_name = (name_el.text or "Unnamed Mod").strip() if name_el is not None else "Unnamed Mod"

    steps = []
    steps_el = root.find("installSteps")
    if steps_el is not None:
        for s in steps_el.findall("installStep"):
            steps.append(_parse_fomod_step(s))

    conditionals = []
    cfi = root.find("conditionalFileInstalls")
    if cfi is not None:
        patterns = cfi.find("patterns")
        if patterns is not None:
            for pat in patterns.findall("pattern"):
                conditionals.append(FomodConditional(
                    dependencies=_parse_fomod_deps(pat.find("dependencies")),
                    files=_parse_fomod_files(pat.find("files")),
                ))

    return FomodConfig(
        module_name=module_name,
        required_files=_parse_fomod_files(root.find("requiredInstallFiles")),
        steps=steps,
        conditionals=conditionals,
    )


def resolve_fomod_installation(config: FomodConfig, selections: dict) -> list:
    """Given {step_name: [chosen option names]}, produce the priority-ordered
    list of file mappings to install: required files, plus each selected
    option's files, plus any conditional pattern its flags satisfy."""
    flags = {}
    selected = list(config.required_files)

    for step in config.steps:
        chosen = set(selections.get(step.name, []))
        for group in step.groups:
            for opt in group.options:
                if opt.name in chosen:
                    selected.extend(opt.files)
                    for fl in opt.flags_set:
                        flags[fl.name] = fl.value

    for cond in config.conditionals:
        if cond.dependencies.evaluate(flags):
            selected.extend(cond.files)

    # Stable sort: equal priorities keep declaration order, higher priority
    # is copied later and therefore wins same-path conflicts.
    return sorted(selected, key=lambda m: m.priority)


def expand_fomod_mapping(mapping: FomodFileMapping, staging_root: Path) -> list:
    """Expand one mapping into (absolute_source_file, dest_relative) pairs.

    THIS is where the mod-named-folder problem is actually solved. For a
    <folder source="Cool Mod Name" destination="">, we walk INSIDE
    "Cool Mod Name" and rebuild each file's path relative to that folder --
    so "Cool Mod Name/red4ext/plugins/x.dll" becomes "red4ext/plugins/x.dll".
    The wrapper folder's own name never appears in the output.

    FOMOD authors write paths Windows-style ("Main\\archive\\pc\\mod\\x.archive").
    On Linux a backslash is an ordinary filename character, not a separator,
    so those paths match nothing and the install silently yields zero files.
    Both source and destination are therefore normalised to forward slashes
    before use. Only the SEPARATORS change -- the names themselves, and their
    casing, are left exactly as written, because Cyberpunk's folders are
    case-exact and mods like "#PreemScopes.archive" are loaded by name.
    """
    results = []

    source = mapping.source.replace("\\", "/").strip("/")
    destination = mapping.destination.replace("\\", "/").strip("/")

    src = staging_root / source if source else staging_root

    if not mapping.is_folder:
        dest = destination or source
        if src.is_file():
            results.append((src, dest))
        return results

    if not src.is_dir():
        return results  # folder named in the XML isn't in the archive

    for current, _dirs, files in os.walk(src):
        rel_root = Path(current).relative_to(src)
        for f in files:
            rel_inner = (rel_root / f).as_posix() if str(rel_root) != "." else f
            dest_rel = f"{destination}/{rel_inner}" if destination else rel_inner
            results.append((Path(current) / f, dest_rel))

    return results

# --- UI Widgets ---
class DropArea(QtWidgets.QFrame):
    fileDropped = QtCore.pyqtSignal(list)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)

        self.normal_style = """
            background: #1e1e1e;
            border: 2px dashed #4a9bff;
            border-radius: 6px;
        """
        self.hover_style = """
            background: #252525;
            border: 2px dashed #6bb4ff;
            border-radius: 6px;
        """
        self.setStyleSheet(self.normal_style)
        self.setMinimumHeight(80)
        self.setMaximumHeight(100)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        label = QtWidgets.QLabel("Drag & drop mods here")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("font-size: 14px; color: #4a9bff; font-weight: bold;")
        layout.addWidget(label)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            self.setStyleSheet(self.hover_style)
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self.setStyleSheet(self.normal_style)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self.setStyleSheet(self.normal_style)
        urls = [u.toLocalFile() for u in event.mimeData().urls() if u.toLocalFile()]
        if urls:
            self.fileDropped.emit(urls)
        event.accept()

class ModTable(QtWidgets.QTableWidget):
    orderChanged = QtCore.pyqtSignal()

    def __init__(self):
        super().__init__(0, 8)
        self.setHorizontalHeaderLabels(["↑", "↓", "Mod Name", "Files", "Installed", "Mod ID", "Type", "Load Order"])
        header = self.horizontalHeader()
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.setSelectionBehavior(QtWidgets.QTableWidget.SelectionBehavior.SelectRows)
        self.setSelectionMode(QtWidgets.QTableWidget.SelectionMode.SingleSelection)
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setColumnWidth(0, 30)
        self.setColumnWidth(1, 30)
        self.setColumnWidth(7, 100)

        self.setStyleSheet("""
            QTableWidget {
                background-color: #1e1e1e;
                alternate-background-color: #252525;
                color: #d0d0d0;
                border: 1px solid #333333;
                selection-background-color: #4a9bff;
            }
            QHeaderView::section {
                background-color: #2d2d2d;
                color: #a0a0a0;
                padding: 8px;
                border: none;
                font-weight: bold;
            }
            QScrollBar:vertical {
                background: #252525;
                width: 18px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical {
                background: #4a9bff;
                border-radius: 4px;
                min-height: 20px;
            }
            QScrollBar::handle:vertical:hover {
                background: #6bb4ff;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                border: none;
                background: none;
            }
        """)

    def populate(self, state_or_archives, search=""):
        """Populate table with either ModRecords (dict) or archive Path objects (list)"""
        self.setRowCount(0)
        search_lower = search.lower() if search else ""

        if isinstance(state_or_archives, dict):
            items = sorted(state_or_archives.items(), key=lambda x: (x[1].order if x[1].order is not None else 0, x[0]))
            for mod_name, record in items:
                if search_lower and search_lower not in mod_name.lower():
                    continue

                row = self.rowCount()
                self.insertRow(row)

                # Up arrow button
                up_btn = QtWidgets.QPushButton("↑")
                up_btn.setMaximumWidth(30)
                up_btn.clicked.connect(lambda checked, r=row, m=mod_name: self._move_mod_up(r, m))
                self.setCellWidget(row, 0, up_btn)

                # Down arrow button
                down_btn = QtWidgets.QPushButton("↓")
                down_btn.setMaximumWidth(30)
                down_btn.clicked.connect(lambda checked, r=row, m=mod_name: self._move_mod_down(r, m))
                self.setCellWidget(row, 1, down_btn)

                self.setItem(row, 2, QtWidgets.QTableWidgetItem(mod_name))
                self.setItem(row, 3, QtWidgets.QTableWidgetItem(str(len(record.files))))
                installed_time = datetime.fromtimestamp(record.installed_at).strftime('%Y-%m-%d %H:%M')
                self.setItem(row, 4, QtWidgets.QTableWidgetItem(installed_time))
                self.setItem(row, 5, QtWidgets.QTableWidgetItem(record.mod_id))
                self.setItem(row, 6, QtWidgets.QTableWidgetItem(record.mod_type))

                # Load order spinbox
                order_spin = QtWidgets.QSpinBox()
                order_spin.setMinimum(1)
                order_spin.setMaximum(999)
                order_spin.setValue(record.order)
                order_spin.setMaximumWidth(100)
                order_spin.valueChanged.connect(lambda val, m=mod_name: self._on_order_spinbox_changed(m, val))
                self.setCellWidget(row, 7, order_spin)
        else:
            for archive in sorted(state_or_archives):
                if search_lower and search_lower not in archive.name.lower():
                    continue

                row = self.rowCount()
                self.insertRow(row)
                self.setItem(row, 0, QtWidgets.QTableWidgetItem(archive.name))

                try:
                    sz = archive.stat().st_size / (1024 * 1024)
                    self.setItem(row, 1, QtWidgets.QTableWidgetItem(f"{sz:.1f} MB"))

                    fc = 0
                    suffix = archive.suffix.lower()
                    if suffix == ".zip":
                        with zipfile.ZipFile(archive) as z:
                            fc = len(z.namelist())
                    elif suffix == ".rar" and HAS_RARFILE:
                        with rarfile.RarFile(archive) as r:
                            fc = len(r.namelist())
                    elif suffix == ".7z" and HAS_PY7ZR:
                        with py7zr.SevenZipFile(archive) as z:
                            fc = len(z.getnames())
                    self.setItem(row, 2, QtWidgets.QTableWidgetItem(str(fc)))
                except:
                    self.setItem(row, 1, QtWidgets.QTableWidgetItem("?"))
                    self.setItem(row, 2, QtWidgets.QTableWidgetItem("?"))

                mt = datetime.fromtimestamp(archive.stat().st_mtime).strftime('%Y-%m-%d %H:%M')
                self.setItem(row, 3, QtWidgets.QTableWidgetItem(mt))

        self.resizeColumnsToContents()

    def _move_mod_up(self, row, mod_name):
        """Move mod up in load order"""
        if row == 0:
            return
        self._swap_rows(row, row - 1)

    def _move_mod_down(self, row, mod_name):
        """Move mod down in load order"""
        if row >= self.rowCount() - 1:
            return
        self._swap_rows(row, row + 1)

    def _swap_rows(self, row1, row2):
        """Swap two rows and update order"""
        # Get spinbox values
        spin1 = self.cellWidget(row1, 7)
        spin2 = self.cellWidget(row2, 7)

        if isinstance(spin1, QtWidgets.QSpinBox) and isinstance(spin2, QtWidgets.QSpinBox):
            val1 = spin1.value()
            val2 = spin2.value()
            # Swap values
            spin1.blockSignals(True)
            spin2.blockSignals(True)
            spin1.setValue(val2)
            spin2.setValue(val1)
            spin1.blockSignals(False)
            spin2.blockSignals(False)

        self.orderChanged.emit()

    def _on_order_spinbox_changed(self, mod_name, new_order):
        """Handle direct spinbox entry for load order"""
        if mod_name not in self.parent_state:
            return

        old_order = self.parent_state[mod_name].order
        if old_order == new_order:
            return

        # Find which mod is currently at new_order
        for other_mod, record in self.parent_state.items():
            if record.order == new_order:
                # Swap orders
                record.order = old_order
                break

        self.parent_state[mod_name].order = new_order
        self.orderChanged.emit()

    def set_parent_state(self, state):
        """Store reference to parent state dict"""
        self.parent_state = state

    def selected_mod(self):
        sel = self.selectionModel().selectedRows()
        return self.item(sel[0].row(), 2).text() if sel else None

class ArchiveTable(QtWidgets.QTableWidget):
    def __init__(self):
        super().__init__(0, 3)
        self.setHorizontalHeaderLabels(["Archive Name", "Size", "Modified"])
        self.horizontalHeader().setStretchLastSection(True)
        self.setSelectionBehavior(QtWidgets.QTableWidget.SelectionBehavior.SelectRows)
        self.setSelectionMode(QtWidgets.QTableWidget.SelectionMode.SingleSelection)
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setStyleSheet("""
            QTableWidget {
                background-color: #1e1e1e;
                alternate-background-color: #252525;
                color: #d0d0d0;
                border: 1px solid #333333;
                selection-background-color: #4a9bff;
            }
            QHeaderView::section {
                background-color: #2d2d2d;
                color: #a0a0a0;
                padding: 8px;
                border: none;
                font-weight: bold;
            }
            QScrollBar:vertical {
                background: #252525;
                width: 18px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical {
                background: #4a9bff;
                border-radius: 4px;
                min-height: 20px;
            }
            QScrollBar::handle:vertical:hover {
                background: #6bb4ff;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                border: none;
                background: none;
            }
        """)

    def populate(self, archives: list[Path], search: str = ""):
        """Fill the table, optionally filtering by a search string.

        `search` defaults to empty so the existing no-argument callers
        (refresh_not_installed, refresh_archived) keep working unchanged --
        the search boxes were already passing it, which is what raised
        TypeError before this parameter existed.
        """
        search_lower = search.lower() if search else ""

        self.setRowCount(0)
        for archive in sorted(archives):
            if search_lower and search_lower not in archive.name.lower():
                continue
            try:
                stat = archive.stat()
            except OSError:
                # Archive vanished between the scan and now (moved, deleted,
                # or a download that completed away). Skip rather than crash.
                continue
            row = self.rowCount()
            self.insertRow(row)
            self.setItem(row, 0, QtWidgets.QTableWidgetItem(archive.name))
            size_mb = stat.st_size / (1024 * 1024)
            self.setItem(row, 1, QtWidgets.QTableWidgetItem(f"{size_mb:.1f} MB"))
            mtime = datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M')
            self.setItem(row, 2, QtWidgets.QTableWidgetItem(mtime))
        self.resizeColumnsToContents()

    def selected_archive(self):
        sel = self.selectionModel().selectedRows()
        return self.item(sel[0].row(), 0).text() if sel else None

class DownloadMonitor(QtCore.QThread):
    newDownload = QtCore.pyqtSignal(Path)
    downloadProgress = QtCore.pyqtSignal(str, int)

    def __init__(self, downloads_folder: Path, target_folder: Path):
        super().__init__()
        self.downloads_folder = downloads_folder
        self.target_folder = target_folder
        self.seen_files = set()
        self.tracking_crdownloads = {}
        self.tracking_files = {}
        self.library_files = set()  # Track files already in library
        self.running = True

    def run(self):
        # Initial scan of library
        self._scan_library()

        while self.running:
            # Monitor downloads folder
            if self.downloads_folder.exists():
                self._monitor_downloads()

            # Monitor library folder for new additions
            self._scan_library()

            time.sleep(2)

    def _scan_library(self):
        """Scan mod library folder for new files (not just from downloads)"""
        if not self.target_folder.exists():
            return

        current_library = set()
        for ext in ['.zip', '.rar', '.7z']:
            for archive in self.target_folder.rglob(f"*{ext}"):
                if not archive.name.startswith("archived_"):
                    current_library.add(archive)

        # Find new files added to library
        new_files = current_library - self.library_files
        for f in new_files:
            if f not in self.seen_files:
                self.seen_files.add(f)
                self.newDownload.emit(f)
                self.downloadProgress.emit(f.name, 100)
                print(f"[LIBRARY] Detected new mod in library: {f.name}")

        self.library_files = current_library

    def _monitor_downloads(self):
        """Monitor downloads folder for completed downloads"""
        current_files = set()

        for f in self.downloads_folder.iterdir():
            if f.suffix.lower() == '.crdownload':
                current_files.add(f)
                display_name = f.stem

                if f not in self.tracking_crdownloads:
                    self.tracking_crdownloads[f] = display_name
                    self.downloadProgress.emit(display_name, -1)
                    print(f"[DOWNLOAD] Detected downloading: {display_name}")
                continue

            # Skip partial/in-progress downloads from any browser. These get
            # renamed to their final name once complete, at which point they
            # fall through to the move logic below.
            if f.suffix.lower() in ('.crdownload', '.part', '.download', '.tmp', '.opdownload'):
                current_files.add(f)
                display_name = f.stem

                if f not in self.tracking_crdownloads:
                    self.tracking_crdownloads[f] = display_name
                    self.downloadProgress.emit(display_name, -1)
                    print(f"[DOWNLOAD] Detected downloading: {display_name}")
                continue

            # Move ANY completed file into the library, not just archives --
            # a mod can ship as a loose .archive, an .xl tweak, a PDF guide,
            # etc. Directories are skipped (we can't "download" a folder and
            # moving one wholesale isn't what a watch folder is for).
            if f.is_file():
                current_files.add(f)

                if f in self.seen_files:
                    continue

                was_crdownload = False
                for crf, display in list(self.tracking_crdownloads.items()):
                    if f.name.startswith(display) or display.startswith(f.stem):
                        del self.tracking_crdownloads[crf]
                        was_crdownload = True
                        print(f"[DOWNLOAD] Download completed: {f.name}")
                        break

                try:
                    current_size = f.stat().st_size
                except:
                    continue

                if f not in self.tracking_files:
                    self.tracking_files[f] = current_size
                    if was_crdownload:
                        self.downloadProgress.emit(f.name, 99)
                    continue

                last_size = self.tracking_files[f]

                if current_size == last_size and current_size > 0:
                    time.sleep(1)
                    try:
                        confirm_size = f.stat().st_size
                    except:
                        continue

                    if confirm_size == current_size:
                        self.seen_files.add(f)
                        if f in self.tracking_files:
                            del self.tracking_files[f]

                        self.target_folder.mkdir(parents=True, exist_ok=True)
                        target = self.target_folder / f.name
                        try:
                            shutil.move(str(f), str(target))
                            self.newDownload.emit(target)
                            self.downloadProgress.emit(f.name, 100)
                            print(f"[DOWNLOAD] Moved to library: {f.name}")
                        except Exception as e:
                            print(f"[DOWNLOAD] Failed to move {f.name}: {e}")
                else:
                    self.tracking_files[f] = current_size

        removed_cr = set(self.tracking_crdownloads.keys()) - current_files
        for f in removed_cr:
            display_name = self.tracking_crdownloads[f]
            del self.tracking_crdownloads[f]
            print(f"[DOWNLOAD] Stopped tracking: {display_name}")

        removed_files = set(self.tracking_files.keys()) - current_files
        for f in removed_files:
            del self.tracking_files[f]

    def stop(self):
        self.running = False

class ArchiveNameDialog(QtWidgets.QDialog):
    def __init__(self, default_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Archive Name")
        self.setModal(True)
        self.resize(400, 150)

        layout = QtWidgets.QVBoxLayout(self)

        layout.addWidget(QtWidgets.QLabel("Enter archive name:"))

        self.name_edit = QtWidgets.QLineEdit(default_name)
        layout.addWidget(self.name_edit)

        info_label = QtWidgets.QLabel("Note: Archive name will start with 'archived_'")
        info_label.setStyleSheet("color: #4a9bff; font-size: 10px;")
        layout.addWidget(info_label)

        button_layout = QtWidgets.QHBoxLayout()
        ok_btn = QtWidgets.QPushButton("OK")
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QtWidgets.QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(cancel_btn)
        button_layout.addWidget(ok_btn)
        layout.addLayout(button_layout)

    def get_name(self):
        return self.name_edit.text()

# --- Main Window ---
def _fomod_option_dest_files(option, staging_root: Path) -> set:
    """Return the set of destination-relative paths this option would write.

    Used to detect options within a group that install the SAME file -- if
    two options both write archive/pc/mod/car_paint.archive, letting the
    user pick both just means one silently overwrites the other. Detecting
    that lets the wizard force an either/or choice instead.
    """
    dests = set()
    for mapping in option.files:
        for _src, dest_rel in expand_fomod_mapping(mapping, staging_root):
            dests.add(dest_rel.replace("\\", "/").lower())
    return dests


def _group_has_file_conflict(group, staging_root: Path) -> bool:
    """True if any two options in the group write at least one identical
    destination file. Such a group can't sensibly allow multiple selections."""
    seen = {}
    for option in group.options:
        for dest in _fomod_option_dest_files(option, staging_root):
            if dest in seen and seen[dest] != option.name:
                return True
            seen[dest] = option.name
    return False


class FomodWizardDialog(QtWidgets.QDialog):
    """Qt version of the FOMOD installer wizard.

    Walks the install steps one at a time, rendering each group as radio
    buttons (SelectExactlyOne / SelectAtMostOne) or checkboxes (SelectAny /
    SelectAtLeastOne / SelectAll), enforcing each group's rule before
    letting you advance, and skipping steps whose <visible> conditions the
    flags so far don't satisfy.

    Navigation is linear: going Back keeps your earlier answers, and later
    steps re-evaluate their visibility when you move forward again. That
    covers effectively every real FOMOD without trying to be a byte-perfect
    reimplementation of every wizard edge case.
    """

    def __init__(self, config: FomodConfig, staging_root: Path, parent=None):
        super().__init__(parent)
        self.config = config
        self.staging_root = Path(staging_root)

        self.flags: dict = {}
        self.selections: dict = {}
        self._current_index = 0
        self._group_widgets: dict = {}
        # Qt discards a QPixmap's data if nothing references it; keeping the
        # loaded previews here avoids blank image labels.
        self._pixmaps: list = []

        self.setWindowTitle(f"Install: {config.module_name}")
        self.resize(760, 640)

        layout = QtWidgets.QVBoxLayout(self)

        self.header = QtWidgets.QLabel()
        self.header.setStyleSheet("font-size: 15px; font-weight: bold; padding: 4px;")
        layout.addWidget(self.header)

        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.content = QtWidgets.QWidget()
        self.content_layout = QtWidgets.QVBoxLayout(self.content)
        self.content_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(self.content)
        layout.addWidget(self.scroll, 1)

        buttons = QtWidgets.QHBoxLayout()
        self.back_btn = QtWidgets.QPushButton("Back")
        self.back_btn.clicked.connect(self._go_back)
        buttons.addWidget(self.back_btn)

        cancel_btn = QtWidgets.QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(cancel_btn)

        buttons.addStretch()

        self.next_btn = QtWidgets.QPushButton("Next")
        self.next_btn.setDefault(True)
        self.next_btn.clicked.connect(self._go_next)
        buttons.addWidget(self.next_btn)

        layout.addLayout(buttons)

        self._render_step(0)

    # ---- navigation -----------------------------------------------------

    def _step_visible(self, step: FomodStep) -> bool:
        if step.visible is None:
            return True
        return step.visible.evaluate(self.flags)

    def _render_step(self, start_index: int):
        idx = start_index
        while idx < len(self.config.steps) and not self._step_visible(self.config.steps[idx]):
            idx += 1

        if idx >= len(self.config.steps):
            self.accept()  # nothing left to ask
            return

        self._current_index = idx
        step = self.config.steps[idx]
        previously = set(self.selections.get(step.name, []))

        # Clear the previous step's widgets.
        while self.content_layout.count():
            item = self.content_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._group_widgets = {}

        self.header.setText(f"{step.name}   ({idx + 1} / {len(self.config.steps)})")

        for group in step.groups:
            self._render_group(group, previously)

        self.back_btn.setEnabled(idx > 0)
        is_last = all(not self._step_visible(s) for s in self.config.steps[idx + 1:])
        self.next_btn.setText("Install" if is_last else "Next")

    def _render_group(self, group: FomodGroup, previously: set):
        box = QtWidgets.QGroupBox(group.name)
        box_layout = QtWidgets.QVBoxLayout(box)

        declared_single = group.selection_type in (
            SelectionType.SELECT_EXACTLY_ONE,
            SelectionType.SELECT_AT_MOST_ONE,
        )

        # A group whose options write the same file can't allow multiple
        # picks -- selecting two would just have one silently overwrite the
        # other. Force it to behave as either/or, and tell the user why so
        # the restriction doesn't look arbitrary.
        forced_exclusive = False
        if not declared_single and group.selection_type != SelectionType.SELECT_ALL:
            if _group_has_file_conflict(group, self.staging_root):
                forced_exclusive = True

        single = declared_single or forced_exclusive

        if forced_exclusive:
            note = QtWidgets.QLabel(
                "⚠ These options replace the same file, so only one can be "
                "used. Pick whichever you want (or none)."
            )
            note.setWordWrap(True)
            note.setStyleSheet("color: #d19a2a; font-style: italic; padding: 2px 0 4px 0;")
            box_layout.addWidget(note)

        if single:
            # A button group keeps the radios mutually exclusive within this
            # group only -- without it, radios across separate groups would
            # fight each other.
            button_group = QtWidgets.QButtonGroup(self)
            button_group.setExclusive(True)
            widgets = []

            # SELECT_EXACTLY_ONE must have a choice; every other exclusive
            # case (declared AtMostOne, or a forced-exclusive group) is
            # allowed to select nothing, so offer an explicit "None" radio --
            # Qt radio buttons can't otherwise be unchecked once set.
            allow_none = group.selection_type != SelectionType.SELECT_EXACTLY_ONE
            restored = next((o.name for o in group.options if o.name in previously), None)

            if allow_none:
                none_radio = QtWidgets.QRadioButton("None (don't install any of these)")
                none_radio.setChecked(restored is None)
                button_group.addButton(none_radio)
                box_layout.addWidget(none_radio)
                widgets.append((None, none_radio))

            for i, opt in enumerate(group.options):
                radio = QtWidgets.QRadioButton(opt.name)
                if restored is not None:
                    radio.setChecked(opt.name == restored)
                elif not allow_none and i == 0:
                    radio.setChecked(True)  # EXACTLY_ONE, first visit: start valid
                button_group.addButton(radio)
                box_layout.addWidget(radio)
                self._add_option_detail(box_layout, opt)
                widgets.append((opt.name, radio))

            self._group_widgets[group.name] = ("single", widgets, button_group)
        else:
            widgets = []
            for opt in group.options:
                check = QtWidgets.QCheckBox(opt.name)
                if group.selection_type == SelectionType.SELECT_ALL:
                    check.setChecked(True)
                    check.setEnabled(False)  # nothing to choose
                else:
                    check.setChecked(opt.name in previously)
                box_layout.addWidget(check)
                self._add_option_detail(box_layout, opt)
                widgets.append((opt.name, check))

            self._group_widgets[group.name] = ("multi", widgets, None)

        self.content_layout.addWidget(box)

    def _add_option_detail(self, layout, opt: FomodOption):
        """Badge, description, and preview image for one option."""
        if opt.type_descriptor and opt.type_descriptor not in ("", "Optional"):
            badge = QtWidgets.QLabel(f"[{opt.type_descriptor}]")
            badge.setStyleSheet("color: #2a9d5c; margin-left: 22px;")
            layout.addWidget(badge)

        if opt.description:
            desc = QtWidgets.QLabel(opt.description)
            desc.setWordWrap(True)
            desc.setStyleSheet("color: #999; margin-left: 22px;")
            layout.addWidget(desc)

        pixmap = self._load_preview(opt.image)
        if pixmap is not None:
            img_label = QtWidgets.QLabel()
            img_label.setPixmap(pixmap)
            img_label.setStyleSheet("margin-left: 22px; margin-top: 4px;")
            layout.addWidget(img_label)

    def _load_preview(self, rel_path: str):
        """Load an option's preview screenshot.

        Scoped strictly to images inside the fomod/ metadata folder. It does
        a fallback filename search ONLY because authors write these paths
        Windows-style (backslashes, inconsistent casing) and they'd
        otherwise not resolve on Linux. Nothing here touches install paths:
        Cyberpunk's folders are case-exact and are used verbatim.

        Failure is always non-fatal -- a broken preview must never interrupt
        an install that's already underway.
        """
        if not rel_path:
            return None

        # Normalise separators only; do not alter casing of anything we install.
        cleaned = rel_path.replace("\\", "/").strip().lstrip("/")
        candidate = self.staging_root / cleaned

        if not candidate.is_file():
            # Fall back to locating the image by filename anywhere in the
            # extracted tree. Preview images only -- never install targets.
            wanted = Path(cleaned).name.lower()
            found = None
            for current, _dirs, files in os.walk(self.staging_root):
                for f in files:
                    if f.lower() == wanted:
                        found = Path(current) / f
                        break
                if found:
                    break
            if found is None:
                return None
            candidate = found

        try:
            pixmap = QtGui.QPixmap(str(candidate))
            if pixmap.isNull():
                return None
            if pixmap.width() > 420 or pixmap.height() > 300:
                pixmap = pixmap.scaled(
                    420, 300,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            self._pixmaps.append(pixmap)  # keep a reference alive
            return pixmap
        except Exception as e:
            print(f"[FOMOD] preview failed for {rel_path}: {e}")
            return None

    # ---- validation -----------------------------------------------------

    def _collect(self, step: FomodStep):
        """Return the chosen option names for this step, or None if a group's
        selection rule isn't satisfied (a warning is shown in that case)."""
        chosen = []
        for group in step.groups:
            entry = self._group_widgets.get(group.name)
            if entry is None:
                continue
            kind, widgets, _bg = entry

            if kind == "single":
                # The "None" radio carries a name of None; filter it so it
                # never enters the selection list.
                picked = [name for name, w in widgets if w.isChecked() and name is not None]
                if group.selection_type == SelectionType.SELECT_EXACTLY_ONE and not picked:
                    QtWidgets.QMessageBox.warning(
                        self, "Selection required",
                        f"Please choose an option in '{group.name}'."
                    )
                    return None
                chosen.extend(picked)
            else:
                picked = [name for name, w in widgets if w.isChecked()]
                if group.selection_type == SelectionType.SELECT_AT_LEAST_ONE and not picked:
                    QtWidgets.QMessageBox.warning(
                        self, "Selection required",
                        f"Please choose at least one option in '{group.name}'."
                    )
                    return None
                chosen.extend(picked)
        return chosen

    def _go_next(self):
        step = self.config.steps[self._current_index]
        chosen = self._collect(step)
        if chosen is None:
            return

        self.selections[step.name] = chosen
        for group in step.groups:
            for opt in group.options:
                if opt.name in chosen:
                    for fl in opt.flags_set:
                        self.flags[fl.name] = fl.value

        self._render_step(self._current_index + 1)

    def _go_back(self):
        idx = self._current_index - 1
        while idx >= 0 and not self._step_visible(self.config.steps[idx]):
            idx -= 1
        if idx >= 0:
            self._render_step(idx)

    def get_selections(self) -> dict:
        return dict(self.selections)


def _classify_fomod_install(files_written: list) -> str:
    """Work out a mod type from the paths a FOMOD install actually wrote.

    detect_mod_type() inspects an archive's layout, which is meaningless for
    a FOMOD (its option folders are named things like "Optional - 4K").
    Classifying from the real destinations instead gives the mod list an
    accurate type.
    """
    prefixes = {p.replace("\\", "/").split("/")[0].lower() for p in files_written}

    if "red4ext" in prefixes:
        return "RED4ext"
    if "bin" in prefixes:
        return "CET"
    if "r6" in prefixes:
        return "REDscript/TweakXL"
    if "mods" in prefixes:
        return "REDmod"
    if "engine" in prefixes:
        return "Engine"
    if "archive" in prefixes:
        return "Archive"
    if len(prefixes) > 1:
        return "Multi-Component"
    return "FOMOD"


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1400, 800)

        self.state = load_state()
        self.settings = load_settings()
        self.game_path = Path(self.settings["game_path"]) if self.settings["game_path"] else self.detect_game_path()
        self.mod_library = Path(self.settings["mod_library"])
        self.mod_library.mkdir(parents=True, exist_ok=True)
        self.archive_folder = Path(self.settings.get("archive_folder", ""))

        # Only monitor if the user has explicitly chosen a folder. Starting
        # the monitor moves completed downloads into the library, so an
        # unset (empty) folder means: do nothing until they opt in via
        # "Set Downloads Folder" in settings.
        self.download_monitor = None
        downloads_setting = self.settings.get("downloads_folder", "").strip()
        if downloads_setting:
            downloads_folder = Path(downloads_setting)
            self.download_monitor = DownloadMonitor(downloads_folder, self.mod_library)
            self.download_monitor.newDownload.connect(self.on_new_download)
            self.download_monitor.downloadProgress.connect(self.on_download_progress)
            self.download_monitor.start()
        else:
            print("[MONITOR] No downloads folder set -- monitoring disabled. "
                  "Set one in settings to enable it.")

        self.active_downloads = {}
        self.install_worker = None
        # Remembers a Yes-to-All / No-to-All overwrite choice across the
        # files of a single install; reset at the start of each install.
        self._overwrite_decision = None
        # Install queue for "Install All" / multi-select batches. Installs are
        # async (worker thread), so batches run one archive at a time.
        self._install_queue = []
        self._install_queue_total = 0
        self._install_queue_done = 0

        self.setup_ui()

    def closeEvent(self, event):
        if self.download_monitor is not None:
            self.download_monitor.stop()
            self.download_monitor.wait()
        event.accept()

    def setup_ui(self):
        self.setStyleSheet("""
            QMainWindow {
                background-color: #121212;
                color: #d0d0d0;
            }
            QMenuBar {
                background-color: #1e1e1e;
                color: #d0d0d0;
                border-bottom: 1px solid #333333;
            }
            QMenuBar::item {
                background: transparent;
                padding: 4px 8px;
            }
            QMenuBar::item:selected {
                background: #2d2d2d;
            }
            QMenuBar::item:pressed {
                background: #333333;
            }
            QTabWidget::pane {
                border: 1px solid #333333;
                background-color: #1e1e1e;
                border-radius: 4px;
            }
            QTabBar::tab {
                background: #1e1e1e;
                color: #a0a0a0;
                padding: 8px 16px;
                border: 1px solid #333333;
                border-bottom: none;
                margin-right: 2px;
            }
            QTabBar::tab:selected {
                background: #2d2d2d;
                color: #d0d0d0;
                border-bottom: 1px solid #2d2d2d;
            }
            QTabBar::tab:hover {
                background: #252525;
            }
            QPushButton {
                background-color: #2d2d2d;
                color: #d0d0d0;
                border: 1px solid #333333;
                padding: 6px 12px;
                border-radius: 4px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #3a3a3a;
                border: 1px solid #4a9bff;
            }
            QPushButton:pressed {
                background-color: #1a1a1a;
            }
            QLineEdit {
                background-color: #252525;
                color: #d0d0d0;
                border: 1px solid #333333;
                padding: 6px;
                border-radius: 4px;
            }
            QCheckBox {
                color: #d0d0d0;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
            }
            QCheckBox::indicator:checked {
                background-color: #4a9bff;
                border: 1px solid #333333;
            }
            QTextEdit {
                background-color: #1e1e1e;
                color: #d0d0d0;
                border: 1px solid #333333;
                padding: 8px;
            }
            QSpinBox {
                background-color: #252525;
                color: #d0d0d0;
                border: 1px solid #333333;
                padding: 4px;
                border-radius: 4px;
            }
        """)

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self.create_installed_tab(), "Installed Mods")
        self.tabs.addTab(self.create_not_installed_tab(), "Not Installed Mods / Downloading")
        self.tabs.addTab(self.create_archived_tab(), "Archived")
        layout.addWidget(self.tabs)

    def create_installed_tab(self):
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)

        path_row = QtWidgets.QHBoxLayout()
        path_row.addWidget(QtWidgets.QLabel("Game folder:"))
        self.game_path_edit = QtWidgets.QLineEdit(str(self.game_path) if self.game_path else "")
        path_row.addWidget(self.game_path_edit)
        browse_btn = QtWidgets.QPushButton("Browse...")
        browse_btn.clicked.connect(self.browse_game_path)
        path_row.addWidget(browse_btn)
        validate_btn = QtWidgets.QPushButton("Validate")
        validate_btn.clicked.connect(self.validate_game_path)
        path_row.addWidget(validate_btn)
        layout.addLayout(path_row)

        lib_row = QtWidgets.QHBoxLayout()
        lib_row.addWidget(QtWidgets.QLabel("Mod Library:"))
        self.mod_lib_edit = QtWidgets.QLineEdit(str(self.mod_library))
        lib_row.addWidget(self.mod_lib_edit)
        lib_browse = QtWidgets.QPushButton("Set Mod Library Folder")
        lib_browse.clicked.connect(self.set_mod_library)
        lib_row.addWidget(lib_browse)
        layout.addLayout(lib_row)

        dl_row = QtWidgets.QHBoxLayout()
        dl_row.addWidget(QtWidgets.QLabel("Downloads folder:"))
        self.downloads_edit = QtWidgets.QLineEdit(self.settings["downloads_folder"])
        dl_row.addWidget(self.downloads_edit)
        dl_browse = QtWidgets.QPushButton("Set Downloads Folder")
        dl_browse.clicked.connect(self.set_downloads_folder)
        dl_row.addWidget(dl_browse)
        layout.addLayout(dl_row)

        self.download_status = QtWidgets.QLabel("Downloads: Monitoring...")
        self.download_status.setStyleSheet("color: #4a9bff; padding: 4px;")
        layout.addWidget(self.download_status)

        options_row = QtWidgets.QHBoxLayout()
        self.overwrite_check = QtWidgets.QCheckBox("Overwrite existing files")
        self.overwrite_check.setChecked(self.settings.get("overwrite_files", False))
        options_row.addWidget(self.overwrite_check)
        shortcut_btn = QtWidgets.QPushButton("Make Desktop Shortcut")
        shortcut_btn.clicked.connect(self.create_desktop_shortcut)
        options_row.addWidget(shortcut_btn)
        options_row.addStretch()
        layout.addLayout(options_row)

        self.drop_area = DropArea()
        self.drop_area.fileDropped.connect(self.handle_drop)
        layout.addWidget(self.drop_area)

        btn_row = QtWidgets.QHBoxLayout()
        install_folder = QtWidgets.QPushButton("Install from folder...")
        install_folder.clicked.connect(self.install_from_folder_dialog)
        btn_row.addWidget(install_folder)

        archive_installed_btn = QtWidgets.QPushButton("Archive All Installed Mods")
        archive_installed_btn.clicked.connect(self.archive_all_installed_mods)
        btn_row.addWidget(archive_installed_btn)

        btn_row.addStretch()
        uninstall_btn = QtWidgets.QPushButton("Uninstall selected")
        uninstall_btn.clicked.connect(self.uninstall_selected)
        btn_row.addWidget(uninstall_btn)
        uninstall_all = QtWidgets.QPushButton("Uninstall ALL mods")
        uninstall_all.clicked.connect(self.uninstall_all)
        btn_row.addWidget(uninstall_all)
        layout.addLayout(btn_row)

        search_layout = QtWidgets.QHBoxLayout()
        search_layout.addWidget(QtWidgets.QLabel("Search:"))
        self.installed_search = QtWidgets.QLineEdit()
        self.installed_search.setPlaceholderText("Search installed mods (case-insensitive)...")
        self.installed_search.textChanged.connect(self._on_installed_search)
        search_layout.addWidget(self.installed_search)
        layout.addLayout(search_layout)

        self.installed_table = ModTable()
        self.installed_table.set_parent_state(self.state)
        self.installed_table.orderChanged.connect(self.update_mod_order)
        layout.addWidget(self.installed_table)
        self.refresh_installed()

        return widget

    def create_not_installed_tab(self):
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)

        progress_layout = QtWidgets.QHBoxLayout()
        self.download_progress_bar = QtWidgets.QProgressBar()
        self.download_progress_bar.setVisible(False)
        self.download_status_label = QtWidgets.QLabel("")
        self.download_status_label.setStyleSheet("color: #4a9bff; padding: 4px;")
        progress_layout.addWidget(self.download_progress_bar)
        progress_layout.addWidget(self.download_status_label)
        layout.addLayout(progress_layout)

        search_layout = QtWidgets.QHBoxLayout()
        search_layout.addWidget(QtWidgets.QLabel("Search:"))
        self.not_installed_search = QtWidgets.QLineEdit()
        self.not_installed_search.setPlaceholderText("Search mods (case-insensitive)...")
        self.not_installed_search.textChanged.connect(self._on_not_installed_search)
        search_layout.addWidget(self.not_installed_search)
        layout.addLayout(search_layout)

        btn_row = QtWidgets.QHBoxLayout()
        install_btn = QtWidgets.QPushButton("Install Selected Mod")
        install_btn.clicked.connect(self.install_from_not_installed)
        btn_row.addWidget(install_btn)

        install_all_btn = QtWidgets.QPushButton("Install All Mods")
        install_all_btn.clicked.connect(self.install_all_mods)
        btn_row.addWidget(install_all_btn)

        move_to_archived = QtWidgets.QPushButton("Move to Archived")
        move_to_archived.clicked.connect(self.move_selected_archive_to_archived)
        btn_row.addWidget(move_to_archived)

        delete_btn = QtWidgets.QPushButton("Delete Selected Mod")
        delete_btn.clicked.connect(self.delete_archive)
        btn_row.addWidget(delete_btn)

        delete_all_btn = QtWidgets.QPushButton("Delete All Mods")
        delete_all_btn.clicked.connect(self.delete_all_mods)
        btn_row.addWidget(delete_all_btn)

        btn_row.addStretch()

        archive_not_installed_btn = QtWidgets.QPushButton("Archive All Not Installed Mods")
        archive_not_installed_btn.clicked.connect(self.archive_all_not_installed_mods)
        btn_row.addWidget(archive_not_installed_btn)

        layout.addLayout(btn_row)

        self.not_installed_table = ArchiveTable()
        layout.addWidget(self.not_installed_table)

        self.not_installed_status_label = QtWidgets.QLabel("Total: 0 mods, 0 files")
        self.not_installed_status_label.setStyleSheet("color: #4a9bff; padding: 4px; font-size: 10px;")
        layout.addWidget(self.not_installed_status_label)

        self.refresh_not_installed()
        return widget

    def create_archived_tab(self):
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)

        archive_folder_row = QtWidgets.QHBoxLayout()
        archive_folder_row.addWidget(QtWidgets.QLabel("Archive Location:"))
        self.archive_folder_edit = QtWidgets.QLineEdit(str(self.archive_folder) if self.archive_folder else "")
        self.archive_folder_edit.setReadOnly(True)
        archive_folder_row.addWidget(self.archive_folder_edit)
        set_archive_btn = QtWidgets.QPushButton("Set Archive Location")
        set_archive_btn.clicked.connect(self.set_archive_location)
        archive_folder_row.addWidget(set_archive_btn)
        layout.addLayout(archive_folder_row)

        # Archive location info
        info_label = QtWidgets.QLabel("Archives are stored in: archive/installed mods archive/ and archive/not installed archive/")
        info_label.setStyleSheet("color: #4a9bff; font-size: 9px; padding: 4px;")
        layout.addWidget(info_label)

        button_container = QtWidgets.QWidget()
        button_layout = QtWidgets.QHBoxLayout(button_container)

        install_selected_btn = QtWidgets.QPushButton("Install Selected Archive")
        install_selected_btn.clicked.connect(self.install_selected_archive)
        button_layout.addWidget(install_selected_btn)

        extract_selected_btn = QtWidgets.QPushButton("Extract Selected Archive")
        extract_selected_btn.clicked.connect(self.extract_selected_archive)
        button_layout.addWidget(extract_selected_btn)

        move_to_not_installed_btn = QtWidgets.QPushButton("Move to Not Installed")
        move_to_not_installed_btn.clicked.connect(self.move_selected_archive_to_not_installed)
        button_layout.addWidget(move_to_not_installed_btn)

        delete_archived_btn = QtWidgets.QPushButton("Delete Archive")
        delete_archived_btn.clicked.connect(self.delete_archived_archive)
        button_layout.addWidget(delete_archived_btn)

        button_layout.addStretch()

        layout.addWidget(button_container)

        self.archived_table = ArchiveTable()
        layout.addWidget(self.archived_table)
        self.refresh_archived()

        return widget

    def detect_game_path(self):
        for p in COMMON_GAME_DIRS:
            if (p / "r6").exists():
                return p
        return None

    def browse_game_path(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Select Cyberpunk 2077 folder")
        if d:
            self.game_path_edit.setText(d)
            self.game_path = Path(d)
            self.settings["game_path"] = d
            save_settings(self.settings)
            self.refresh_installed()

    def validate_game_path(self):
        gp = Path(self.game_path_edit.text().strip())
        if not gp.exists():
            QtWidgets.QMessageBox.warning(self, "Invalid", "Folder does not exist.")
            return
        if not any((gp / m).exists() for m in ["r6", "archive", "bin"]):
            QtWidgets.QMessageBox.warning(self, "Not game", "Missing r6/archive/bin folders")
            return
        QtWidgets.QMessageBox.information(self, "OK", "Valid Cyberpunk 2077 folder!")

    def set_mod_library(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Select Mod Library Folder")
        if d:
            self.mod_lib_edit.setText(d)
            self.mod_library = Path(d)
            self.mod_library.mkdir(parents=True, exist_ok=True)
            self.settings["mod_library"] = d
            save_settings(self.settings)
            self.refresh_not_installed()

            # Restart monitoring against the new library, but only if a
            # downloads folder is actually set -- changing the library must
            # not silently switch monitoring on.
            self._restart_download_monitor(self.settings.get("downloads_folder", "").strip())

    def _restart_download_monitor(self, downloads_path: str):
        """Stop any running monitor and start a fresh one for downloads_path.
        An empty path leaves monitoring off. Centralised so the None state
        is handled in exactly one place."""
        if self.download_monitor is not None:
            self.download_monitor.stop()
            self.download_monitor.wait()
            self.download_monitor = None

        downloads_path = (downloads_path or "").strip()
        if not downloads_path:
            print("[MONITOR] Downloads folder cleared -- monitoring disabled.")
            return

        self.download_monitor = DownloadMonitor(Path(downloads_path), self.mod_library)
        self.download_monitor.newDownload.connect(self.on_new_download)
        self.download_monitor.downloadProgress.connect(self.on_download_progress)
        self.download_monitor.start()
        print(f"[MONITOR] Now monitoring: {downloads_path}")

    def set_downloads_folder(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Select Downloads Folder")
        if d:
            self.downloads_edit.setText(d)
            self.settings["downloads_folder"] = d
            save_settings(self.settings)
            self._restart_download_monitor(d)

    def set_archive_location(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Select Archive Location")
        if d:
            self.archive_folder_edit.setText(d)
            self.archive_folder = Path(d)
            self.settings["archive_folder"] = str(d)
            save_settings(self.settings)
            self.refresh_archived()

    def create_desktop_shortcut(self):
        desktop_dir = Path.home() / "Desktop"
        desktop_dir.mkdir(exist_ok=True)
        shortcut_file = desktop_dir / f"{APP_NAME}.desktop"
        script_path = Path(sys.argv[0]).resolve()
        script_dir = script_path.parent

        # Get paths for shell script and icon
        shell_script = script_dir / "run_mod_manager"
        icon_path = script_dir / "desktop image" / "cyberpunk_mod_manager_icon.png"

        icon_arg = f"\nIcon={icon_path}" if icon_path.exists() else ""

        content = f"""[Desktop Entry]
Type=Application
Name=Cyberpunk Mod Manager
Exec="{shell_script}"
Terminal=false
Categories=Game;{icon_arg}
"""
        shortcut_file.write_text(content)
        shortcut_file.chmod(0o755)

        if shell_script.exists():
            shell_script.chmod(0o755)
            msg = f"Created {shortcut_file}"
            if icon_path.exists():
                msg += f"\nWith icon from {icon_path}"
            QtWidgets.QMessageBox.information(self, "Shortcut", msg)
        else:
            QtWidgets.QMessageBox.warning(self, "Warning", f"Created {shortcut_file}\nbut run_mod_manager not found in script directory")

    def refresh_installed(self):
        gp_text = self.game_path_edit.text().strip()
        self.game_path = Path(gp_text) if gp_text else self.detect_game_path()
        if self.game_path:
            self.game_path_edit.setText(str(self.game_path))
        self.installed_table.populate(self.state)

    def refresh_not_installed(self):
        archives = []
        if self.mod_library.exists():
            for ext in ['.zip', '.rar', '.7z']:
                for archive in self.mod_library.rglob(f"*{ext}"):
                    if not archive.name.startswith("archived_"):
                        archives.append(archive)

        self.not_installed_table.populate(archives)

        total_files = 0
        for archive in archives:
            try:
                suffix = archive.suffix.lower()
                if suffix == ".zip":
                    with zipfile.ZipFile(archive) as z:
                        total_files += len(z.namelist())
                elif suffix == ".rar" and HAS_RARFILE:
                    with rarfile.RarFile(archive) as r:
                        total_files += len(r.namelist())
                elif suffix == ".7z" and HAS_PY7ZR:
                    with py7zr.SevenZipFile(archive) as z:
                        total_files += len(z.getnames())
            except:
                pass

        self.not_installed_status_label.setText(
            f"Total: {len(archives)} mods, {total_files} files"
        )

    def refresh_archived(self):
        archives = []
        if self.archive_folder.exists():
            for ext in ['.zip', '.rar', '.7z']:
                for archive in self.archive_folder.rglob(f"*{ext}"):
                    archives.append(archive)
        self.archived_table.populate(archives)

    def update_mod_order(self):
        for row in range(self.installed_table.rowCount()):
            mod_name = self.installed_table.item(row, 2).text()
            spin = self.installed_table.cellWidget(row, 7)
            if isinstance(spin, QtWidgets.QSpinBox) and mod_name in self.state:
                self.state[mod_name].order = spin.value()
        save_state(self.state)
        self.refresh_installed()

    def on_new_download(self, path: Path):
        print(f"[Download] Completed and moved to mod library: {path.name}")
        if path.name in self.active_downloads:
            del self.active_downloads[path.name]
        self.update_download_status()
        self.refresh_not_installed()

    def on_download_progress(self, filename: str, progress: int):
        if hasattr(self, 'not_installed_table') and self.tabs.currentIndex() == 1:
            if not self.download_progress_bar.isVisible():
                self.download_progress_bar.setVisible(True)

            if progress < 0:
                self.download_status_label.setText(f"Downloading: {filename}")
                self.download_progress_bar.setValue(0)
            elif progress == 100:
                self.download_status_label.setText(f"Download Complete: {filename}")
                self.download_progress_bar.setValue(100)
            else:
                self.download_status_label.setText(f"Downloading: {filename} ({progress}%)")
                self.download_progress_bar.setValue(progress)

        if progress == -1:
            self.active_downloads[filename] = "Downloading..."
        elif progress == 100:
            if filename in self.active_downloads:
                self.active_downloads[filename] = "Complete!"
        elif progress == 0:
            self.active_downloads[filename] = "Detected"

        self.update_download_status()

    def update_download_status(self):
        if not self.active_downloads:
            self.download_status.setText("Downloads: Monitoring...")
            self.download_status.setStyleSheet("color: #4a9bff; padding: 4px;")
        else:
            count = len(self.active_downloads)
            status_text = f"Active Downloads ({count}): " + ", ".join([f"{name} ({status})" for name, status in self.active_downloads.items()])
            self.download_status.setText(status_text)
            self.download_status.setStyleSheet("color: #00ff00; padding: 4px;")

    def handle_drop(self, paths):
        for p in paths:
            p = Path(p)
            suffix = p.suffix.lower()

            if suffix == ".zip":
                self.install_from_archive(p)
            elif suffix == ".rar" and HAS_RARFILE:
                self.install_from_archive(p)
            elif suffix == ".7z" and HAS_PY7ZR:
                self.install_from_archive(p)
            elif suffix in [".rar", ".7z"]:
                QtWidgets.QMessageBox.warning(self, "Unsupported", f"{suffix} support not available")
            elif p.is_dir():
                self.install_from_folder(p)

    def install_from_archive_dialog(self):
        filters = ["Zip files (*.zip)"]
        if HAS_RARFILE:
            filters.append("Rar files (*.rar)")
        if HAS_PY7ZR:
            filters.append("7z files (*.7z)")

        fp, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Select archive", filter=";;".join(filters))
        if fp:
            self.install_from_archive(Path(fp))

    def install_from_folder_dialog(self):
        dp = QtWidgets.QFileDialog.getExistingDirectory(self, "Select mod folder")
        if dp:
            self.install_from_folder(Path(dp))

    def install_from_not_installed(self):
        archive_name = self.not_installed_table.selected_archive()
        if not archive_name:
            return

        archive_path = None
        for ext in ['.zip', '.rar', '.7z']:
            candidates = list(self.mod_library.rglob(archive_name))
            if candidates:
                archive_path = candidates[0]
                break

        if archive_path and archive_path.exists():
            self.install_from_archive(archive_path)

    def install_all_mods(self):
        archives = []
        if self.mod_library.exists():
            for ext in ['.zip', '.rar', '.7z']:
                for archive in self.mod_library.rglob(f"*{ext}"):
                    if not archive.name.startswith("archived_"):
                        archives.append(archive)

        if not archives:
            QtWidgets.QMessageBox.information(self, "No Mods", "No mods found to install")
            return

        reply = QtWidgets.QMessageBox.question(
            self, "Confirm", f"Install all {len(archives)} mods?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No
        )

        if reply != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        # Order the batch so framework/dependency mods install first, in
        # dependency order, before any regular mod -- a mod that needs
        # ArchiveXL will fail if ArchiveXL isn't already present. Frameworks
        # already installed are skipped individually (not all-or-nothing):
        # if RED4ext is missing but RedScript is present, RED4ext installs
        # and RedScript is skipped.
        ordered = self._order_archives_frameworks_first(archives)
        self._enqueue_installs(ordered)

    def _order_archives_frameworks_first(self, archives: list) -> list:
        """Return archives reordered so known frameworks come first in
        dependency order (RED4ext, RedScript, CET, ArchiveXL, TweakXL,
        Codeware), then everything else in its original order. Frameworks
        already present in the game folder are dropped from the batch."""
        frameworks = []   # (rank, archive)
        regular = []
        skipped = []

        for archive in archives:
            fw = classify_archive_framework(archive)
            if fw is None:
                regular.append(archive)
                continue

            # Already installed? Skip just this one.
            if self.game_path and framework_is_installed(fw, self.game_path):
                skipped.append((fw, archive))
                print(f"[FRAMEWORK] {fw} already installed -- skipping {archive.name}")
                continue

            frameworks.append((FRAMEWORK_RANK[fw], fw, archive))

        # Sort frameworks by their required order; stable within same rank.
        frameworks.sort(key=lambda t: t[0])

        if frameworks:
            print("[FRAMEWORK] Install order: "
                  + ", ".join(f"{fw}" for _r, fw, _a in frameworks)
                  + (f" | then {len(regular)} other mod(s)" if regular else ""))

        ordered = [a for _r, _fw, a in frameworks] + regular

        if skipped:
            names = ", ".join(fw for fw, _a in skipped)
            print(f"[FRAMEWORK] Skipped (already installed): {names}")

        return ordered

    def _enqueue_installs(self, archives: list):
        """Queue a batch of archives to install one after another."""
        self._install_queue = list(archives)
        self._install_queue_total = len(archives)
        self._install_queue_done = 0
        self._install_next_in_queue()

    def _install_next_in_queue(self):
        """Start the next queued archive, or finish if the queue is empty."""
        queue = getattr(self, "_install_queue", None)
        if not queue:
            # Batch complete (or nothing queued).
            if getattr(self, "_install_queue_total", 0) > 1:
                print(f"[INSTALL] Batch complete: {self._install_queue_done}/"
                      f"{self._install_queue_total} processed")
            self._install_queue_total = 0
            return

        archive = queue.pop(0)
        self._install_queue_done = getattr(self, "_install_queue_done", 0) + 1
        print(f"[INSTALL] Queue {self._install_queue_done}/"
              f"{self._install_queue_total}: {archive.name}")
        self.install_from_archive(archive)

    def _on_installed_search(self):
        search_text = self.installed_search.text()
        self.installed_table.populate(self.state, search_text)

    def _on_not_installed_search(self):
        search_text = self.not_installed_search.text()
        archives = []
        if self.mod_library.exists():
            try:
                for ext in ['.zip', '.rar', '.7z']:
                    for archive in self.mod_library.rglob(f"*{ext}"):
                        if not archive.name.startswith("archived_"):
                            archives.append(archive)
            except Exception as e:
                print(f"[ERROR] Error scanning mod library: {e}")

        self.not_installed_table.populate(archives, search_text)

    def delete_all_mods(self):
        archives = []
        if self.mod_library.exists():
            for ext in ['.zip', '.rar', '.7z']:
                for archive in self.mod_library.rglob(f"*{ext}"):
                    if not archive.name.startswith("archived_"):
                        archives.append(archive)

        if not archives:
            QtWidgets.QMessageBox.information(self, "No Mods", "No mods found to delete")
            return

        reply = QtWidgets.QMessageBox.warning(
            self, "WARNING",
            f"Are you sure you want to DELETE ALL {len(archives)} mods?\n\nThis action cannot be undone!",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No
        )

        if reply == QtWidgets.QMessageBox.StandardButton.Yes:
            deleted_count = 0
            for archive in archives:
                try:
                    archive.unlink()
                    deleted_count += 1
                except Exception as e:
                    print(f"Failed to delete {archive.name}: {e}")

            QtWidgets.QMessageBox.information(self, "Complete", f"Deleted {deleted_count} of {len(archives)} mods")
            self.refresh_not_installed()

    def uninstall_selected(self):
        mod_name = self.installed_table.selected_mod()
        if not mod_name or mod_name not in self.state:
            return

        reply = QtWidgets.QMessageBox.question(
            self, "Confirm", f"Uninstall '{mod_name}'?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No
        )

        if reply == QtWidgets.QMessageBox.StandardButton.Yes:
            self._remove_files(self.state[mod_name].files)
            del self.state[mod_name]
            save_state(self.state)
            self.refresh_installed()
            self.refresh_not_installed()
            QtWidgets.QMessageBox.information(self, "Success", f"Uninstalled '{mod_name}'")

    def uninstall_all(self):
        reply = QtWidgets.QMessageBox.question(
            self, "Confirm", f"Uninstall ALL {len(self.state)} mods?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No
        )

        if reply == QtWidgets.QMessageBox.StandardButton.Yes:
            for rec in self.state.values():
                self._remove_files(rec.files)
            self.state.clear()
            save_state(self.state)
            self.refresh_installed()
            QtWidgets.QMessageBox.information(self, "Success", "All mods uninstalled")

    def delete_archive(self):
        archive_name = self.not_installed_table.selected_archive()
        if not archive_name:
            return

        reply = QtWidgets.QMessageBox.question(
            self, "Confirm", f"Delete '{archive_name}'?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No
        )

        if reply == QtWidgets.QMessageBox.StandardButton.Yes:
            for ext in ['.zip', '.rar', '.7z']:
                candidates = list(self.mod_library.rglob(archive_name))
                for archive_path in candidates:
                    if archive_path.exists():
                        archive_path.unlink()
            self.refresh_not_installed()

    def move_selected_archive_to_archived(self):
        archive_name = self.not_installed_table.selected_archive()
        if not archive_name or not self.archive_folder:
            QtWidgets.QMessageBox.warning(self, "Error", "Please set archive location first")
            return

        source_path = None
        for ext in ['.zip', '.rar', '.7z']:
            candidates = list(self.mod_library.rglob(archive_name))
            if candidates:
                source_path = candidates[0]
                break

        if source_path and source_path.exists():
            try:
                new_name = f"archived_{archive_name}" if not archive_name.startswith("archived_") else archive_name
                dest_path = self.archive_folder / new_name
                shutil.move(str(source_path), str(dest_path))
                self.refresh_not_installed()
                self.refresh_archived()
                QtWidgets.QMessageBox.information(self, "Success", f"Moved '{archive_name}' to archived location")
            except Exception as e:
                QtWidgets.QMessageBox.critical(self, "Error", f"Failed to move archive: {e}")

    def install_selected_archive(self):
        archive_name = self.archived_table.selected_archive()
        if not archive_name or not self.archive_folder:
            return

        source_path = None
        for ext in ['.zip', '.rar', '.7z']:
            candidates = list(self.archive_folder.rglob(archive_name))
            if candidates:
                source_path = candidates[0]
                break

        if source_path and source_path.exists():
            # Use the same extraction and installation method that works correctly
            self.extract_and_install_archive(source_path)

    def archive_all_installed_mods(self):
        if not self.state:
            QtWidgets.QMessageBox.information(self, "No mods", "No installed mods to archive")
            return

        if not self.archive_folder:
            QtWidgets.QMessageBox.warning(self, "Error", "Set archive location first")
            return

        # Create subdirectory for installed mods archives
        installed_archive_dir = self.archive_folder / "installed mods archive"
        installed_archive_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"installed_backup_{timestamp}"

        dialog = ArchiveNameDialog(default_name, self)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return

        custom_name = dialog.get_name().strip()
        if not custom_name:
            custom_name = default_name

        archive_name = f"archived_{custom_name}.zip"
        archive_path = installed_archive_dir / archive_name

        try:
            with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                folders_added = set()
                files_archived = 0

                for mod_name, rec in self.state.items():
                    for file_path in rec.files:
                        # Get the full path in the game folder
                        full_file_path = self.game_path / file_path

                        # Only add if file exists
                        if full_file_path.exists():
                            # Add to archive with FULL folder structure from game root
                            # e.g., archive/pc/mod/filename.archive or r6/tweaks/folder/file.xml
                            archive_entry = str(Path(file_path))
                            zf.write(full_file_path, archive_entry)
                            files_archived += 1

                            # Track top-level folder
                            parts = Path(file_path).parts
                            if len(parts) > 0:
                                folders_added.add(parts[0])

            QtWidgets.QMessageBox.information(
                self, "Success",
                f"Created backup: {archive_name}\n"
                f"Archived {len(self.state)} mods\n"
                f"Files: {files_archived}\n"
                f"Location: {archive_path}\n"
                f"Main folders: {', '.join(sorted(folders_added))}"
            )
            self.refresh_archived()
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Error", f"Failed to create archive: {e}")

    def extract_selected_archive(self):
        """Extract archive contents to a folder without installing"""
        archive_name = self.archived_table.selected_archive()
        if not archive_name or not self.archive_folder:
            return

        source_path = None
        for ext in ['.zip', '.rar', '.7z']:
            candidates = list(self.archive_folder.rglob(archive_name))
            if candidates:
                source_path = candidates[0]
                break

        if not source_path or not source_path.exists():
            QtWidgets.QMessageBox.warning(self, "Error", "Archive not found")
            return

        extract_dir = QtWidgets.QFileDialog.getExistingDirectory(self, "Select extraction folder")
        if not extract_dir:
            return

        extract_path = Path(extract_dir)

        try:
            if source_path.suffix.lower() == ".zip":
                with zipfile.ZipFile(source_path) as z:
                    z.extractall(extract_path)
            elif source_path.suffix.lower() == ".rar":
                if not HAS_RARFILE:
                    QtWidgets.QMessageBox.critical(self, "Error", "rarfile library not available")
                    return
                with rarfile.RarFile(source_path) as r:
                    r.extractall(extract_path)
            elif source_path.suffix.lower() == ".7z":
                if not HAS_PY7ZR:
                    QtWidgets.QMessageBox.critical(self, "Error", "py7zr library not available")
                    return
                with py7zr.SevenZipFile(source_path) as z:
                    z.extractall(extract_path)
            else:
                QtWidgets.QMessageBox.critical(self, "Error", f"Unsupported format: {source_path.suffix}")
                return

            QtWidgets.QMessageBox.information(self, "Success", f"Extracted '{archive_name}' to {extract_path}")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Error", f"Failed to extract archive: {e}")

    def archive_all_not_installed_mods(self):
        archives = []
        if self.mod_library.exists():
            for ext in ['.zip', '.rar', '.7z']:
                for archive in self.mod_library.rglob(f"*{ext}"):
                    if not archive.name.startswith("archived_"):
                        archives.append(archive)

        if not archives:
            QtWidgets.QMessageBox.information(self, "No mods", "No mods found in Not Installed to archive")
            return

        if not self.archive_folder:
            QtWidgets.QMessageBox.warning(self, "Error", "Set archive location first")
            return

        # Create subdirectory for not installed mods archives
        not_installed_archive_dir = self.archive_folder / "not installed archive"
        not_installed_archive_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"not_installed_backup_{timestamp}"

        dialog = ArchiveNameDialog(default_name, self)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return

        custom_name = dialog.get_name().strip()
        if not custom_name:
            custom_name = default_name

        archive_name = f"archived_{custom_name}.zip"
        archive_path = not_installed_archive_dir / archive_name

        try:
            with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for archive in archives:
                    zf.write(archive, archive.name)

            QtWidgets.QMessageBox.information(
                self, "Success",
                f"Created archive: {archive_name}\n"
                f"Archived {len(archives)} mods\n"
                f"Location: {archive_path}"
            )
            self.refresh_archived()
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Error", f"Failed to create archive: {e}")

    def _remove_files(self, files):
        removed = 0
        for f in files:
            if not isinstance(f, str) or not f:
                continue
            fp = self.game_path / f
            if not fp.exists():
                continue  # skip nonexistent paths
            try:
                if fp.is_dir():
                    shutil.rmtree(fp)
                else:
                    fp.unlink()
                removed += 1
            except Exception as e:
                print(f"Failed to remove {fp}: {e}")
            parent = fp.parent
            while parent != self.game_path and parent.exists():
                try:
                    if not any(parent.iterdir()):
                        parent.rmdir()
                        parent = parent.parent
                    else:
                        break
                except Exception:
                    break
        print(f"Removed {removed}/{len(files)} files")

    def install_from_archive(self, archive: Path):
        if self.install_worker and self.install_worker.isRunning():
            # Silently ignore a second install while one is running -- the
            # guard still matters (concurrent installs would clash), but the
            # popup was noise.
            print("[INSTALL] Ignored: an install is already in progress")
            return

        self.install_worker = InstallWorker(
            archive, self.game_path, self.mod_library,
            self.overwrite_check.isChecked()
        )
        self.install_worker.installComplete.connect(self._on_install_complete)
        self.install_worker.installError.connect(self._on_install_error)
        self.install_worker.fomodDetected.connect(self._on_fomod_detected)
        self.install_worker.needsInstall.connect(self._on_needs_install)
        self.install_worker.start()

    def _on_needs_install(self, extracted: Path, archive: Path):
        """Runs on the GUI thread for every non-FOMOD archive the worker
        extracted. Routes through install_from_folder so loose/unknown files
        get the destination picker and existing files get the overwrite
        prompt -- the whole point of moving off the worker's silent copy."""
        extracted = Path(extracted)
        archive = Path(archive)
        try:
            self.install_from_folder(extracted, archive_path=str(archive))
        finally:
            try:
                if extracted.exists():
                    shutil.rmtree(extracted, ignore_errors=True)
            except Exception as e:
                print(f"[INSTALL] temp cleanup failed: {e}")
            self._advance_install_queue()

    def _advance_install_queue(self):
        """Move to the next queued install, if a batch is in progress.

        Deferred to the next event-loop tick so the just-finished worker
        thread has fully settled -- otherwise the busy guard in
        install_from_archive could see it still 'running' and drop the next
        item."""
        if getattr(self, "_install_queue", None):
            QtCore.QTimer.singleShot(0, self._install_next_in_queue)

    def _on_fomod_detected(self, extracted: Path, archive: Path):
        """Runs on the GUI thread after the worker extracted a FOMOD archive.

        The worker deliberately stops before installing so the wizard (a Qt
        dialog) can be shown from here. Cleaning up the worker's temp folder
        is therefore this method's responsibility.
        """
        extracted = Path(extracted)
        archive = Path(archive)
        mod_name = archive.stem

        try:
            handled = self._try_install_fomod(extracted, mod_name, str(archive))
            if not handled:
                # find_fomod_config disagreed with is_fomod_folder, which
                # shouldn't happen -- fall back rather than silently do
                # nothing, so the user still gets their mod installed.
                print("[FOMOD] Not handled after detection; using normal install")
                self.install_from_folder(extracted, archive_path=str(archive))
        finally:
            try:
                if extracted.exists():
                    shutil.rmtree(extracted, ignore_errors=True)
            except Exception as e:
                print(f"[FOMOD] temp cleanup failed: {e}")
            self._advance_install_queue()

    def _try_install_fomod(self, folder: Path, mod_name: str, archive_path="") -> bool:
        """If this folder is a FOMOD, run the wizard and install the chosen
        files. Returns True if handled (installed or cancelled), False if
        this isn't a FOMOD and normal installation should proceed.
        """
        config_path = find_fomod_config(folder)
        if config_path is None:
            return False

        # The FOMOD's own root is the folder CONTAINING fomod/, which may be
        # nested if the archive wraps everything in a mod-named folder.
        fomod_root = config_path.parent.parent
        print(f"[FOMOD] Detected installer: {config_path}")
        print(f"[FOMOD] Root: {fomod_root}")

        try:
            config = parse_fomod(config_path)
        except Exception as e:
            print(f"[FOMOD] Parse failed: {e}")
            # Deliberately do NOT fall through to normal detection here.
            # This folder is laid out as a FOMOD (named option folders plus
            # fomod/ metadata), so detect_mod_type() would treat those option
            # folders as content and copy them -- and the metadata -- into
            # the game directory. That is the exact bug FOMOD support exists
            # to prevent, so a broken installer must not re-open that path.
            QtWidgets.QMessageBox.warning(
                self, "FOMOD Error",
                f"'{mod_name}' has a FOMOD installer, but its ModuleConfig.xml "
                f"could not be read:\n\n{e}\n\n"
                "Installing it automatically would likely copy the installer's "
                "option folders into your game directory, so it has been "
                "stopped.\n\n"
                "Extract the archive and install the correct subfolder manually."
            )
            return True  # handled: refused, nothing written

        # An installer with no steps has nothing to ask; just take the
        # required files rather than showing an empty wizard.
        if config.steps:
            wizard = FomodWizardDialog(config, fomod_root, parent=self)
            if wizard.exec() != QtWidgets.QDialog.DialogCode.Accepted:
                print("[FOMOD] Cancelled by user")
                return True  # handled: user chose not to install
            selections = wizard.get_selections()
        else:
            selections = {}

        mappings = resolve_fomod_installation(config, selections)

        # Expand to concrete (source_file, dest_relative) pairs. Folder
        # mappings copy their CONTENTS, so a "Cool Mod Name" wrapper never
        # appears in the destination path.
        expanded = []
        for mapping in mappings:
            expanded.extend(expand_fomod_mapping(mapping, fomod_root))

        if not expanded:
            QtWidgets.QMessageBox.warning(
                self, "Nothing to install",
                "The FOMOD installer didn't resolve to any files.\n\n"
                "The archive may be laid out unusually -- try installing it "
                "manually."
            )
            return True

        overwrite = self.overwrite_check.isChecked()
        files_copied = []
        skipped = 0

        for src_file, dest_rel in expanded:
            if not src_file.is_file():
                continue
            # Destination paths come straight from the archive and the XML.
            # Cyberpunk's folders are case-exact, so nothing is normalised.
            dest_file = self.game_path / dest_rel
            if dest_file.exists() and not overwrite:
                skipped += 1
                continue
            try:
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dest_file)
                files_copied.append(str(dest_file.relative_to(self.game_path)))
            except Exception as e:
                print(f"[FOMOD] Failed to copy {src_file}: {e}")

        if not files_copied:
            QtWidgets.QMessageBox.warning(
                self, "Warning",
                "No files were installed."
                + (f"\n\n{skipped} already existed (enable Overwrite to replace them)."
                   if skipped else "")
            )
            return True

        # Classify for the mod list using the paths actually written, so the
        # type reflects what landed on disk rather than the archive layout.
        mod_type = _classify_fomod_install(files_copied)

        max_order = max((rec.order for rec in self.state.values()), default=0)
        self.state[mod_name] = ModRecord(
            name=mod_name,
            mod_id=generate_mod_id(fomod_root),
            mod_type=mod_type,
            installed_at=time.time(),
            order=max_order + 1,
            files=files_copied,
            archive_path=archive_path,
        )
        save_state(self.state)
        self.refresh_installed()

        print(f"[FOMOD] Installed {len(files_copied)} files as {mod_type}")
        return True

    def _is_loose_files(self, source_root: Path) -> bool:
        """True when the mod is just loose files with no game-folder structure
        of its own (no archive/, bin/, r6/, red4ext/, engine/, or mods/ at the
        root). A lone .tweak or .archive like this could belong in more than
        one place, so the user should choose rather than have us guess."""
        try:
            entries = list(source_root.iterdir())
        except OSError:
            return False

        # If any recognised game folder is present, the layout tells us where
        # things go -- no need to ask.
        for name in MAIN_GAME_FOLDERS:
            if any(e.name.lower() == name and e.is_dir() for e in entries):
                return False

        # Otherwise: is there at least one actual file to install?
        has_file = any(p.is_file() for p in source_root.rglob("*"))
        return has_file

    def _ask_install_destination(self, mod_name: str, source_root: Path,
                                 reason: str, suggested: str = None):
        """Let the user choose where a mod installs.

        Returns a destination path relative to the game folder (e.g.
        "r6/tweaks"), or None if cancelled. Used for unrecognised mods and
        for loose single-file mods, so a file with no folder structure still
        lands where the user wants instead of being auto-placed.

        `suggested`, if given, is our best guess (e.g. "r6/tweaks" for a
        .tweak) and is pre-selected in the dropdown.
        """
        # Show what's actually in the mod so the user can decide.
        sample = []
        for p in sorted(source_root.rglob("*")):
            if p.is_file():
                sample.append(p.name)
            if len(sample) >= 8:
                break
        sample_text = "\n".join(f"  • {s}" for s in sample)
        if not sample_text:
            sample_text = "  (no files found)"

        common = [
            ("archive/pc/mod", "Archive mods (.archive files)"),
            ("r6/tweaks", "TweakXL / .tweak files"),
            ("r6/scripts", "REDscript (.reds files)"),
            ("red4ext/plugins", "RED4ext plugins (.dll)"),
            ("bin/x64/plugins/cyber_engine_tweaks/mods", "Cyber Engine Tweaks"),
            ("mods", "REDmod"),
            ("__custom__", "Custom location…"),
        ]

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(f"Install location — {mod_name}")
        dlg.resize(540, 440)
        layout = QtWidgets.QVBoxLayout(dlg)

        # Mod name as a clear, prominent heading -- makes it easy to look the
        # mod up on Nexus to check where its files are meant to go.
        name_label = QtWidgets.QLabel(mod_name)
        name_label.setStyleSheet(
            "font-size: 16px; font-weight: bold; padding: 2px 0 6px 0;"
        )
        name_label.setWordWrap(True)
        layout.addWidget(name_label)

        if suggested:
            sub = (f"Loose file with no folder structure. "
                   f"<span style='color:#999'>Best guess: {suggested} — change it if that's wrong.</span>")
        else:
            sub = (f"Not automatically recognised. "
                   f"<span style='color:#999'>{reason}</span>")
        sub_label = QtWidgets.QLabel(sub)
        sub_label.setWordWrap(True)
        layout.addWidget(sub_label)

        files_label = QtWidgets.QLabel(f"Files in this mod:\n{sample_text}")
        files_label.setStyleSheet("color:#aaa; padding:6px 0;")
        layout.addWidget(files_label)

        layout.addWidget(QtWidgets.QLabel("Install into (relative to your game folder):"))

        combo = QtWidgets.QComboBox()
        for path, label in common:
            combo.addItem(f"{label}   →   {path}" if path != "__custom__" else label, path)
        layout.addWidget(combo)

        # Pre-select: an explicit suggestion wins; otherwise guess from the
        # file extensions present.
        default_index = 0
        if suggested:
            for i, (path, _label) in enumerate(common):
                if path == suggested:
                    default_index = i
                    break
        else:
            exts = {p.suffix.lower() for p in source_root.rglob("*") if p.is_file()}
            if ".tweak" in exts or ".xl" in exts:
                default_index = 1
            elif ".reds" in exts:
                default_index = 2
            elif ".dll" in exts:
                default_index = 3
            elif ".archive" in exts:
                default_index = 0
        combo.setCurrentIndex(default_index)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return None

        chosen = combo.currentData()
        if chosen == "__custom__":
            text, ok = QtWidgets.QInputDialog.getText(
                self, "Custom location",
                "Path relative to your game folder\n(e.g. archive/pc/mod):"
            )
            if not ok or not text.strip():
                return None
            # Normalise separators; keep it relative and inside the game dir.
            rel = text.strip().replace("\\", "/").strip("/")
            if not rel or ".." in rel.split("/"):
                QtWidgets.QMessageBox.warning(self, "Invalid path",
                                              "That path isn't valid.")
                return None
            return rel

        return chosen

    def _copy_tree_with_prompt(self, source: Path, dest_base: Path, mod_name: str = ""):
        """Copy every file under `source` into `dest_base`, asking what to do
        when a destination file already exists.

        `mod_name` is shown on the overwrite prompt so it's clear which mod
        the conflict belongs to (handy for looking the mod up on Nexus).

        Returns the list of installed paths (relative to game_path), or None
        if the user cancelled the whole install. The overwrite decision is
        remembered across files via self._overwrite_decision so "Yes to All"
        / "No to All" work.
        """
        files_copied = []

        for root, _dirs, files in os.walk(source):
            rel_root = Path(root).relative_to(source)
            dest_root = dest_base / rel_root
            dest_root.mkdir(parents=True, exist_ok=True)

            for f in files:
                src_file = Path(root) / f
                dest_file = dest_root / f

                if dest_file.exists():
                    decision = self._resolve_overwrite(dest_file, mod_name)
                    if decision == "cancel":
                        return None
                    if decision == "skip":
                        continue
                    # else "overwrite": fall through and copy

                try:
                    shutil.copy2(src_file, dest_file)
                    files_copied.append(str(dest_file.relative_to(self.game_path)))
                except Exception as e:
                    print(f"[ERROR] Failed to copy {src_file}: {e}")

        return files_copied

    def _resolve_overwrite(self, dest_file: Path, mod_name: str = "") -> str:
        """Decide whether to overwrite an existing file. Returns
        "overwrite", "skip", or "cancel". Honours a remembered
        Yes-to-All / No-to-All choice for the current install.

        `mod_name` is displayed prominently so the user knows which mod is
        being installed when a conflict comes up."""
        if self._overwrite_decision == "all":
            return "overwrite"
        if self._overwrite_decision == "none":
            return "skip"

        try:
            rel = dest_file.relative_to(self.game_path)
        except ValueError:
            rel = dest_file.name

        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("File already exists")
        box.setIcon(QtWidgets.QMessageBox.Icon.Question)
        heading = f"<b>Installing: {mod_name}</b><br><br>" if mod_name else ""
        box.setText(
            f"{heading}This file already exists:\n\n{rel}\n\nReplace it?"
        )
        yes = box.addButton("Replace", QtWidgets.QMessageBox.ButtonRole.YesRole)
        yes_all = box.addButton("Replace All", QtWidgets.QMessageBox.ButtonRole.YesRole)
        no = box.addButton("Skip", QtWidgets.QMessageBox.ButtonRole.NoRole)
        no_all = box.addButton("Skip All", QtWidgets.QMessageBox.ButtonRole.NoRole)
        cancel = box.addButton("Cancel Install", QtWidgets.QMessageBox.ButtonRole.RejectRole)
        box.exec()

        clicked = box.clickedButton()
        if clicked == yes:
            return "overwrite"
        if clicked == yes_all:
            self._overwrite_decision = "all"
            return "overwrite"
        if clicked == no:
            return "skip"
        if clicked == no_all:
            self._overwrite_decision = "none"
            return "skip"
        if clicked == cancel:
            return "cancel"
        return "skip"  # dialog closed some other way: safest default

    def install_from_folder(self, folder: Path, archive_path=""):
        if not self.game_path:
            QtWidgets.QMessageBox.warning(self, "Error", "Set game path first")
            return

        # Prefer the original archive's name over the folder's. When the
        # install comes from the worker, `folder` is a temp dir like
        # "cp2077_install_xxxx", which would otherwise show up as the mod
        # name in the picker and the installed list.
        if archive_path:
            mod_name = Path(archive_path).stem
        else:
            mod_name = folder.name

        # A FOMOD archive has to be handled before normal detection: it ships
        # several named option folders ("Main Files", "Optional - 4K") and an
        # XML saying which to install and where their CONTENTS go. Detection
        # would otherwise see the option folders as content and copy all of
        # them -- plus the fomod/ metadata -- into the game directory.
        if self._try_install_fomod(folder, mod_name, archive_path):
            return

        source_root = self._unwrap_wrapper(folder)

        mod_type, actual_source, dest_relative, reason = detect_mod_type(source_root)

        print(f"[INSTALL] Mod: {mod_name}")
        print(f"[INSTALL] Type: {mod_type}")

        # Show the destination picker when the mod is either unrecognised OR
        # just loose files with no game-folder structure of its own. A single
        # ".tweak" or ".archive" sitting on its own could belong in more than
        # one place, and the author didn't fold it into archive/ or r6/ to
        # tell us -- so the user chooses, with our best guess pre-selected.
        needs_picker = (mod_type == "Unknown") or self._is_loose_files(source_root)

        if needs_picker:
            suggested = None if mod_type == "Unknown" else str(dest_relative)
            chosen_dest = self._ask_install_destination(
                mod_name, source_root, reason, suggested=suggested
            )
            if chosen_dest is None:
                print("[INSTALL] Cancelled at destination picker")
                return
            dest_relative = Path(chosen_dest)

        mod_id = generate_mod_id(actual_source)

        # One overwrite decision state for the whole install, so "Yes to
        # All" / "No to All" during a multi-file copy is remembered.
        self._overwrite_decision = None  # None until the first conflict asks

        if str(dest_relative) == ".":
            files_copied = self._copy_tree_with_prompt(actual_source, self.game_path, mod_name)
        else:
            dest_base = self.game_path / dest_relative
            dest_base.mkdir(parents=True, exist_ok=True)
            files_copied = self._copy_tree_with_prompt(actual_source, dest_base, mod_name)

        if files_copied is None:
            # User cancelled the whole install from an overwrite prompt.
            print("[INSTALL] Cancelled during copy")
            return

        if not files_copied:
            QtWidgets.QMessageBox.warning(self, "Warning", "No files were installed")
            return

        max_order = max((rec.order for rec in self.state.values()), default=0)

        self.state[mod_name] = ModRecord(
            name=mod_name,
            mod_id=mod_id,
            mod_type=mod_type,
            installed_at=time.time(),
            order=max_order + 1,
            files=files_copied,
            archive_path=archive_path
        )
        save_state(self.state)
        self.refresh_installed()

        print(f"[SUCCESS] Installed {len(files_copied)} files")

    def extract_and_install_archive(self, archive_path: Path):
        """Extract archive and all nested archives, then install using install_from_folder"""
        # Use permanent temp folder in script directory
        script_dir = Path(sys.argv[0]).parent
        tmpdir = script_dir / "temp_mod_extract"
        tmpdir.mkdir(parents=True, exist_ok=True)

        # Create a unique subfolder for this extraction
        extract_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        work_dir = tmpdir / extract_id

        # Clean up any old extractions (older than 1 hour)
        try:
            import time as time_module
            current_time = time_module.time()
            for old_dir in tmpdir.iterdir():
                if old_dir.is_dir():
                    dir_time = old_dir.stat().st_mtime
                    if current_time - dir_time > 3600:  # 1 hour old
                        shutil.rmtree(old_dir, ignore_errors=True)
        except:
            pass

        work_dir.mkdir(parents=True, exist_ok=True)
        print(f"[EXTRACT] Using temp directory: {work_dir}")

        try:
            # Extract main archive
            try:
                if archive_path.suffix.lower() == ".zip":
                    with zipfile.ZipFile(archive_path) as z:
                        z.extractall(work_dir)
                    print(f"[EXTRACT] Extracted zip to {work_dir}")
                elif archive_path.suffix.lower() == ".rar":
                    if not HAS_RARFILE:
                        QtWidgets.QMessageBox.critical(self, "Error", "rarfile library not available")
                        return
                    with rarfile.RarFile(archive_path) as r:
                        r.extractall(work_dir)
                    print(f"[EXTRACT] Extracted rar to {work_dir}")
                elif archive_path.suffix.lower() == ".7z":
                    if not HAS_PY7ZR:
                        QtWidgets.QMessageBox.critical(self, "Error", "py7zr library not available")
                        return
                    with py7zr.SevenZipFile(archive_path) as z:
                        z.extractall(work_dir)
                    print(f"[EXTRACT] Extracted 7z to {work_dir}")
                else:
                    QtWidgets.QMessageBox.critical(self, "Error", f"Unsupported archive format: {archive_path.suffix}")
                    return
            except Exception as e:
                QtWidgets.QMessageBox.critical(self, "Error", f"Failed to extract archive: {e}")
                print(f"[ERROR] Extraction failed: {e}")
                return

            # Extract any nested archives in place
            self._extract_nested_archives(work_dir)

            # List contents for debugging
            print(f"[DEBUG] Contents of work directory:")
            for item in work_dir.rglob("*"):
                print(f"  {item.relative_to(work_dir)}")

            # Install from the extracted folder
            print(f"[INSTALL] Installing from extracted archive: {work_dir}")
            self.install_from_folder(work_dir, archive_path=str(archive_path))

        finally:
            # Clean up work directory and any orphaned folders
            if work_dir and work_dir.exists():
                try:
                    shutil.rmtree(work_dir)
                    print(f"[CLEANUP] Removed work directory: {work_dir}")
                except Exception as e:
                    print(f"[CLEANUP] Failed to remove work directory: {e}")

            # Also clean up any extracting folders from game directory
            self._cleanup_extracting_folders(self.game_path)

    def _extract_nested_archives(self, folder: Path, max_depth: int = 5, current_depth: int = 0):
        """Recursively extract nested archives in place (replaces archive with extracted contents)"""
        if current_depth >= max_depth:
            return

        nested_archives = []
        for nested_file in list(folder.rglob("*")):
            if nested_file.is_file() and nested_file.suffix.lower() in [".zip", ".rar", ".7z"]:
                nested_archives.append(nested_file)

        for nested_file in nested_archives:
            try:
                print(f"[EXTRACT] Found nested archive: {nested_file.relative_to(folder)}")

                # Extract directly to parent directory
                parent_dir = nested_file.parent

                if nested_file.suffix.lower() == ".zip":
                    with zipfile.ZipFile(nested_file) as z:
                        z.extractall(parent_dir)
                elif nested_file.suffix.lower() == ".rar":
                    if HAS_RARFILE:
                        with rarfile.RarFile(nested_file) as r:
                            r.extractall(parent_dir)
                    else:
                        print(f"[SKIP] rarfile not available for: {nested_file.name}")
                        continue
                elif nested_file.suffix.lower() == ".7z":
                    if HAS_PY7ZR:
                        with py7zr.SevenZipFile(nested_file) as z:
                            z.extractall(parent_dir)
                    else:
                        print(f"[SKIP] py7zr not available for: {nested_file.name}")
                        continue

                print(f"[EXTRACT] Extracted {nested_file.name}")

                # Delete the archive file
                nested_file.unlink()
                print(f"[EXTRACT] Deleted archive: {nested_file.name}")

                # Recursively extract any archives in the parent directory (but skip the one we just deleted)
                self._extract_nested_archives(parent_dir, max_depth, current_depth + 1)

            except Exception as e:
                print(f"[ERROR] Failed to extract nested archive {nested_file.name}: {e}")
                continue

    def _cleanup_extracting_folders(self, folder: Path):
        """Remove any _extracting_ folders left behind"""
        try:
            for extracting_dir in folder.rglob("_extracting_*"):
                if extracting_dir.is_dir():
                    shutil.rmtree(extracting_dir)
                    print(f"[CLEANUP] Removed orphaned folder: {extracting_dir}")
        except Exception as e:
            print(f"[CLEANUP] Error removing extracting folders: {e}")

    def _unwrap_wrapper(self, folder: Path) -> Path:
        """Descend past wrapper folders to the real mod root.

        Mod archives are routinely wrapped in a folder named after the mod
        ("Cool Mod Name/red4ext/..."). Installing that folder as-is puts
        <game>/Cool Mod Name/red4ext/ on disk, where the game never looks.

        The previous version only descended when a folder had EXACTLY one
        child directory, so a wrapper sitting next to a README.txt or a
        Docs/ folder was left in place. This version ignores documentation
        siblings and loose files when deciding, so those common layouts
        unwrap correctly too.
        """
        return _unwrap_mod_wrapper(folder)

    def delete_archived_archive(self):
        archive_name = self.archived_table.selected_archive()
        if not archive_name or not self.archive_folder:
            return

        reply = QtWidgets.QMessageBox.question(
            self, "Confirm", f"Delete '{archive_name}' from archived location?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No
        )

        if reply == QtWidgets.QMessageBox.StandardButton.Yes:
            candidates = list(self.archive_folder.rglob(archive_name))
            for archive_path in candidates:
                if archive_path.exists():
                    try:
                        archive_path.unlink()
                    except Exception as e:
                        print(f"Failed to delete {archive_name}: {e}")
            self.refresh_archived()

    def move_selected_archive_to_not_installed(self):
        archive_name = self.archived_table.selected_archive()
        if not archive_name or not self.archive_folder:
            return

        source_path = None
        for ext in ['.zip', '.rar', '.7z']:
            candidates = list(self.archive_folder.rglob(archive_name))
            if candidates:
                source_path = candidates[0]
                break

        if source_path and source_path.exists():
            try:
                dest_path = self.mod_library / archive_name
                shutil.move(str(source_path), str(dest_path))
                self.refresh_archived()
                self.refresh_not_installed()
                QtWidgets.QMessageBox.information(self, "Success", f"Moved '{archive_name}' to not installed")
            except Exception as e:
                QtWidgets.QMessageBox.critical(self, "Error", f"Failed to move archive: {e}")

    def _on_install_complete(self, mod_name: str, files_copied: list, archive_path: str):
        max_order = max((rec.order for rec in self.state.values()), default=0)
        mod_type = "Archive"

        mod_id = hashlib.sha256("".join(sorted(files_copied)).encode()).hexdigest()[:12]

        self.state[mod_name] = ModRecord(
            name=mod_name, mod_id=mod_id, mod_type=mod_type,
            installed_at=time.time(), order=max_order + 1,
            files=files_copied, archive_path=archive_path
        )
        save_state(self.state)
        self.refresh_installed()
        self.refresh_not_installed()

        QtWidgets.QMessageBox.information(self, "Success",
            f"Installed '{mod_name}'\n{len(files_copied)} files copied")

    def _on_install_error(self, error_msg: str):
        # During a batch, don't block on a popup for every failure -- log it
        # and keep going, then let the batch summary stand. A single install
        # still shows the dialog.
        in_batch = getattr(self, "_install_queue_total", 0) > 1
        print(f"[INSTALL] Error: {error_msg}")
        if not in_batch:
            QtWidgets.QMessageBox.critical(self, "Installation Error", error_msg)
        self._advance_install_queue()

# --- CLI (headless) ---
def _cli_game_path(args) -> Path:
    if getattr(args, "game", None):
        return Path(args.game)
    st = load_settings()
    gp = st.get("game_path", "")
    if gp:
        return Path(gp)
    # detect
    for p in COMMON_GAME_DIRS:
        if (p / "r6").exists():
            return p
    raise SystemExit("No game path: pass --game or set settings.json game_path")

def _match_records(state, substr):
    low = substr.lower()
    matches = [n for n in state if low in n.lower()]
    return matches

def cmd_list(args):
    gp = _cli_game_path(args)
    state = load_state()
    items = sorted(state.items(), key=lambda x: (x[1].order if x[1].order is not None else 0, x[0]))
    print(f"{'Order':>5}  {'Name':<40}  {'Type':<20}  {'Status'}")
    print("-" * 90)
    for name, rec in items:
        if rec.mod_type.startswith("DISABLED"):
            status = "DISABLED"
        else:
            missing = [f for f in rec.files if not (gp / f).exists()]
            status = "OK" if not missing else f"BROKEN({len(missing)})"
        print(f"{rec.order:>5}  {name[:40]:<40}  {rec.mod_type[:20]:<20}  {status}")
    print(f"\n{len(state)} records, game={gp}")

def cmd_verify(args):
    gp = _cli_game_path(args)
    state = load_state()
    broken = 0
    for name, rec in state.items():
        if rec.mod_type.startswith("DISABLED"):
            continue
        missing = [f for f in rec.files if not (gp / f).exists()]
        if missing:
            broken += 1
            print(f"BROKEN: {name} ({len(missing)} missing)")
            for m in missing[:5]:
                print(f"  - {m}")
    if broken:
        print(f"\n{broken} broken record(s)")
        raise SystemExit(1)
    print(f"All {len(state)} records verify OK")

def _move_files(gp, rec, to_disabled: bool):
    name = rec.name
    disabled_root = gp / "mods" / "_disabled" / name
    moved = 0
    for f in rec.files:
        if not isinstance(f, str) or not f:
            continue
        src = (disabled_root / f) if to_disabled is False else (gp / f)
        dst = (gp / f) if to_disabled is False else (disabled_root / f)
        if not src.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dst))
            moved += 1
        except Exception as e:
            print(f"  failed move {src}: {e}")
    # cleanup empty disabled dir on enable
    if not to_disabled:
        try:
            if disabled_root.exists() and not any(disabled_root.rglob("*")):
                disabled_root.rmdir()
        except Exception:
            pass
    return moved

def cmd_disable(args):
    gp = _cli_game_path(args)
    state = load_state()
    matches = _match_records(state, args.name)
    if len(matches) != 1:
        print("Matches:", matches)
        raise SystemExit("expected exactly one match" if matches else "no match")
    name = matches[0]
    rec = state[name]
    if rec.mod_type.startswith("DISABLED"):
        print("Already disabled")
        return
    original_type = rec.mod_type
    moved = _move_files(gp, rec, True)
    rec.mod_type = "DISABLED — " + original_type
    save_state(state)
    print(f"Disabled {name}: moved {moved} files to {gp/'mods'/'_disabled'/name}")

def cmd_enable(args):
    gp = _cli_game_path(args)
    state = load_state()
    matches = _match_records(state, args.name)
    if len(matches) != 1:
        print("Matches:", matches)
        raise SystemExit("expected exactly one match" if matches else "no match")
    name = matches[0]
    rec = state[name]
    if not rec.mod_type.startswith("DISABLED"):
        print("Already enabled")
        return
    moved = _move_files(gp, rec, False)
    rec.mod_type = rec.mod_type[len("DISABLED — "):] if rec.mod_type.startswith("DISABLED — ") else rec.mod_type.replace("DISABLED — ", "", 1)
    save_state(state)
    print(f"Enabled {name}: moved {moved} files back")

# --- Main Entry ---
if __name__ == "__main__":
    import argparse
    # Back-compat: `cp2077_mod_manager.py <gamedir>` launches GUI (ignored, settings used)
    if len(sys.argv) > 1 and sys.argv[1] not in ("list","verify","enable","disable","-h","--help"):
        # GUI mode
        app = QtWidgets.QApplication(sys.argv)
        win = MainWindow()
        win.show()
        sys.exit(app.exec())
    parser = argparse.ArgumentParser(prog="cp2077_mod_manager")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.add_argument("--game", default=None); p.set_defaults(func=cmd_list)
    p = sub.add_parser("verify"); p.add_argument("--game", default=None); p.set_defaults(func=cmd_verify)
    p = sub.add_parser("disable"); p.add_argument("name"); p.add_argument("--game", default=None); p.set_defaults(func=cmd_disable)
    p = sub.add_parser("enable"); p.add_argument("name"); p.add_argument("--game", default=None); p.set_defaults(func=cmd_enable)
    args = parser.parse_args()
    args.func(args)
