"""Read-only mobile dashboard, served over Tailscale only.

Binds to the Mac's Tailscale IP (100.x) so it is reachable from the
user's phone on the same tailnet and from nowhere else. Falls back to
127.0.0.1 if Tailscale is not running. Started as a daemon thread by
menubar.py.
"""
import html
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from common import HANDOFF_DIR, get_config, get_lang, log, read_json
from sessions import age_str, scan_projects
from usage import UsageError, fetch_usage, format_resets_at
TAILSCALE_BINS = ["/usr/local/bin/tailscale",
                  "/Applications/Tailscale.app/Contents/MacOS/Tailscale"]


def tailscale_ip() -> str:
    for binpath in TAILSCALE_BINS:
        try:
            out = subprocess.run([binpath, "ip", "-4"], capture_output=True,
                                 text=True, timeout=5)
            ip = out.stdout.strip().splitlines()[0] if out.returncode == 0 else ""
            if ip.startswith("100."):
                return ip
        except Exception:
            continue
    return ""


def _state() -> dict:
    lang = get_lang()
    state = {"usage": None, "usage_error": None, "projects": [], "lang": lang}
    try:
        state["usage"] = fetch_usage()
    except UsageError as e:
        state["usage_error"] = str(e)
    for p in scan_projects()[:get_config()["max_projects"]]:
        slug = Path(p.dir_name).name
        meta = read_json(HANDOFF_DIR / f"{slug}.meta.json", {}) or {}
        has_handoff = (HANDOFF_DIR / f"{slug}.md").exists()
        state["projects"].append({
            "name": p.name, "cwd": p.cwd, "slug": slug,
            "last_active": age_str(p.last_active),
            "handoff": {"exists": has_handoff,
                        "generated_at": meta.get("generated_at", ""),
                        "method": meta.get("method", "")},
            "sessions": [{"label": s.label or s.session_id[:8],
                          "age": age_str(s.mtime), "id": s.session_id}
                         for s in p.sessions[:5]],
        })
    return state


PAGE = """<!doctype html><html lang="ja"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>aiagent-control</title><style>
body{font-family:-apple-system,sans-serif;background:#111;color:#eee;margin:0;padding:16px}
h1{font-size:18px;margin:0 0 12px}
.bar{background:#333;border-radius:6px;height:22px;position:relative;margin:6px 0}
.bar>div{height:100%;border-radius:6px;background:#2a9d5c}
.bar.warn>div{background:#d4a017}.bar.hot>div{background:#c0392b}
.bar span{position:absolute;left:8px;top:2px;font-size:13px}
.proj{background:#1c1c1e;border-radius:10px;padding:12px;margin:10px 0}
.proj h2{font-size:15px;margin:0 0 6px}
.sess{font-size:13px;color:#aaa;padding:3px 0;border-top:1px solid #2a2a2c}
.ho{font-size:12px;color:#7fb3ff;text-decoration:none}
.age{color:#666;font-size:11px;margin-left:6px}
.err{color:#e57373;font-size:13px}
pre{white-space:pre-wrap;font-size:13px;background:#1c1c1e;padding:12px;border-radius:10px}
a{color:#7fb3ff}
</style></head><body>
<h1>🤖 aiagent-control</h1><div id="app">loading…</div>
<script>
async function load(){
  const r = await fetch('/api/state'); const s = await r.json();
  let h = '';
  if (s.usage_error) h += `<div class="err">${s.usage_error}</div>`;
  if (s.usage){
    const w = (label,win)=>{ if(!win||win.utilization==null) return '';
      const u=Math.round(win.utilization);
      const cls=u>=90?'hot':u>=70?'warn':'';
      return `<div class="bar ${cls}"><div style="width:${Math.min(u,100)}%"></div><span>${label} ${u}%</span></div>`; };
    h += w(s.lang==='ja'?'5時間枠':'5h window', s.usage.five_hour) + w(s.lang==='ja'?'週間枠':'weekly', s.usage.seven_day);
    for (const [k,v] of Object.entries(s.usage.extras||{})) h += w(k, v);
  }
  for (const p of s.projects){
    h += `<div class="proj"><h2>${p.name}<span class="age">${p.last_active}</span></h2>`;
    if (p.handoff.exists)
      h += `<a class="ho" href="/handoff/${p.slug}">📝 handoff (${(p.handoff.generated_at||'').slice(0,16).replace('T',' ')} / ${p.handoff.method})</a>`;
    else h += `<span class="ho" style="color:#666">📝 -</span>`;
    for (const x of p.sessions)
      h += `<div class="sess">${x.label}<span class="age">${x.age}</span></div>`;
    h += '</div>';
  }
  document.getElementById('app').innerHTML = h;
}
load(); setInterval(load, 60000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body: bytes, ctype: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            if self.path == "/" or self.path == "":
                self._send(PAGE.encode(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self._send(json.dumps(_state(), ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
            elif self.path.startswith("/handoff/"):
                slug = Path(self.path[len("/handoff/"):]).name  # no traversal
                f = HANDOFF_DIR / f"{slug}.md"
                if f.exists():
                    body = ("<!doctype html><meta charset='utf-8'>"
                            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                            "<style>body{background:#111;color:#eee;font-family:-apple-system}"
                            "pre{white-space:pre-wrap;font-size:14px;padding:12px}</style>"
                            "<a href='/' style='color:#7fb3ff;padding:12px;display:block'>← back</a>"
                            f"<pre>{html.escape(f.read_text())}</pre>")
                    self._send(body.encode(), "text/html; charset=utf-8")
                else:
                    self._send(b"not found", "text/plain", 404)
            else:
                self._send(b"not found", "text/plain", 404)
        except Exception as e:
            log("webdash", f"ERROR {self.path}: {e}")
            try:
                self._send(b"error", "text/plain", 500)
            except Exception:
                pass


def start_in_background() -> str:
    """Start the server thread. Returns the URL, or '' if it failed."""
    ip = tailscale_ip() or "127.0.0.1"
    try:
        server = ThreadingHTTPServer((ip, get_config()["dashboard_port"]), Handler)
    except OSError as e:
        log("webdash", f"cannot bind {ip}: {e}")
        return ""
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://{ip}:{get_config()['dashboard_port']}"
    log("webdash", f"serving on {url}")
    return url


if __name__ == "__main__":
    url = start_in_background()
    print(url or "failed to start")
    import time
    time.sleep(3600)
