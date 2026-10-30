# Nexus Mods free-account downloads

How to download a Nexus Mods file on a **free (non-premium) account** using the
`nexus_mcp_plus` MCP server/CLI (launched by an MCP client, or directly) — not
`cpmm`. A one-time browser login supplies the session cookie; the minted
archive is then installed into the game with `cpmm install <file.zip>`. Source
of truth for the `nexus_mcp_plus` wrapper package — must not drift.

## 1. The problem

The Nexus API cannot hand you a download URL on its own for a free account:

- `GET https://api.nexusmods.com/v1/games/<domain>/mods/<mod>/files/<file>/download_link.json`
  with **no `key`/`expires`** returns **HTTP 403** with the verbatim body:

  > You don't have permission to get download links from the API without visting nexusmods.com - this is for premium users only.

API-only linking (no prior website session) is premium-only. A free account must
first obtain a short-lived `nxm://` link by minting it through the website with
a live `nexusmods_session` cookie, then trade that link's `key`+`expires` at the
same API endpoint.

## 2. The working chain (4 steps)

1. **Browser session cookie.** Read the `nexusmods_session` cookie from a
   logged-in browser. Firefox stores it in plaintext SQLite; Chromium keeps it
   in the OS keyring. Cookie DBs are copied to a temp file first (the live DB
   may be locked, and Firefox profile dirs are symlinked into tmpfs by
   profile-sync-daemon). Cookie values are never logged.

2. **Mint the nxm link.**
   `POST https://www.nexusmods.com/Core/Libs/Common/Managers/Downloads?GenerateDownloadUrl`
   with body `{fid, game_id, nmm=1}`, the session cookie, `X-Requested-With:
   XMLHttpRequest`, a `Referer` to the file page, and a Chrome TLS fingerprint
   via `curl_cffi` (`impersonate="chrome124"`). Returns
   `{"url": "nxm://<domain>/mods/<mod>/files/<file>?key=...&expires=...&user_id=..."}`.

   - `game_id` is per-domain; e.g. `cyberpunk2077` → `3333`.
   - Fallback: `GET https://www.nexusmods.com/Core/Libs/Common/Widgets/DownloadPopUp?id=<file>&game_id=<gid>&nmm=1`
     and scrape the `nxm://` URL out of the HTML with the regex
     `nxm://[^"'<\s]+key=([^&"'<\s]+)[^"'<\s]*expires=(\d+)`.

3. **Resolve the CDN link.**
   `GET https://api.nexusmods.com/v1/games/<domain>/mods/<mod>/files/<file>/download_link.json?key=..&expires=..`
   with the `apikey: <API_KEY>` header. Returns the mirror list. Free accounts
   are accepted **here** — because `key`+`expires` came from the website step.

4. **Stream the file.** Download `mirrors[0].URI` to disk with
   `out.<suffix>.part` + atomic `os.replace`.

### Cloudflare / no-session behaviour

Without a valid session the website endpoints are bot-gated:
`GET .../Widgets/DownloadPopUp?...` and `POST .../Managers/Downloads?...GenerateDownloadUrl`
both return **HTTP 403**, `Cf-Mitigated: challenge`, body
`<title>Just a moment...</title>` (Cloudflare Turnstile). No header trick
bypasses this; a logged-in browser is mandatory.

## 3. Evidence it works on a free account

A real `nxm://` link replaces the premium-403 with **key validation**:

| Key/expires supplied | HTTP | Body |
|---|---|---|
| no key/expires | 403 | `You don't have permission to get download links from the API without visting nexusmods.com - this is for premium users only.` |
| bogus key, future expires (`key=deadbeef…&expires=4102444800`) | 400 | `{"code":400,"message":"Provided key and expire time isn't correct for this user/file."}` |
| past expiry (`key=zz&expires=1000000000`) | 410 | `{"code":410,"message":"This link has expired - please visit the mod page again to get a new link"}` |

The premium message disappears the moment any `key`+`expires` is supplied; the
only remaining failure mode for a real download is an expired/wrong key. Real
links are short-lived (minutes). `rookiepy` did not build on this host (Python
3.14 wheel failure); `browser_cookie3` 0.20.1 works and is the only non-stdlib
dependency the prototype needs besides `curl_cffi`.

## 4. Library survey

| Repo | Language | License | Cookie extraction | Mint endpoint | nxm parse | Streams file |
|---|---|---|---|---|---|---|
| `er2g/nexusmods-mcp-server` | Python | none | n/a (cookies.json import) | YES — GET DownloadPopUp, scrapes `#dl_link` | indirect (CDN URL) | YES |
| `mbj733/nexus-mcp` | TS | ISC | YES (NEXUS_COOKIES JSON) | YES — GET DownloadPopUp | indirect (CDN URL) | YES |
| `SahiDemon/NCD` | Python | none | YES (browser_cookie3 multi-browser + live verify) | YES — POST GenerateDownloadUrl + GET DownloadPopUp HTML fallback | YES (regex `nxm://...key=..expires=..`) | YES |
| `0xra0/bethesda-strings-editor` | Python | MIT | YES (Firefox/Chromium SQLite, unencrypted fallback) | YES — POST GenerateDownloadUrl | YES | YES |
| `Cutleast/SSE-Auto-Translator` | Python | NOASSERTION | YES (Qt SSO browser cookies) | n/a (relies on NXM link minted by the site) | YES (NxmRequest.from_url) | YES |
| `amssss0/Wabbajack-fast-downloader2` | Python | — | manual cookie string | YES — POST GenerateDownloadUrl via cloudscraper | indirect (CDN URL) | YES |
| `joy13975/nexus-mocodo` | Python | MIT | manual cookies.py | YES — POST GenerateDownloadUrl | indirect (CDN URL) | YES |
| `pigxity/NexusDL` | Python | GPL-3.0 | manual config (nexusmods_session only) | YES — POST GenerateDownloadUrl via curl_cffi | indirect | YES |
| `phate7/wabbajack-background-downloader` | Python | — | manual cookie string | YES — POST GenerateDownloadUrl | indirect | YES |
| `Talya1412/nexus-mcp` (installed) | Python | MIT | NO | NO (API-only) | YES (tool args) | YES (download_mod_file) |
| `dandels/dmodman` | Rust | MIT | NO (nxm:// handler only) | NO (consumes nxm://) | YES (NxmUrl parser) | YES |
| `greg-ynx/NexusDownloadFlow` | Python | none | NO | NO (OpenCV GUI button clicker) | NO | NO |

Reference implementations: **best** is `SahiDemon/NCD` — the only candidate
that combines `browser_cookie3` multi-browser auto-detection with a live-session
verifier and the POST `GenerateDownloadUrl` + `DownloadPopUp` fallback.
**Cleanest MIT secondary** is `0xra0/bethesda-strings-editor` for the
copy-then-query of the Firefox/Chromium SQLite cookie DB and the
`_free_download_url` chain.

## 5. Tool contract

Wrapper package source: `nexus_mcp_plus/` in this repo.
Durable venv: `~/.local/share/nexus-mcp/venv`.
Entry point: `nexus-mcp-plus`.

### MCP tools

- `nexus_free_session_status()` →
  `{api_key_present, premium, firefox_session, chromium_session, hint}`
- `nexus_free_mint_link(game_domain, mod_id, file_id)` →
  `{nxm_url_redacted, key_present, expires, download_link_http, mirrors}`
- `nexus_free_download_file(game_domain, mod_id, file_id, dest_dir, verify_sha256=False)` →
  `{saved_path, bytes, sha256, source_url_host}`

### CLI

- `nexus-mcp-plus --check`
- `nexus-mcp-plus <game_domain> <mod_id> <file_id> <dest_dir>`

### MCP client config (paste under `mcpServers`)

```json
{"nexus":{"command":"bash","args":["-c","NEXUS_API_KEY=$(cat ~/.config/cp2077-mod-manager/nexus_api_key) exec ~/.local/share/nexus-mcp/venv/bin/nexus-mcp-plus"]}}
```

### Upstream server

Installed server (140 tools: search / details / files):
`~/.local/share/nexus-mcp/venv/bin/nexus-mcp`, env `NEXUS_API_KEY`.

## 6. One-time user action and commands

1. Log into `https://www.nexusmods.com` in **Firefox profile
   `3buotdx0.default-release`** (its cookie DB is plain SQLite, no keyring
   needed). Chrome works too but needs the OS keyring at
   `~/.local/share/keyrings/login.keyring`.

2. Verify the session:
   ```
   ~/.local/share/nexus-mcp/venv/bin/nexus-mcp-plus --check
   ```
   Expected: an `OK:` line reporting the session cookie found via Firefox (or
   Chromium) with `N cookies`; ready to download.

3. Download a file (example: Cyberpunk 2077, mod 107, file 168615):
   ```
   ~/.local/share/nexus-mcp/venv/bin/nexus-mcp-plus cyberpunk2077 107 168615 /tmp/dl
   ```
   Expected output:
   ```
   nxm: nxm://cyberpunk2077/mods/107/files/168615?key=***&expires=<epoch>
   cdn: HTTP 200, N mirror(s)
   saved: /tmp/dl/<name>.zip (34868963 bytes)
   ```

4. Verify the zip:
   ```
   python3 -c "import zipfile;print(zipfile.ZipFile('<saved>.zip').namelist()[:5])"
   ```

## 7. Security / ops

- API key lives at `~/.config/cp2077-mod-manager/nexus_api_key`, mode `600`, read
  at launch — **never** inline in configs or commits.
- Cookie values are **never** logged (the printed `nxm:` line redacts `key=` as
  `***`).
- Cookie DBs are copied to a temp file before reading (the live DB may be
  locked; Firefox profile dirs are symlinked into tmpfs by profile-sync-daemon).
- The API key must **never** be written into this repo.

## 8. Limits

- Free accounts download from nexusmods.com either manually or via this
  session-based flow only. **Automated API-only download requires premium.**
- Rate-limit reality: few calls, no loops. The free mint step is per-file and
  short-lived (minutes), so cache or pace calls accordingly.
