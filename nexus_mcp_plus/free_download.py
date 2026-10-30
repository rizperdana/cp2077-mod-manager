#!/usr/bin/env python3
"""free_download.py — download a Nexus Mods file on a FREE account.

Chain (endpoints verified against nexusmods.com 2026-10-30; evidence in
proto REPORT.md, candidate-library table):
  1. Read the browser's nexusmods.com session cookie (Firefox plaintext SQLite,
     copy-first incl. profile-sync-daemon tmpfs; Chromium via browser_cookie3 +
     OS keyring).
  2. POST https://www.nexusmods.com/Core/Libs/Common/Managers/Downloads?GenerateDownloadUrl
     with {fid, game_id, nmm} + session cookie + Chrome TLS fingerprint
     (curl_cffi impersonate="chrome124"). Returns JSON
     {"url": "nxm://<game>/mods/<mod>/files/<file>?key=..&expires=..&user_id=.."}.
     Fallback: GET .../Widgets/DownloadPopUp?id=..&game_id=..&nmm=1 and scrape
     the nxm URL out of the HTML (regex shapes shared with SahiDemon/NCD,
     0xra0/bethesda-strings-editor, dandels/dmodman).
  3. GET https://api.nexusmods.com/v1/games/<domain>/mods/<mod>/files/<file>/download_link.json
     ?key=..&expires=.. with only the apikey header -> 200 + mirror list
     (free accounts OK; without a key the API answers 403 premium-only; a bogus
     key answers 400; an expired key 410 — so a freshly minted key is the only
     prerequisite).
  4. Stream the CDN URL to disk (atomically, via .part + os.replace), with a
     size cross-check against the API's size_kb figure.

No session cookie -> one clean sentence naming the fix, never a traceback.
The API key and cookie values are never printed; `key=` is redacted in nxm URLs.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

GAME_DOMAINS = {
    "cyberpunk2077": "3333", "skyrimspecialedition": "1704", "skyrim": "110",
    "fallout4": "1151", "falloutnv": "130", "oblivion": "101", "starfield": "4853",
    "stardewvalley": "1303", "witcher3": "952", "bannerlord": "3547",
    "baldursgate3": "3474", "morrowind": "100",
}

API_BASE = "https://api.nexusmods.com"
POPUP_URL = "https://www.nexusmods.com/Core/Libs/Common/Widgets/DownloadPopUp"
GENERATE_URL = ("https://www.nexusmods.com/Core/Libs/Common/Managers/Downloads"
                "?GenerateDownloadUrl")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
KEY_FILE = "~/.config/cp2077-mod-manager/nexus_api_key"
NO_SESSION_HINT = ("No nexusmods.com session cookie found. Fix: log into "
                   "nexusmods.com in Firefox profile 3buotdx0.default-release, "
                   "then retry.")
NET_TIMEOUT = 30          # seconds, API/mint calls
DOWNLOAD_TIMEOUT = 600    # seconds, CDN stream

NXM_RE = re.compile(
    r"nxm://[^\"'<\s]+key=([^&\"'<\s]+)[^\"'<\s]*expires=(\d+)", re.IGNORECASE)


class FreeDownloadError(Exception):
    """Single-sentence, user-actionable failure (no traceback)."""


def redact_nxm(u: str) -> str:
    """Redact the secret `key=` parameter from an nxm/CDN URL."""
    return re.sub(r"([?&]key=)[^&\"'<\s]+", r"\1***", u)


def api_key() -> str:
    """API key from NEXUS_API_KEY or the config file; never logged."""
    v = (os.environ.get("NEXUS_API_KEY") or "").strip()
    if v:
        return v
    p = Path(KEY_FILE).expanduser()
    if p.is_file():
        return p.read_text(encoding="utf-8").strip()
    raise FreeDownloadError(
        f"Error: no API key (set NEXUS_API_KEY or create {KEY_FILE}).")


# -- 1. cookies ----------------------------------------------------------------

def read_firefox_cookies() -> dict[str, str]:
    """Plain-SQLite read of every Firefox profile; copies DB first (lock + psd)."""
    import glob as _g
    out: dict[str, str] = {}
    for pat in ("~/.mozilla/firefox/*/cookies.sqlite",
                "/run/user/*/psd/*-*/cookies.sqlite"):
        for db in _g.glob(os.path.expanduser(pat)):
            tmp = os.path.join(tempfile.gettempdir(),
                               f"nexus_mcp_plus_ck_{os.getpid()}.sqlite")
            try:
                shutil.copy2(db, tmp)
                for ext in ("-wal", "-shm"):
                    if os.path.exists(db + ext):
                        try:
                            shutil.copy2(db + ext, tmp + ext)
                        except PermissionError:
                            pass
                con = sqlite3.connect(tmp)
                try:
                    rows = con.execute(
                        "SELECT name, value FROM moz_cookies "
                        "WHERE host LIKE '%nexusmods.com'").fetchall()
                finally:
                    con.close()
                if rows:
                    out.update(dict(rows))
            except Exception:
                continue
            finally:
                # temp cookie-DB copy is deleted on every path, success or not
                for rem in (tmp, tmp + "-wal", tmp + "-shm"):
                    try:
                        os.remove(rem)
                    except OSError:
                        pass
    return out


def read_chromium_cookies() -> dict[str, str]:
    """Keyring-encrypted Chromium cookies via browser_cookie3 (lazy import)."""
    try:
        import browser_cookie3  # type: ignore
    except Exception:
        return {}
    try:
        return {c.name: c.value
                for c in browser_cookie3.chrome(domain_name="nexusmods.com")}
    except Exception:
        return {}


def session_cookies() -> dict[str, str]:
    ck = read_firefox_cookies() or read_chromium_cookies()
    return ck if ck.get("nexusmods_session") else {}


# -- 2. mint the nxm link --------------------------------------------------------

def mint_nxm(domain: str, mod_id: int, file_id: int,
             cookies: dict[str, str]) -> str:
    """Return a raw nxm:// URL (key+expires) minted by the website."""
    try:
        from curl_cffi import requests as cr  # lazy; engine importable without
    except Exception as exc:
        raise FreeDownloadError(
            f"Error: curl_cffi not installed ({exc}); run "
            "`pip install curl_cffi` into the nexus-mcp venv.") from exc
    game_id = GAME_DOMAINS.get(domain)
    if not game_id:
        raise FreeDownloadError(
            f"Error: unknown game_domain '{domain}' - add its numeric id to "
            "GAME_DOMAINS (shown on any mod page's DownloadPopUp call).")
    s = cr.Session(impersonate="chrome124")
    for n, v in cookies.items():
        s.cookies.set(n, v, domain=".nexusmods.com")
    referer = (f"https://www.nexusmods.com/{domain}/mods/{mod_id}"
               f"?tab=files&file_id={file_id}")

    # 2a. POST GenerateDownloadUrl (the site's Slow-download button)
    r = s.post(GENERATE_URL, data={"fid": str(file_id), "game_id": game_id,
                                   "nmm": "1"},
               headers={"X-Requested-With": "XMLHttpRequest", "Referer": referer,
                        "Origin": "https://www.nexusmods.com",
                        "Accept": "application/json, text/javascript, */*; q=0.01"},
               timeout=NET_TIMEOUT)
    if r.status_code == 403:
        raise FreeDownloadError(NO_SESSION_HINT)
    if r.ok:
        try:
            url = r.json().get("url", "")
        except ValueError:
            url = ""
        if "nxm://" in url and "key=" in url:
            return url.replace("&amp;", "&")
        if url and "nexus-cdn" in url:  # direct CDN link variant
            return url

    # 2b. fallback: DownloadPopUp HTML scrape
    r2 = s.get(POPUP_URL, params={"id": file_id, "nmm": "1", "game_id": game_id},
               headers={"X-Requested-With": "XMLHttpRequest", "Referer": referer},
               timeout=NET_TIMEOUT)
    if r2.status_code == 403:
        raise FreeDownloadError(NO_SESSION_HINT)
    m = NXM_RE.search(r2.text)
    if m:
        return (f"nxm://{domain}/mods/{mod_id}/files/{file_id}"
                f"?key={m.group(1)}&expires={m.group(2)}")
    raise FreeDownloadError(
        "Error: could not mint an nxm link - website session expired; log "
        "into nexusmods.com again, then retry.")


# -- 3/4. CDN link + download ----------------------------------------------------

def cdn_links(domain: str, mod_id: int, file_id: int, key: str,
              expires: str) -> list[dict]:
    """download_link.json with key+expires; free-account key validation path."""
    url = (f"{API_BASE}/games/{domain}/mods/{mod_id}/files/{file_id}"
           f"/download_link.json?key={urllib.parse.quote(key)}&expires={expires}")
    req = urllib.request.Request(
        url, headers={"apikey": api_key(), "Accept": "application/json",
                      "User-Agent": "nexus-mcp-plus/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=NET_TIMEOUT) as r:
            payload = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:200]
        if e.code in (400, 410):
            raise FreeDownloadError(
                f"Error: minted download key rejected (HTTP {e.code}): {body}")
        raise FreeDownloadError(
            f"Error: download_link.json HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise FreeDownloadError(f"Error: network error contacting Nexus API: {e}")
    if not isinstance(payload, list) or not payload:
        raise FreeDownloadError("Error: no download mirrors returned.")
    return payload


def file_info(domain: str, mod_id: int, file_id: int) -> dict:
    """v1 file metadata (file_name, size_kb) for the size cross-check."""
    url = f"{API_BASE}/games/{domain}/mods/{mod_id}/files/{file_id}.json"
    req = urllib.request.Request(
        url, headers={"apikey": api_key(), "Accept": "application/json",
                      "User-Agent": "nexus-mcp-plus/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=NET_TIMEOUT) as r:
            payload = json.loads(r.read())
    except Exception as exc:
        raise FreeDownloadError(
            f"Error: could not fetch file metadata: {exc}") from exc
    return payload if isinstance(payload, dict) else {}


def download(uri: str, dest_dir: str, expected_bytes: int | None = None,
             filename: str | None = None) -> Path:
    """Stream a CDN URL to dest_dir atomically; verifies size when known.
    filename: prefer the API's file_name over the URL basename when given."""
    d = Path(dest_dir).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    name = filename or os.path.basename(urllib.parse.urlparse(uri).path) or "mod.bin"
    out = d / name
    tmp = out.with_suffix(out.suffix + ".part")
    total = 0
    try:
        req = urllib.request.Request(uri, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as r, \
                open(tmp, "wb") as f:
            while True:
                chunk = r.read(1 << 18)
                if not chunk:
                    break
                f.write(chunk)
                total += len(chunk)
        if expected_bytes is not None and total != expected_bytes:
            raise FreeDownloadError(
                f"Error: downloaded {total} bytes but Nexus API reports "
                f"{expected_bytes}; discarding.")
        os.replace(tmp, out)
    except FreeDownloadError:
        tmp.unlink(missing_ok=True)
        raise
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise FreeDownloadError(f"Error: CDN download failed: {exc}") from exc
    return out


def _validate_key(key: str) -> dict | None:
    """v1 users/validate.json; None when the call fails for any reason."""
    req = urllib.request.Request(
        f"{API_BASE}/v1/users/validate.json",
        headers={"apikey": key, "Accept": "application/json",
                 "User-Agent": "nexus-mcp-plus/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=NET_TIMEOUT) as r:
            payload = json.loads(r.read())
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def session_status() -> dict:
    """JSON status snapshot for the MCP tool / --check."""
    ff = read_firefox_cookies()
    cr = read_chromium_cookies()
    key = bool((os.environ.get("NEXUS_API_KEY") or "").strip()
               or Path(KEY_FILE).expanduser().is_file())
    premium = None
    if key:
        info = _validate_key(api_key())
        if info is not None:
            premium = bool(info.get("is_premium?") if "is_premium?" in info
                           else info.get("is_premium"))
    if ff.get("nexusmods_session") or cr.get("nexusmods_session"):
        hint = "Session cookie present - free-account downloads are ready."
    else:
        hint = NO_SESSION_HINT
    return {"api_key_present": key, "premium": premium,
            "firefox_session": bool(ff.get("nexusmods_session")),
            "chromium_session": bool(cr.get("nexusmods_session")), "hint": hint}


# -- CLI -----------------------------------------------------------------------

def cmd_check() -> int:
    st = session_status()
    print(f"api key: {'present' if st['api_key_present'] else 'MISSING'}"
          f"{'' if st['premium'] is None else f' (premium={st['premium']})'}")
    if st["firefox_session"] or st["chromium_session"]:
        src = "Firefox" if st["firefox_session"] else "Chromium"
        print(f"OK: nexusmods session cookie found via {src}. "
              "Free-account downloads are ready.")
        return 0
    print(f"NO SESSION. {st['hint']}")
    return 1


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Nexus Mods free-account downloader (nxm mint + key/expires chain)")
    ap.add_argument("game_domain", nargs="?")
    ap.add_argument("mod_id", nargs="?", type=int)
    ap.add_argument("file_id", nargs="?", type=int)
    ap.add_argument("dest_dir", nargs="?")
    ap.add_argument("--api-key", help="override API key")
    ap.add_argument("--check", action="store_true",
                    help="verify API key + browser session only")
    a = ap.parse_args()
    if a.api_key:
        os.environ["NEXUS_API_KEY"] = a.api_key
    if a.check:
        sys.exit(cmd_check())
    if not (a.game_domain and a.mod_id and a.file_id and a.dest_dir):
        ap.error("need <game_domain> <mod_id> <file_id> <dest_dir> (or --check)")
    try:
        ck = session_cookies()
        if not ck.get("nexusmods_session"):
            raise FreeDownloadError(f"Error: {NO_SESSION_HINT}")
        nxm = mint_nxm(a.game_domain, a.mod_id, a.file_id, ck)
        print(f"nxm: {redact_nxm(nxm)}")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(nxm).query)
        links = cdn_links(a.game_domain, a.mod_id, a.file_id,
                          q["key"][0], q["expires"][0])
        print(f"cdn: HTTP 200, {len(links)} mirror(s)")
        try:
            info = file_info(a.game_domain, a.mod_id, a.file_id)
            size_kb = info.get("size_kb")
            expected = int(size_kb) * 1024 if isinstance(size_kb, (int, float)) else None
        except FreeDownloadError:
            expected = None  # metadata is best-effort; download proceeds
        out = download(links[0]["URI"], a.dest_dir, expected)
        print(f"saved: {out} ({out.stat().st_size} bytes)")
    except FreeDownloadError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
