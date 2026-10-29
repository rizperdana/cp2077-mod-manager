#!/usr/bin/env python3
"""Stdlib-only web dashboard for the CP2077 mod manager."""
import argparse, http.server, json, os, re, socketserver, threading, urllib.parse
from pathlib import Path

APP_NAME = "cp2077-mod-manager"
STATE_DIR = Path.home() / ".local" / "share" / APP_NAME
STATE_FILE = STATE_DIR / "mods.json"
SETTINGS_FILE = STATE_DIR / "settings.json"
BAK_FILE = STATE_DIR / "mods.json.bak"

def load_settings():
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def load_state():
    def _parse(p):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    raw = None
    if STATE_FILE.exists():
        raw = _parse(STATE_FILE)
    if raw is None and BAK_FILE.exists():
        raw = _parse(BAK_FILE)
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        if not isinstance(v, dict):
            continue
        try:
            out[k] = v
        except Exception:
            pass
    return out

def game_path_from(args):
    if getattr(args, "game", None):
        return Path(args.game)
    st = load_settings()
    gp = st.get("game_path", "")
    if gp:
        return Path(gp)
    return None

def newest_red4ext_log(gp):
    d = gp / "red4ext" / "logs"
    if not d.exists():
        return None
    logs = sorted(d.glob("red4ext-*.log"))
    return logs[-1] if logs else None

def redscript_log(gp):
    p = gp / "r6" / "logs" / "redscript_rCURRENT.log"
    return p if p.exists() else None

def cet_log(gp):
    p = gp / "bin" / "x64" / "plugins" / "cyber_engine_tweaks" / "cyber_engine_tweaks.log"
    return p if p.exists() else None

def parse_game_version(gp):
    L = newest_red4ext_log(gp)
    if not L:
        return {"product": None, "file": None}
    text = L.read_text(encoding="utf-8", errors="replace")
    prod = re.search(r"Product version:\s*(.+)", text)
    fver = re.search(r"File version:\s*(.+)", text)
    return {"product": prod.group(1).strip() if prod else None,
            "file": fver.group(1).strip() if fver else None}

def parse_frameworks(gp):
    out = {"red4ext": None, "cet": None}
    L = newest_red4ext_log(gp)
    if L:
        text = L.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"RED4ext \(v([\d.]+)\)", text)
        if m:
            out["red4ext"] = m.group(1)
    c = cet_log(gp)
    if c:
        text = c.read_text(encoding="utf-8", errors="replace")
        # latest block: search last occurrence
        ms = re.findall(r"CET version v([\d.]+)", text)
        if ms:
            out["cet"] = ms[-1]
    return out

def count_plugins(gp):
    L = newest_red4ext_log(gp)
    if not L:
        return 0, []
    text = L.read_text(encoding="utf-8", errors="replace")
    # Derive plugin list from "Loading plugin from" lines; count unique plugin
    # dir names (single backslash or slash separators in the Windows log path).
    loaded_dirs = re.findall(r"plugins[/\\]([^/\\]+)[/\\]", text)
    seen = set(); uniq = []
    for n in loaded_dirs:
        if n not in seen:
            seen.add(n); uniq.append(n)
    loaded = len(re.findall(r"has been loaded", text))
    return loaded, uniq

def redscript_result(gp):
    p = redscript_log(gp)
    if not p:
        return {"result": "no log", "tail": []}
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = lines[-30:]
    low = "\n".join(tail).lower()
    if "output successfully saved" in low:
        res = "OK"
    elif "error" in low:
        res = "ERROR"
    else:
        res = "unknown"
    return {"result": res, "tail": tail}

def parked_exists(gp, basename):
    root = gp / "mods" / "_disabled"
    if not root.exists():
        return False
    for f in root.rglob("*"):
        if f.is_file() and f.name == basename:
            return True
    return False

def record_status(gp, rec):
    disabled = str(rec.get("mod_type", "")).startswith("DISABLED")
    files = rec.get("files", [])
    if not isinstance(files, list):
        files = []
    missing = []
    parked = []
    if disabled:
        for f in files:
            if not isinstance(f, str):
                continue
            if (gp / f).exists():
                continue
            if parked_exists(gp, Path(f).name):
                parked.append(f)
            else:
                missing.append(f)
        return "DISABLED", missing, parked
    for f in files:
        if not isinstance(f, str):
            continue
        if not (gp / f).exists():
            missing.append(f)
    return ("OK" if not missing else "BROKEN"), missing, parked

def norm(s):
    return str(s).lower().replace(" ", "").replace("_", "").replace("-", "")

def untracked_scan(gp, state):
    roots = [
        gp / "archive" / "pc" / "mod",
        gp / "r6" / "scripts",
        gp / "r6" / "tweaks",
        gp / "red4ext" / "plugins",
    ]
    # r6/scripts/* and r6/tweaks/* are direct children; archive/pc/mod direct; red4ext/plugins direct
    entries = []
    for r in roots:
        if r.exists():
            for child in r.iterdir():
                entries.append((r, child))
    # also r6/scripts/* means direct children only (already)
    tracked_norms = set()
    rec_names = []
    rec_files = []
    for rec in state.values():
        rec_names.append(norm(rec.get("name", "")))
        for f in rec.get("files", []) or []:
            rec_files.append(str(f).lower())
    out = []
    for root, child in entries:
        cname = child.name
        cn = norm(cname)
        tracked = False
        for rn in rec_names:
            if cn and (cn in rn or rn in cn):
                tracked = True
                break
        if not tracked:
            for f in rec_files:
                if cname.lower() in f:
                    tracked = True
                    break
        if not tracked:
            try:
                rel = child.relative_to(gp).as_posix()
            except Exception:
                rel = str(child)
            out.append(rel)
    return out

def build_state(gp):
    state = load_state()
    records = []
    summary = {"total": 0, "ok": 0, "broken": 0, "disabled": 0}
    for name, rec in state.items():
        status, missing, parked = record_status(gp, rec)
        records.append({
            "name": name,
            "mod_type": rec.get("mod_type", ""),
            "order": rec.get("order", 0),
            "status": status,
            "missing": missing,
            "parked": parked,
            "files_count": len(rec.get("files", []) or []),
        })
        summary["total"] += 1
        if status == "OK":
            summary["ok"] += 1
        elif status == "BROKEN":
            summary["broken"] += 1
        elif status == "DISABLED":
            summary["disabled"] += 1
    records.sort(key=lambda r: (r["order"] if r["order"] is not None else 0, r["name"]))
    plugins_count, plugins = count_plugins(gp)
    return {
        "game_path": str(gp) if gp else None,
        "summary": summary,
        "records": records,
        "game_version": parse_game_version(gp) if gp else None,
        "frameworks": parse_frameworks(gp) if gp else None,
        "redscript": redscript_result(gp) if gp else None,
        "plugins_loaded": plugins_count,
        "plugins": plugins,
        "untracked": untracked_scan(gp, state) if gp else [],
    }

HTML = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>CP2077 Mod Monitor</title>
<style>
body{background:#121212;color:#e0e0e0;font-family:monospace;margin:20px;}
h1{color:#4a9bff;}
table{border-collapse:collapse;width:100%;}
th,td{border:1px solid #333;padding:6px;text-align:left;font-size:13px;}
th{background:#1e1e1e;}
.ok{color:#4caf50;} .broken{color:#f44336;} .disabled{color:#ff9800;} .untracked{color:#9e9e9e;}
button{background:#1e1e1e;color:#e0e0e0;border:1px solid #4a9bff;padding:6px 12px;cursor:pointer;margin-right:8px;}
.card{background:#1e1e1e;padding:12px;margin:10px 0;border-radius:6px;}
pre{background:#0a0a0a;padding:8px;max-height:200px;overflow:auto;font-size:12px;}
</style></head><body>
<h1>CP2077 Mod Monitor</h1>
<div>
<button onclick="refresh()">Refresh now</button>
<label><input type="checkbox" id="auto" checked> auto-refresh 30s</label>
</div>
<div id="summary" class="card"></div>
<div id="fw" class="card"></div>
<div id="records" class="card"></div>
<div id="untracked" class="card"></div>
<div id="log" class="card"><h3>Logs</h3>
<button onclick="loadLog('red4ext')">red4ext</button>
<button onclick="loadLog('cet')">CET</button>
<button onclick="loadLog('redscript')">redscript</button>
<pre id="logview"></pre></div>
<script>
let timer=null;
function fmt(x){return x==null?'-':x;}
async function refresh(){
  const r=await fetch('/api/state'); const d=await r.json();
  document.getElementById('summary').innerHTML='<h3>Summary</h3>'+
    'Records: '+d.summary.total+' | OK: '+d.summary.ok+' | Broken: '+d.summary.broken+' | Disabled: '+d.summary.disabled+
    '<br>Game: '+fmt(d.game_path)+
    '<br>Game version: '+fmt(d.game_version&&d.game_version.product)+' / '+fmt(d.game_version&&d.game_version.file)+
    '<br>Plugins loaded: '+d.plugins_loaded;
  document.getElementById('fw').innerHTML='<h3>Frameworks</h3>'+
    'RED4ext: '+fmt(d.frameworks&&d.frameworks.red4ext)+' | CET: '+fmt(d.frameworks&&d.frameworks.cet)+
    '<br>Redscript: '+fmt(d.redscript&&d.redscript.result);
  let html='<h3>Records</h3><table><tr><th>Order</th><th>Name</th><th>Type</th><th>Status</th><th>Files</th></tr>';
  for(const rec of d.records){
    const cls=rec.status.toLowerCase();
    html+='<tr><td>'+rec.order+'</td><td>'+rec.name+'</td><td>'+rec.mod_type+'</td><td class="'+cls+'">'+rec.status+'</td><td>'+rec.files_count+(rec.missing.length? ' (missing '+rec.missing.length+')':'')+(rec.parked.length? ' (parked '+rec.parked.length+')':'')+'</td></tr>';
  }
  html+='</table>';
  document.getElementById('records').innerHTML=html;
  let u='<h3>UNTRACKED</h3>';
  if(d.untracked.length===0) u+='none';
  else u+='<ul>'+d.untracked.map(x=>'<li class="untracked">'+x+'</li>').join('')+'</ul>';
  document.getElementById('untracked').innerHTML=u;
}
async function loadLog(name){
  const r=await fetch('/api/log/'+name); const d=await r.json();
  document.getElementById('logview').textContent=(d.lines||[]).join('\\n');
}
function schedule(){
  if(timer) clearInterval(timer);
  if(document.getElementById('auto').checked) timer=setInterval(refresh,30000);
}
document.getElementById('auto').addEventListener('change',schedule);
refresh(); schedule();
</script></body></html>"""

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            data = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/api/state":
            gp = self.server.game_path
            payload = build_state(gp)
            data = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path.startswith("/api/log/"):
            name = urllib.parse.unquote(parsed.path[len("/api/log/"):])
            gp = self.server.game_path
            lines = tail_log(gp, name)
            data = json.dumps({"lines": lines}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *a):
        pass

def tail_log(gp, name):
    # resolve
    if gp is None:
        return []
    aliases = {
        "red4ext": newest_red4ext_log(gp),
        "cet": cet_log(gp),
        "redscript": redscript_log(gp),
    }
    p = aliases.get(name)
    if p is None:
        # search filename
        for cand in [gp / "red4ext" / "logs" / name, gp / "r6" / "logs" / name]:
            if cand.exists():
                p = cand; break
        if p is None:
            # try basename in logs dirs
            for d in [gp / "red4ext" / "logs", gp / "r6" / "logs"]:
                if d.exists():
                    matches = list(d.glob(name))
                    if matches:
                        p = matches[0]; break
    if p is None or not Path(p).exists():
        return []
    try:
        lines = Path(p).read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    return lines[-200:]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", default=None)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    gp = game_path_from(args)
    if args.check:
        st = build_state(gp)
        s = st["summary"]
        print(f"{s['total']} records, {s['ok']} ok, {s['broken']} broken, {s['disabled']} disabled")
        raise SystemExit(0)
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", args.port), Handler)
    httpd.game_path = gp
    httpd.allow_reuse_address = True
    print(f"Monitor on http://127.0.0.1:{args.port}  game={gp}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
