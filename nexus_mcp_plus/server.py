"""nexus-mcp-plus — upstream Nexus Mods MCP (140 tools) + free-account download.

Imports the installed `nexus_mods_mcp` FastMCP instance and registers three
extra tools on the SAME instance (no upstream tools copied or re-registered):

  nexus_free_session_status()   -> API key + premium + browser session JSON
  nexus_free_mint_link(...)     -> mint nxm key/expires via nexusmods.com
  nexus_free_download_file(...) -> full free-account download to disk

Runs stdio MCP on `nexus-mcp-plus` (or `python -m nexus_mcp_plus`).
CLI: `nexus-mcp-plus --check` or
     `nexus-mcp-plus <game_domain> <mod_id> <file_id> <dest_dir>`.
"""
from __future__ import annotations

import json
from typing import Any

from nexus_mcp._server import mcp  # upstream FastMCP instance (140 tools)
from nexus_mcp._core import NexusApiError, _api, _check_domain

from . import free_download as fd

SERVER_INSTRUCTIONS_ADDENDUM = (
    "\n\nFree-account download tools (nexus_free_*): on a FREE account the v1 "
    "download_link endpoint returns 403 premium-only unless the request carries "
    "a valid nxm key+expires pair. nexus_free_mint_link mints that pair from "
    "the nexusmods.com website using the browser's session cookie, and "
    "nexus_free_download_file chains mint -> download_link -> CDN stream. "
    "A live nexusmods.com browser session is required; when it is missing the "
    "tools return one sentence naming the fix."
)


def _err(exc: Exception) -> str:
    """One clean sentence, never a traceback or raw Cloudflare HTML."""
    msg = str(exc).strip()
    if "<html" in msg.lower():
        msg = "Error: nexusmods.com returned a Cloudflare challenge page instead of data."
    return msg if msg.endswith(".") else msg + "."


def _as_str(v: Any) -> str | None:
    return v if isinstance(v, str) else None


@mcp.tool(
    name="nexus_free_session_status",
    annotations={"title": "Free-account session status", "readOnlyHint": True},
)
def nexus_free_session_status() -> str:
    """Report API key presence, premium flag, and Firefox/Chromium nexusmods
    session-cookie state. Call first to learn whether free-account downloads
    can run and, when not, the exact fix."""
    return json.dumps(fd.session_status(), separators=(",", ":"))


@mcp.tool(
    name="nexus_free_mint_link",
    annotations={"title": "Mint nxm link (free account)", "readOnlyHint": False},
)
def nexus_free_mint_link(
    game_domain: str,
    mod_id: int,
    file_id: int,
) -> str:
    """Mint a fresh nxm key/expires pair for this free account via the
    nexusmods.com website (needs the browser session cookie). Returns JSON
    {nxm_url_redacted, key_present, expires, download_link_http, mirrors} —
    the nxm URL is redacted; use nexus_free_download_file for the file."""
    if err := _check_domain(game_domain):
        return err
    try:
        ck = fd.session_cookies()
        if not ck.get("nexusmods_session"):
            return _err(fd.FreeDownloadError(fd.NO_SESSION_HINT))
        nxm = fd.mint_nxm(game_domain, mod_id, file_id, ck)
        import urllib.parse as _up
        q = _up.parse_qs(_up.urlparse(nxm).query)
        key, expires = q.get("key", [""])[0], q.get("expires", [""])[0]
        if not key:
            return _err(ValueError(
                "Error: minted nxm URL carried no key (unexpected site response)."))
        status, mirrors = 0, 0
        try:
            links = fd.cdn_links(game_domain, mod_id, file_id, key, expires)
            status, mirrors = 200, len(links)
        except fd.FreeDownloadError as exc:
            return _err(exc)
        return json.dumps(
            {"nxm_url_redacted": fd.redact_nxm(nxm), "key_present": True,
             "expires": int(expires) if expires.isdigit() else 0,
             "download_link_http": status, "mirrors": mirrors},
            separators=(",", ":"))
    except Exception as exc:
        return _err(exc)


@mcp.tool(
    name="nexus_free_download_file",
    annotations={"title": "Download mod file (free account)",
                 "readOnlyHint": False, "destructiveHint": False},
)
def nexus_free_download_file(
    game_domain: str,
    mod_id: int,
    file_id: int,
    dest_dir: str,
    verify_sha256: bool = False,
) -> str:
    """Download a mod file on a FREE account: mints the nxm key via the browser
    session, validates it against download_link.json, streams the CDN file to
    dest_dir. Returns JSON {saved_path, bytes, sha256|null, source_url_host} or
    one clean error sentence (e.g. the exact fix when no browser session)."""
    if err := _check_domain(game_domain):
        return err
    try:
        import hashlib
        import urllib.parse as _up

        ck = fd.session_cookies()
        if not ck.get("nexusmods_session"):
            return _err(fd.FreeDownloadError(fd.NO_SESSION_HINT))
        nxm = fd.mint_nxm(game_domain, mod_id, file_id, ck)
        q = _up.parse_qs(_up.urlparse(nxm).query)
        key, expires = q.get("key", [""])[0], q.get("expires", [""])[0]
        links = fd.cdn_links(game_domain, mod_id, file_id, key, expires)
        info = fd.file_info(game_domain, mod_id, file_id)
        size_kb = info.get("size_kb")
        expected = int(size_kb) * 1024 if isinstance(size_kb, (int, float)) else None
        fname = str(info.get("file_name") or "").strip()
        uri = links[0]["URI"]
        saved = fd.download_to(uri, dest_dir, expected_bytes=expected,
                               filename=fname or None)
        h = None
        if verify_sha256:
            h = hashlib.sha256(saved.read_bytes()).hexdigest()
        host = _up.urlparse(uri).netloc
        return json.dumps(
            {"saved_path": str(saved), "bytes": saved.stat().st_size,
             "sha256": h, "source_url_host": host},
            separators=(",", ":"))
    except NexusApiError as exc:
        return _err(exc)
    except Exception as exc:
        return _err(exc)


def main(argv: list[str] | None = None) -> None:
    """Entry point: MCP stdio server by default; CLI modes otherwise."""
    import sys as _sys
    argv = list(_sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("--check", "-c"):
        raise SystemExit(fd.cmd_check())
    if len(argv) == 4 and argv[0] != "--help":
        # positional CLI download mode (same entry, no MCP)
        import os as _os
        import urllib.parse as _up
        try:
            ck = fd.session_cookies()
            if not ck.get("nexusmods_session"):
                raise fd.FreeDownloadError(f"Error: {fd.NO_SESSION_HINT}")
            domain, mod_id, file_id, dest = argv
            nxm = fd.mint_nxm(domain, int(mod_id), int(file_id), ck)
            print(f"nxm: {fd.redact_nxm(nxm)}")
            q = _up.parse_qs(_up.urlparse(nxm).query)
            links = fd.cdn_links(domain, int(mod_id), int(file_id),
                                 q["key"][0], q["expires"][0])
            info = fd.file_info(domain, int(mod_id), int(file_id))
            size_kb = info.get("size_kb")
            expected = (int(size_kb) * 1024
                        if isinstance(size_kb, (int, float)) else None)
            out = fd.download_to(links[0]["URI"], dest, expected_bytes=expected,
                                 filename=str(info.get("file_name") or "") or None)
            print(f"saved: {out} ({out.stat().st_size} bytes)")
        except (fd.FreeDownloadError, NexusApiError, ValueError) as exc:
            raise SystemExit(_err(exc))
        return
    if argv and argv[0] == "--help":
        print(__doc__)
        return
    mcp.run()  # stdio MCP server (upstream 140 tools + our 3)


if __name__ == "__main__":
    main()
