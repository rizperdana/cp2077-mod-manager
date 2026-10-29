#!/usr/bin/env python3
"""cpmm — Cyberpunk 2077 mod manager: one stdlib-only file (TUI + CLI)."""
from __future__ import annotations
import argparse, json, os, re, shutil, subprocess, sys, time
import datetime, tempfile, zipfile
from pathlib import Path

APP = "cpmm"
DEFAULT_STATE_DIR = Path.home() / ".local/share/cp2077-mod-manager"

THEME = {
    "fg": "\x1b[38;5;252m",
    "muted": "\x1b[38;5;244m",
    "border": "\x1b[38;5;238m",
    "accent": "\x1b[38;5;81m",
    "success": "\x1b[38;5;114m",
    "warning": "\x1b[38;5;221m",
    "error": "\x1b[38;5;203m",
    "reset": "\x1b[0m",
    "reverse": "\x1b[7m",
    "clear": "\x1b[2J\x1b[H",
    "h": "\u2500",
    "bar": "\u258c",
    "ellipsis": "\u2026",
}

_COLOR = True


def set_color(on):
    global _COLOR
    _COLOR = bool(on)


def c(key, text):
    if not _COLOR or key not in THEME:
        return text
    return f"{THEME[key]}{text}{THEME['reset']}"


def rule(width):
    return c("border", THEME["h"] * width)


def header(game_base, n_mods, n_ok):
    left = c("accent", "cpmm")
    right = c("muted", f"{game_base} \u00b7 {n_mods} mods \u00b7 {n_ok} ok")
    width = shutil.get_terminal_size((100, 24)).columns
    gap = max(2, width - len("cpmm") - len(f"{game_base} \u00b7 {n_mods} mods \u00b7 {n_ok} ok"))
    return f"{left}{' ' * gap}{right}"


def footer(hint):
    return c("muted", hint)


def term_width():
    return shutil.get_terminal_size((100, 24)).columns


def trunc(s, n):
    if len(s) <= n:
        return s
    return s[: max(1, n - 1)] + THEME["ellipsis"]


def err_line(msg):
    return c("error", msg)


def warn_line(msg):
    return c("warning", msg)


def ok_line(msg):
    return c("success", msg)


# ---- state paths ----
def state_dir():
    return Path(os.environ.get("CPMM_STATE_DIR") or str(DEFAULT_STATE_DIR))


def state_file():
    return state_dir() / "mods.json"


def settings_file():
    return state_dir() / "settings.json"


def load_settings():
    p = settings_file()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_settings(d):
    p = settings_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
    if p.exists():
        p.replace(p.with_name(p.name + ".bak"))
    os.replace(tmp, p)


def _valid_record(rec):
    if not isinstance(rec, dict):
        return False
    for key in ("name", "mod_id", "mod_type", "installed_at", "order"):
        if key not in rec:
            return False
    if not isinstance(rec.get("files"), list):
        return False
    if not rec.get("name") or not isinstance(rec.get("mod_id"), str):
        return False
    return True


def load_state():
    p = state_file()
    bak = state_dir() / "mods.json.bak"
    raw = None
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            if bak.exists():
                try:
                    raw = json.loads(bak.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    raw = None
    if not isinstance(raw, dict):
        raw = {}
    records = {}
    for key, rec in raw.items():
        if not isinstance(rec, dict):
            continue
        if not _valid_record(rec):
            continue
        rec.setdefault("category", "")
        rec.setdefault("archive_path", "")
        if not rec.get("category"):
            rec["category"] = _category_for_name(rec.get("name", key))
        records[key] = rec
    # migration: fill category where missing in source (handled above by _category_for_name default 'misc' for unknown)
    # ensure category field present even if was None
    changed = False
    # detect whether any record lacked category originally (we can't know; always rewrite if category added)
    # We only save if a category was newly assigned OR missing field.
    # Simple: compare to raw on-disk
    try:
        ondisk = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except json.JSONDecodeError:
        ondisk = {}
    needs_save = False
    for key, rec in records.items():
        src = ondisk.get(key, {}) if isinstance(ondisk, dict) else {}
        if "category" not in src:
            needs_save = True
    if needs_save and records:
        # backup previous
        if p.exists() and not bak.exists():
            try:
                shutil.copy2(p, bak)
            except OSError:
                pass
        save_state(records)
    return records


def save_state(records):
    p = state_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    bak = state_dir() / "mods.json.bak"
    if p.exists():
        try:
            shutil.copy2(p, bak)
        except OSError:
            pass
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(records, indent=2), encoding="utf-8")
    os.replace(tmp, p)


# ---- category migration map ----
CATEGORY_MAP = {
    "CET": "framework", "RED4ext": "framework", "redscript": "framework",
    "ArchiveXL": "framework", "TweakXL": "framework", "Codeware": "framework",
    "BrowserExtensionFramework": "framework", "Native Settings UI": "framework",
    "Mod Settings": "framework", "Equipment-EX": "framework", "Cyberware-EX": "framework",
    "Input Loader": "framework",
}


def _soft(s):
    return re.sub(r"[\s_\-\(\)\[\]']+", " ", str(s).lower()).strip()


def _runs(s):
    return re.findall(r"\d+", _soft(s))


def _subseq(qr, nr):
    if not qr:
        return True
    if len(qr) > len(nr):
        return False
    return any(nr[i:i + len(qr)] == qr for i in range(len(nr) - len(qr) + 1))


def _score(query, name):
    q = query.strip().lower()
    n = str(name).lower()
    if n == q:
        return 100.0
    qs = _soft(q)
    ns = _soft(n)
    if ns == qs:
        return 90.0
    pts = 0.0
    if ns.startswith(qs):
        pts += 6
    elif qs in ns:
        pts += 4
    words = qs.split()
    if words:
        hits = sum(1 for w in words if w in ns)
        pts += 3.0 * hits / len(words)
    qr = _runs(qs)
    nr = _runs(ns)
    if qr and _subseq(qr, nr):
        pts += 3
    return pts


def resolve_record(query, records):
    # exact name key or mod_id
    for key, rec in records.items():
        if key == query or rec.get("name") == query or rec.get("mod_id") == query:
            return key
    # case-insensitive mod_id
    for key, rec in records.items():
        if str(rec.get("mod_id", "")).lower() == query.lower():
            return key
    scored = []
    for key, rec in records.items():
        s = _score(query, rec.get("name", key))
        scored.append((s, key))
    scored.sort(reverse=True)
    if not scored or scored[0][0] < 4.5:
        raise ValueError(f"no mod matches '{query}' — run: cpmm list")
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        raise ValueError(f"ambiguous '{query}' — run: cpmm list")
    return scored[0][1]


def _category_for_name(name):
    # resolve via fuzzy match against CATEGORY_MAP keys
    best = None
    for q, cat in CATEGORY_MAP.items():
        s = _score(q, name)
        if best is None or s > best[0]:
            best = (s, cat)
    if best and best[0] >= 4.5:
        return best[1]
    return "misc"


# ---- game dir ----
def game_path():
    s = load_settings()
    gp = s.get("game_path")
    if gp:
        p = Path(gp)
        if (p / "bin/x64/Cyberpunk2077.exe").exists():
            return p
    env = os.environ.get("CP2077_GAME")
    if env:
        p = Path(env)
        if (p / "bin/x64/Cyberpunk2077.exe").exists():
            return p
    return None


def detect_candidates():
    results = []
    seen = set()
    def add(src, path):
        try:
            p = Path(path).expanduser()
        except Exception:
            return
        try:
            key = str(p.resolve())
        except OSError:
            key = str(p)
        if key in seen:
            return
        seen.add(key)
        exe = p / "bin/x64/Cyberpunk2077.exe"
        results.append({"src": src, "path": p, "valid": exe.exists()})
    s = load_settings().get("game_path")
    if s:
        add("settings", s)
    env = os.environ.get("CP2077_GAME")
    if env:
        add("env", env)
    home = Path.home()
    for exe in sorted(home.glob("Games/**/Cyberpunk 2077/bin/x64/Cyberpunk2077.exe")):
        game = exe.parents[2]
        tag = "heroic" if "Heroic" in str(game) else ("steam" if "steamapps" in str(game) else "lutris")
        add(tag, game)
    for fixed in (
        home / ".steam/steam/steamapps/common/Cyberpunk 2077",
        home / ".local/share/Steam/steamapps/common/Cyberpunk 2077",
        home / ".steam/root/steamapps/common/Cyberpunk 2077",
    ):
        add("steam", fixed)
    return results


def do_detect():
    cands = detect_candidates()
    lines = []
    lines.append("Candidates:")
    for i, cd in enumerate(cands, 1):
        mark = "ok" if cd["valid"] else "no exe"
        lines.append(f"  {i}. [{cd['src']}] {cd['path']}  ({mark})")
    chosen = None
    for cd in cands:
        if cd["valid"]:
            chosen = cd["path"]
            break
    if chosen is None:
        lines.append("No valid game dir found.")
    else:
        lines.append(f"Chosen: {chosen}")
        s = load_settings()
        s["game_path"] = str(chosen)
        save_settings(s)
    return "\n".join(lines), chosen


def set_game(dirpath):
    p = Path(dirpath).expanduser()
    if not (p / "bin/x64/Cyberpunk2077.exe").exists():
        raise ValueError(f"not a valid game dir (missing bin/x64/Cyberpunk2077.exe): {p}")
    s = load_settings()
    s["game_path"] = str(p)
    save_settings(s)
    return f"Game dir set: {p}"


# ---- status helpers ----
def is_disabled(rec):
    if rec.get("status") == "disabled":
        return True
    if str(rec.get("mod_type", "")).upper().startswith("DISABLED"):
        return True
    return False


def status_of(rec, game):
    if is_disabled(rec):
        return "DISABLED"
    files = rec.get("files", [])
    present = sum(1 for f in files if (game / f).exists())
    if not files:
        return "OK"
    if present == len(files):
        return "OK"
    if present == 0:
        # parked?
        if (game / "mods/_disabled").exists():
            return "DISABLED"
        return "BROKEN"
    return "BROKEN"


def parked_count(rec, game):
    disabled_dir = game / "mods/_disabled"
    if not disabled_dir.exists():
        return 0
    bases = set()
    for pp in disabled_dir.rglob("*"):
        if pp.is_file():
            name = pp.name
            if name.endswith(".disabled"):
                name = name[:-9]
            bases.add(name)
    n = 0
    for f in rec.get("files", []):
        if Path(f).name in bases:
            n += 1
    return n


def file_size_total(rec, game):
    total = 0
    for f in rec.get("files", []):
        p = game / f
        if p.exists():
            try:
                total += p.stat().st_size
            except OSError:
                pass
        else:
            # disabled parked
            pass
    return total


# ---- install ----
def _classify(relpaths):
    types = []
    cet = any(rp.startswith("bin/x64/plugins/cyber_engine_tweaks/") or rp.endswith(".lua") for rp in relpaths)
    red4 = any(re.search(r"red4ext/plugins/.*\.dll$", rp) for rp in relpaths)
    redscript = any(rp.endswith(".reds") for rp in relpaths)
    tweak = any(rp.startswith("r6/tweaks/") and rp.endswith((".yaml", ".yml", ".tweak")) for rp in relpaths)
    axl = any(rp.startswith("archive/pc/mod/") and rp.endswith(".archive.xl") for rp in relpaths)
    arch = any(rp.endswith(".archive") for rp in relpaths)
    if cet:
        types.append("CET")
    if red4:
        types.append("RED4ext")
    if redscript:
        types.append("redscript")
    if tweak:
        types.append("TweakXL")
    if axl:
        types.append("ArchiveXL")
    if arch and "ArchiveXL" not in types:
        types.append("Archive")
    return "+".join(types) if types else "Archive"


def _unwrap(root):
    # descend single wrapper dir when it contains game markers
    cur = root
    markers = {"bin", "r6", "red4ext", "archive", "engine"}
    for _ in range(3):
        entries = [p for p in cur.iterdir()]
        dirs = [p for p in entries if p.is_dir()]
        if len(entries) == 1 and len(dirs) == 1 and not (cur / "bin").exists() and not (cur / "r6").exists():
            if dirs[0].name not in markers:
                cur = dirs[0]
                continue
        break
    return cur


def _extract_archive(src, dest):
    src = Path(src)
    suf = src.suffix.lower()
    dest.mkdir(parents=True, exist_ok=True)
    if suf == ".zip":
        with zipfile.ZipFile(src) as z:
            z.extractall(dest)
        return
    if suf in (".7z",):
        binp = shutil.which("7z") or shutil.which("7za")
        if not binp:
            raise FileNotFoundError("7z binary not found — install p7zip to open .7z archives")
        subprocess.run([binp, "x", "-y", f"-o{dest}", str(src)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    if suf in (".rar",):
        binp = shutil.which("unrar")
        if not binp:
            raise FileNotFoundError("unrar binary not found — install unrar to open .rar archives")
        subprocess.run([binp, "x", "-o+", str(src), str(dest) + "/"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    raise ValueError(f"unsupported archive type: {src.suffix}")


def _collect_files(root):
    out = []
    for p in root.rglob("*"):
        if p.is_file():
            # skip mac junk
            if "__MACOSX" in str(p):
                continue
            if p.name.startswith("._"):
                continue
            out.append(p.relative_to(root).as_posix())
    return out


def do_install(src, category, game):
    src = Path(src).expanduser()
    if not src.exists():
        raise FileNotFoundError(f"source not found: {src}")
    records = load_state()
    name = src.stem if src.is_file() else src.name
    if name in records:
        raise ValueError(f"already installed '{name}' — uninstall first")
    tmp = Path(tempfile.mkdtemp(prefix="cpmm_"))
    try:
        if src.is_dir():
            staging = src
            # copy into tmp to normalize
            staging = tmp / "staged"
            shutil.copytree(src, staging)
        else:
            staging = tmp / "staged"
            staging.mkdir()
            _extract_archive(src, staging)
        staging = _unwrap(staging)
        rels = _collect_files(staging)
        if not rels:
            raise ValueError("no files found in source")
        mod_type = _classify(rels)
        # copy files
        overwrite = load_settings().get("overwrite_files", True)
        for rel in rels:
            dest = game / rel
            if dest.exists() and not overwrite:
                raise ValueError(f"file exists (overwrite disabled): {rel}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staging / rel, dest)
        order = 0
        for r in records.values():
            if isinstance(r.get("order"), int):
                order = max(order, r["order"])
        order += 1
        rec = {
            "name": name,
            "mod_id": name,
            "mod_type": mod_type,
            "installed_at": time.time(),
            "order": order,
            "files": rels,
            "archive_path": str(src),
            "category": category or "misc",
        }
        records[name] = rec
        save_state(records)
        return f"Installed '{name}' ({mod_type}, order {order}, {len(rels)} files)"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- uninstall ----
def do_uninstall(name, game, yes=False, interactive=True):
    records = load_state()
    key = resolve_record(name, records)
    rec = records[key]
    if not yes and interactive:
        print(f"Uninstall '{rec['name']}' ({rec.get('category','')}, {len(rec.get('files', []))} files)? [y/N] ", end="", flush=True)
        ans = input().strip().lower()
        if ans not in ("y", "yes"):
            raise ValueError("cancelled")
    # delete tracked files (original paths)
    for rel in rec.get("files", []):
        p = game / rel
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass
    # disabled parked canonical
    park = game / "mods/_disabled" / rec["name"]
    if park.exists():
        shutil.rmtree(park, ignore_errors=True)
    # legacy basename parked files
    disabled_dir = game / "mods/_disabled"
    if disabled_dir.exists() and is_disabled(rec):
        bases = {Path(f).name for f in rec.get("files", [])}
        for pp in list(disabled_dir.rglob("*")):
            if pp.is_file() and pp.name in bases or (pp.name[:-9] if pp.name.endswith(".disabled") else pp.name) in bases:
                try:
                    pp.unlink()
                except OSError:
                    pass
    # remove empty dirs
    for root in (game, disabled_dir):
        if root.exists():
            for d in sorted(root.rglob("*"), key=lambda x: len(str(x)), reverse=True):
                if d.is_dir() and d != root:
                    try:
                        next(d.iterdir())
                    except StopIteration:
                        d.rmdir()
    del records[key]
    save_state(records)
    return f"Uninstalled '{rec['name']}'"


# ---- enable/disable ----
def do_enable(name, game):
    records = load_state()
    key = resolve_record(name, records)
    rec = records[key]
    if not is_disabled(rec):
        # maybe files present
        files = rec.get("files", [])
        if all((game / f).exists() for f in files):
            return f"'{rec['name']}' is already enabled"
    # canonical parked
    park = game / "mods/_disabled" / rec["name"]
    precheck = []
    for rel in rec.get("files", []):
        src = park / (rel + ".disabled")
        if not src.exists():
            # maybe parked without suffix? try both
            alt = park / rel
            if alt.exists():
                src = alt
            else:
                raise ValueError(f"parked file missing: {rel} — cannot restore (legacy parking?)")
        dest = game / rel
        if dest.exists() and not load_settings().get("overwrite_files", True):
            raise ValueError(f"destination exists: {rel}")
        precheck.append((src, dest))
    for src, dest in precheck:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
    # remove empty park dirs
    if park.exists():
        for d in sorted(park.rglob("*"), key=lambda x: len(str(x)), reverse=True):
            if d.is_dir():
                try:
                    next(d.iterdir())
                except StopIteration:
                    d.rmdir()
        try:
            park.rmdir()
        except OSError:
            pass
    rec["status"] = "enabled"
    # remove DISABLED marker from mod_type if present
    if str(rec.get("mod_type", "")).upper().startswith("DISABLED"):
        rec["mod_type"] = "misc"
    save_state(records)
    return f"Enabled '{rec['name']}'"


def do_disable(name, game):
    records = load_state()
    key = resolve_record(name, records)
    rec = records[key]
    if is_disabled(rec):
        return f"'{rec['name']}' is already disabled"
    files = rec.get("files", [])
    # precheck all present
    for rel in files:
        if not (game / rel).exists():
            raise ValueError(f"file missing, cannot disable: {rel}")
    park = game / "mods/_disabled" / rec["name"]
    # precheck dest free
    for rel in files:
        dest = park / (rel + ".disabled")
        if dest.exists():
            raise ValueError(f"parked file already exists: {rel}")
    # move
    for rel in files:
        src = game / rel
        dest = park / (rel + ".disabled")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
    # remove empty src dirs
    for rel in files:
        d = (game / rel).parent
        while d != game and d.exists():
            try:
                next(d.iterdir())
                break
            except StopIteration:
                d.rmdir()
                d = d.parent
    rec["status"] = "disabled"
    save_state(records)
    return f"Disabled '{rec['name']}'"


# ---- category ----
def do_category(name, category):
    records = load_state()
    key = resolve_record(name, records)
    records[key]["category"] = category
    save_state(records)
    return f"Set category '{category}' for '{records[key]['name']}'"


# ---- meta ----
def do_meta(name, game):
    records = load_state()
    key = resolve_record(name, records)
    rec = records[key]
    st = status_of(rec, game)
    lines = []
    lines.append(f"Name: {rec['name']}")
    lines.append(f"mod_id: {rec.get('mod_id')}")
    lines.append(f"category: {rec.get('category')}")
    lines.append(f"type: {rec.get('mod_type')}")
    lines.append(f"order: {rec.get('order')}")
    lines.append(f"status: {st}")
    ts = rec.get("installed_at")
    try:
        dt = datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        dt = ts
    lines.append(f"installed_at: {dt}")
    lines.append(f"archive_path: {rec.get('archive_path')}")
    files = rec.get("files", [])
    lines.append(f"files: {len(files)} ({sum(1 for f in files if (game/f).exists() or parked_count(rec,game))} ok-ish)")
    # better: ok count
    ok = sum(1 for f in files if (game / f).exists())
    if is_disabled(rec):
        ok = parked_count(rec, game)
    lines[-1] = f"files: {len(files)} ({ok} ok)"
    for f in files:
        p = game / f
        mark = "OK" if p.exists() else ("PARKED" if is_disabled(rec) and Path(f).name in {x.name[:-9] if x.name.endswith('.disabled') else x.name for x in (game/'mods/_disabled').rglob('*') if x.is_file()} else "MISSING")
        size = ""
        if p.exists():
            try:
                size = f"  ({p.stat().st_size}B)"
            except OSError:
                size = ""
        lines.append(f"  {mark:7} {f}{size}")
    total = file_size_total(rec, game)
    if is_disabled(rec):
        # size from parked
        total = 0
        bases = {Path(f).name for f in files}
        dd = game / "mods/_disabled"
        if dd.exists():
            for pp in dd.rglob("*"):
                if pp.is_file() and (pp.name in bases or (pp.name[:-9] if pp.name.endswith('.disabled') else pp.name) in bases):
                    try:
                        total += pp.stat().st_size
                    except OSError:
                        pass
    lines.append(f"total size on disk: {total} bytes")
    return "\n".join(lines)


# ---- check ----
def do_check(game):
    problems = []
    lines = []
    # frameworks
    lines.append("Frameworks:")
    red4_ver = None
    cet_ver = None
    redscript_ver = None
    # red4ext newest log
    red4_logs = sorted((game / "red4ext/logs").glob("red4ext-*.log")) if (game / "red4ext/logs").exists() else []
    red4_log = red4_logs[-1] if red4_logs else None
    if red4_log:
        txt = red4_log.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"RED4ext \(v([\d.]+)\)", txt)
        if m:
            red4_ver = m.group(1)
        # plugins
        plugins = []
        for line in txt.splitlines():
            if "Loading plugin from" in line:
                mm = re.search(r"plugins[/\\]([^/\\]+)[/\\]", line)
                if mm:
                    d = mm.group(1)
                    if d not in plugins:
                        plugins.append(d)
        lines.append(f"  RED4ext {red4_ver or '?'}")
        lines.append(f"  Plugins loaded: {len(plugins)} ({', '.join(plugins)})")
        if "scc invoked successfully" in txt:
            lines.append("  redscript compile: OK (scc invoked successfully)")
        else:
            lines.append("  redscript compile: UNKNOWN")
    else:
        problems.append("RED4ext log not found")
    # CET
    cet_log = game / "bin/x64/plugins/cyber_engine_tweaks/cyber_engine_tweaks.log"
    if cet_log.exists():
        txt = cet_log.read_text(encoding="utf-8", errors="replace")
        ms = re.findall(r"CET version v([\d.]+)", txt)
        if ms:
            cet_ver = ms[-1]
        lines.append(f"  CET {cet_ver or '?'}")
    else:
        problems.append("CET log not found")
    # redscript version
    # search logs
    _rlogs = list((game / "r6/logs").glob("*.log")) if (game / "r6/logs").exists() else []
    for log in _rlogs:
        txt = log.read_text(encoding="utf-8", errors="replace")
        mm = re.search(r"redscript[^\n]*?(\d+\.\d+\.\d+)", txt, re.IGNORECASE)
        if mm:
            redscript_ver = mm.group(1)
            break
    if redscript_ver is None:
        # fallback to state record
        recs = load_state()
        for r in recs.values():
            if "redscript" in str(r.get("mod_type", "")).lower():
                mm = re.search(r"redscript \d+ ([\d.]+)", r.get("name", ""))
                if mm:
                    redscript_ver = mm.group(1)
                    break
    lines.append(f"  redscript {redscript_ver or '?'}")
    # compile result
    compile_ok = False
    rlog = game / "r6/logs/redscript_rCURRENT.log"
    if rlog.exists():
        rtxt = rlog.read_text(encoding="utf-8", errors="replace")
        if "Output successfully saved to" in rtxt and "final.redscripts.modded" in rtxt:
            compile_ok = True
    if red4_log:
        rtxt2 = red4_log.read_text(encoding="utf-8", errors="replace")
        if "scc invoked successfully" in rtxt2:
            compile_ok = True
    lines.append(f"  redscript compile: {'OK' if compile_ok else 'FAIL'}")
    if not compile_ok:
        problems.append("redscript compile failed")
    # integrity
    records = load_state()
    ok_n = broken_n = disabled_n = 0
    for rec in records.values():
        st = status_of(rec, game)
        if st == "OK":
            ok_n += 1
        elif st == "DISABLED":
            disabled_n += 1
        else:
            broken_n += 1
    lines.append(f"Mods: {ok_n} ok \u00b7 {broken_n} broken \u00b7 {disabled_n} disabled")
    if broken_n > 0:
        problems.append(f"{broken_n} broken mods")
    if red4_ver is None:
        problems.append("RED4ext version not detected")
    if cet_ver is None:
        problems.append("CET version not detected")
    healthy = not problems
    lines.append("Health: " + ("OK" if healthy else "PROBLEMS: " + "; ".join(problems)))
    return "\n".join(lines), healthy


# ---- list ----
def do_list(game, category=None):
    records = load_state()
    items = sorted(records.items(), key=lambda kv: (kv[1].get("order") if isinstance(kv[1].get("order"), int) else 0, kv[0]))
    if category:
        items = [(k, r) for k, r in items if r.get("category") == category]
    if not items:
        if category:
            return f"No mods in category '{category}'. Install one or run: cpmm category '<name>' {category}"
        return "No mods installed. Install one: cpmm install <path.zip|folder>"
    # group by category
    groups = {}
    for k, r in items:
        groups.setdefault(r.get("category", "misc"), []).append((k, r))
    cols = term_width()
    fixed = 5 + 1 + 10 + 1 + 26 + 1 + 9 + 1 + 9 + 1  # order name cat type status files
    name_w = max(16, cols - fixed)
    lines = []
    # header row template
    def fmt_row(order, name, cat, typ, st, files):
        return f"{str(order):>4}  {trunc(name, name_w):<{name_w}} {cat:<10} {trunc(typ, 26):<26} {st:<9} {files:>9}"
    for cat in sorted(groups):
        grp = sorted(groups[cat], key=lambda kv: (kv[1].get("order") if isinstance(kv[1].get("order"), int) else 0, kv[0]))
        lines.append(c("accent", f"{cat} \u2014 {len(grp)} mods"))
        lines.append(c("muted", f"{'order':>4}  {'name':<{name_w}} {'category':<10} {'type':<26} {'status':<9} {'files':>9}"))
        for k, r in grp:
            st = status_of(r, game)
            stc = c("success" if st == "OK" else ("warning" if st == "DISABLED" else "error"), f"{st:<9}")
            files = f"{parked_count(r, game) if is_disabled(r) else sum(1 for f in r.get('files', []) if (game/f).exists())}/{len(r.get('files', []))}"
            lines.append(f"{str(r.get('order', '')):>4}  {trunc(r.get('name', k), name_w):<{name_w}} {r.get('category', ''):<10} {trunc(r.get('mod_type', ''), 26):<26} {stc} {files:>9}")
    return "\n".join(lines)


# ---- TUI ----
def _clear():
    if _COLOR:
        sys.stdout.write(THEME["clear"])
        sys.stdout.flush()


def tui():
    while True:
        records = load_state()
        game = game_path()
        n_mods = len(records)
        n_ok = 0
        if game:
            n_ok = sum(1 for r in records.values() if status_of(r, game) == "OK")
        gbase = game.name if game else "no game"
        _clear()
        print(header(gbase, n_mods, n_ok))
        print(rule(term_width()))
        print("  1. List mods")
        print("  2. Set category")
        print("  3. Mod metadata")
        print("  4. Install mod")
        print("  5. Uninstall mod")
        print("  6. Enable mod")
        print("  7. Disable mod")
        print("  8. Health check")
        print("  9. Set game dir")
        print("  0. Quit")
        print(rule(term_width()))
        print(footer("type a number (q to quit)"))
        try:
            choice = input(c("accent", "cpmm> ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            choice = "0"
        if choice in ("0", "q", "quit", ""):
            break
        if choice == "1":
            _clear()
            print(header(gbase, n_mods, n_ok))
            print(rule(term_width()))
            cat = input("category filter (blank=all): ").strip()
            print(do_list(game, cat or None) if game else "No game dir set — run 9.")
            print(rule(term_width()))
            print(footer("Enter to return"))
            input()
        elif choice == "2":
            _clear()
            print(header(gbase, n_mods, n_ok))
            print(rule(term_width()))
            if not game:
                print("No game dir set — run 9.")
                input()
                continue
            print(do_list(game))
            print(rule(term_width()))
            q = input("mod name or number: ").strip()
            # number support
            recs = load_state()
            keys = [k for k, _ in sorted(recs.items(), key=lambda kv: (kv[1].get("order", 0), kv[0]))]
            if q.isdigit():
                idx = int(q) - 1
                if 0 <= idx < len(keys):
                    q = keys[idx]
            cat = input("category: ").strip()
            if not cat:
                print("cancelled")
                input()
                continue
            try:
                print(do_category(q, cat))
            except Exception as e:
                print(err_line(str(e)))
            input()
        elif choice == "3":
            _clear()
            print(header(gbase, n_mods, n_ok))
            print(rule(term_width()))
            if not game:
                print("No game dir set — run 9.")
                input()
                continue
            q = input("mod name or number: ").strip()
            recs = load_state()
            keys = [k for k, _ in sorted(recs.items(), key=lambda kv: (kv[1].get("order", 0), kv[0]))]
            if q.isdigit():
                idx = int(q) - 1
                if 0 <= idx < len(keys):
                    q = keys[idx]
            try:
                print(do_meta(q, game))
            except Exception as e:
                print(err_line(str(e)))
            input()
        elif choice == "4":
            _clear()
            print(header(gbase, n_mods, n_ok))
            print(rule(term_width()))
            if not game:
                print("No game dir set — run 9.")
                input()
                continue
            path = input("path (.zip/.7z/.rar/folder): ").strip()
            cat = input("category (blank=misc): ").strip()
            try:
                print(do_install(path, cat or None, game))
            except Exception as e:
                print(err_line(str(e)))
            input()
        elif choice == "5":
            _clear()
            print(header(gbase, n_mods, n_ok))
            print(rule(term_width()))
            if not game:
                print("No game dir set — run 9.")
                input()
                continue
            print(do_list(game))
            print(rule(term_width()))
            q = input("mod name or number: ").strip()
            recs = load_state()
            keys = [k for k, _ in sorted(recs.items(), key=lambda kv: (kv[1].get("order", 0), kv[0]))]
            if q.isdigit():
                idx = int(q) - 1
                if 0 <= idx < len(keys):
                    q = keys[idx]
            try:
                res = do_uninstall(q, game, yes=False, interactive=True)
                print(ok_line(res))
            except Exception as e:
                print(err_line(str(e)))
            input()
        elif choice == "6":
            _clear()
            print(header(gbase, n_mods, n_ok))
            print(rule(term_width()))
            if not game:
                print("No game dir set — run 9.")
                input()
                continue
            q = input("mod name or number: ").strip()
            recs = load_state()
            keys = [k for k, _ in sorted(recs.items(), key=lambda kv: (kv[1].get("order", 0), kv[0]))]
            if q.isdigit():
                idx = int(q) - 1
                if 0 <= idx < len(keys):
                    q = keys[idx]
            try:
                print(ok_line(do_enable(q, game)))
            except Exception as e:
                print(err_line(str(e)))
            input()
        elif choice == "7":
            _clear()
            print(header(gbase, n_mods, n_ok))
            print(rule(term_width()))
            if not game:
                print("No game dir set — run 9.")
                input()
                continue
            print(do_list(game))
            print(rule(term_width()))
            q = input("mod name or number: ").strip()
            recs = load_state()
            keys = [k for k, _ in sorted(recs.items(), key=lambda kv: (kv[1].get("order", 0), kv[0]))]
            if q.isdigit():
                idx = int(q) - 1
                if 0 <= idx < len(keys):
                    q = keys[idx]
            try:
                # confirm
                rec = load_state()[resolve_record(q, load_state())]
                print(f"Disable '{rec['name']}' ({rec.get('category')}, {len(rec.get('files', []))} files)? [y/N] ", end="", flush=True)
                ans = input().strip().lower()
                if ans not in ("y", "yes"):
                    print("cancelled")
                else:
                    print(ok_line(do_disable(q, game)))
            except Exception as e:
                print(err_line(str(e)))
            input()
        elif choice == "8":
            _clear()
            print(header(gbase, n_mods, n_ok))
            print(rule(term_width()))
            if not game:
                print("No game dir set — run 9.")
                input()
                continue
            txt, _ = do_check(game)
            print(txt)
            input()
        elif choice == "9":
            out, chosen = do_detect()
            print(out)
            if chosen:
                print(ok_line(f"Saved: {chosen}"))
            input()
        else:
            print(err_line("unknown option"))
            input()


# ---- CLI main ----
def main(argv=None):
    global _COLOR
    parser = argparse.ArgumentParser(prog="cpmm", description="Cyberpunk 2077 mod manager (stdlib only).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n  cpmm detect\n  cpmm set --game '/path/to/Cyberpunk 2077'\n  cpmm list\n  cpmm list --category misc\n  cpmm meta 'CET 1.38.1'\n  cpmm check\n  cpmm install mod.zip --category gameplay\n  cpmm uninstall 'Mod Name' -y\n  cpmm tui")
    parser.add_argument("--no-color", action="store_true", help="disable color output")
    sub = parser.add_subparsers(dest="cmd")
    # common parent for no-color in subcommands
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--no-color", action="store_true", help="disable color output")

    def add(name, help_, **kw):
        return sub.add_parser(name, help=help_, parents=[common], formatter_class=argparse.RawDescriptionHelpFormatter, **kw)

    p_detect = add("detect", "detect game dir")
    p_set = add("set", "set game dir")
    p_set.add_argument("--game", required=True, help="game dir")
    p_install = add("install", "install a mod")
    p_install.add_argument("path", help="zip/folder")
    p_install.add_argument("--category", default=None, help="category")
    p_un = add("uninstall", "uninstall a mod")
    p_un.add_argument("name", help="name or id")
    p_un.add_argument("-y", action="store_true", help="skip confirm")
    p_en = add("enable", "enable a mod")
    p_en.add_argument("name", help="name or id")
    p_dis = add("disable", "disable a mod")
    p_dis.add_argument("name", help="name or id")
    p_cat = add("category", "set category")
    p_cat.add_argument("name", help="name or id")
    p_cat.add_argument("category", help="category")
    p_meta = add("meta", "show metadata")
    p_meta.add_argument("name", help="name or id")
    p_check = add("check", "health check")
    p_list = add("list", "list mods")
    p_list.add_argument("--category", default=None, help="filter category")
    p_tui = add("tui", "interactive TUI")

    args = parser.parse_args(argv)
    if args.no_color or getattr(args, "no_color", False):
        set_color(False)
    if os.environ.get("NO_COLOR"):
        set_color(False)
    if not sys.stdout.isatty():
        set_color(False)
    # re-evaluate no_color from subparser
    if getattr(args, "no_color", False):
        set_color(False)

    cmd = args.cmd or "tui"
    if cmd == "tui":
        tui()
        return 0
    if cmd == "detect":
        out, chosen = do_detect()
        print(out)
        return 0
    if cmd == "set":
        print(set_game(args.game))
        return 0
    game = game_path()
    if cmd in ("list", "meta", "check", "install", "uninstall", "enable", "disable", "category") and game is None:
        print(err_line("no game dir set — run: cpmm detect  (or cpmm set --game <dir>)"))
        return 2
    try:
        if cmd == "list":
            print(do_list(game, args.category))
            return 0
        if cmd == "meta":
            print(do_meta(args.name, game))
            return 0
        if cmd == "check":
            txt, healthy = do_check(game)
            print(txt)
            return 0 if healthy else 1
        if cmd == "install":
            print(do_install(args.path, args.category, game))
            return 0
        if cmd == "uninstall":
            print(ok_line(do_uninstall(args.name, game, yes=args.y, interactive=not args.y)))
            return 0
        if cmd == "enable":
            print(ok_line(do_enable(args.name, game)))
            return 0
        if cmd == "disable":
            recs = load_state()
            key = resolve_record(args.name, recs)
            rec = recs[key]
            print(f"Disable '{rec['name']}' ({rec.get('category')}, {len(rec.get('files', []))} files)? [y/N] ", end="", flush=True)
            # for CLI non-interactive default require -y? spec: require -y or confirm prompt.
            # We'll prompt always unless -y not provided; -y flag not on disable. Prompt:
            ans = input().strip().lower()
            if ans not in ("y", "yes"):
                print("cancelled")
                return 0
            print(ok_line(do_disable(args.name, game)))
            return 0
        if cmd == "category":
            print(ok_line(do_category(args.name, args.category)))
            return 0
    except Exception as e:
        print(err_line(str(e)))
        return 2
    return 0
if __name__ == "__main__":
    sys.exit(main())

