#!/usr/bin/env python3
"""
deepseek.py — DeepSeek Code Agent
"""

# ─────────────────────────────────────────────────────────────────────────────
# stdlib
# ─────────────────────────────────────────────────────────────────────────────
import os, sys, json, re, base64, html, time, threading, webbrowser, shutil, signal, getpass
import subprocess, traceback, urllib.request, urllib.parse
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Generator

# ─────────────────────────────────────────────────────────────────────────────
# ANSI
# ─────────────────────────────────────────────────────────────────────────────
R, BOLD, DIM, ITALIC = "\033[0m", "\033[1m", "\033[38;5;245m", "\033[3m"
CYAN, GREEN, YELLOW, RED, BLUE = "\033[38;5;44m", "\033[38;5;35m", "\033[38;5;220m", "\033[38;5;196m", "\033[38;5;33m"
BCYAN = "\033[38;5;27m"   # accent blue (256-color, SSH-safe)
BBLUE = "\033[38;5;27m"   # accent blue (256-color, SSH-safe)
DBLUE = "\033[38;5;18m"   # deep blue
def c(col, t): return f"{col}{t}{R}"
def bold(t):   return c(BOLD, t)
def dim(t):    return c(DIM, t)

# ─────────────────────────────────────────────────────────────────────────────
# ENVIRONMENT DETECTION
# ─────────────────────────────────────────────────────────────────────────────
IS_TERMUX = (
    "TERMUX_VERSION" in os.environ
    or Path("/data/data/com.termux").exists()
    or "com.termux" in os.environ.get("PREFIX", "")
)
IS_ANDROID = IS_TERMUX or Path("/system/build.prop").exists()

class AuthError(Exception): pass

_live_state = {"fn": None}  # stores header render fn for live resize

# ─────────────────────────────────────────────────────────────────────────────
# PATHS & DEFAULTS
# ─────────────────────────────────────────────────────────────────────────────
DATA_DIR    = Path.home() / ".deepseek"
CONFIG_FILE = DATA_DIR / "config.json"
COOKIE_FILE = DATA_DIR / "cookies.json"
WASM_FILE   = DATA_DIR / "sha3.wasm"
CHATS_FILE  = DATA_DIR / "chats.json"

WASM_URL = ("https://raw.githubusercontent.com/tr1xx-tech/deepseek-code"
            "/main/sha3.wasm")
API_BASE = "https://chat.deepseek.com/api/v0"

VERSION   = "1.0.16"
_RAW_BASE = "https://raw.githubusercontent.com/tr1xx-tech/deepseek-code/main"

_PENDING_UPDATE = None

def _check_update():
    global _PENDING_UPDATE
    try:
        req = urllib.request.Request(f"{_RAW_BASE}/VERSION",
                                     headers={"User-Agent": "deepseek/1.0"})
        remote = urllib.request.urlopen(req, timeout=4).read().decode().strip()
        if remote == VERSION:
            return
        req2 = urllib.request.Request(f"{_RAW_BASE}/deepseek.py",
                                      headers={"User-Agent": "deepseek/1.0"})
        new_src = urllib.request.urlopen(req2, timeout=15).read()
        Path(__file__).resolve().write_bytes(new_src)
        _PENDING_UPDATE = remote
    except Exception:
        pass


DEFAULTS = dict(
    auth_token   = "",
    model        = "flash",
    thinking     = False,
    search       = True,
    confirm_bash = True,
    bash_timeout = 30,
    max_file_kb  = 200,
)

def load_cfg() -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    if CONFIG_FILE.exists():
        try: return {**DEFAULTS, **json.loads(CONFIG_FILE.read_text())}
        except: pass
    return DEFAULTS.copy()

def save_cfg(cfg: dict):
    DATA_DIR.mkdir(exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

def load_cookies() -> tuple:
    ua = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36")
    if COOKIE_FILE.exists():
        try:
            d = json.loads(COOKIE_FILE.read_text())
            return d.get("cookies", {}), d.get("user_agent", ua)
        except: pass
    return {}, ua

def save_cookies(cookies: dict, ua: str):
    COOKIE_FILE.write_text(json.dumps({"cookies": cookies, "user_agent": ua}, indent=2))

# ─────────────────────────────────────────────────────────────────────────────
# LOCAL CHAT TRACKING
# ─────────────────────────────────────────────────────────────────────────────
def _load_local_chats() -> list:
    try: return json.loads(CHATS_FILE.read_text()) if CHATS_FILE.exists() else []
    except: return []

def _save_local_chats(chats: list):
    try: CHATS_FILE.write_text(json.dumps(chats, indent=2))
    except: pass

def _track_chat(chat_id: str, title: str, model: str):
    chats = _load_local_chats()
    chats = [ch for ch in chats if ch.get("id") != chat_id]
    chats.insert(0, {"id": chat_id, "title": title[:60], "model": model, "ts": int(time.time())})
    _save_local_chats(chats[:100])

def _remove_local_chat(chat_id: str):
    chats = _load_local_chats()
    _save_local_chats([ch for ch in chats if ch.get("id") != chat_id])

# ─────────────────────────────────────────────────────────────────────────────
# NODE.JS POW SOLVER (fallback when wasmtime is unavailable — e.g. Termux)
# ─────────────────────────────────────────────────────────────────────────────
_NODE_POW_JS = r"""
const fs = require('fs');
const cfg  = JSON.parse(process.argv[2]);
const wasm = fs.readFileSync(process.argv[3]);

const stub = new Proxy({}, { get: () => () => 0 });

WebAssembly.instantiate(wasm, {
    wasi_snapshot_preview1: stub, env: stub
}).then(({ instance: w }) => {
    const mem = w.exports.memory;

    function writeStr(s) {
        const buf = Buffer.from(s, 'utf8');
        const ptr = w.exports.__wbindgen_export_0(buf.length, 1);
        new Uint8Array(mem.buffer, ptr, buf.length).set(buf);
        return [ptr, buf.length];
    }

    const prefix  = `${cfg.salt}_${cfg.expire_at}_`;
    const retptr  = w.exports.__wbindgen_add_to_stack_pointer(-16);
    const [cp,cl] = writeStr(cfg.challenge);
    const [pp,pl] = writeStr(prefix);

    w.exports.wasm_solve(retptr, cp, cl, pp, pl, cfg.difficulty);

    const dv     = new DataView(mem.buffer);
    const status = dv.getInt32(retptr, true);
    w.exports.__wbindgen_add_to_stack_pointer(16);

    if (status === 0) { process.stderr.write('POW solve failed\n'); process.exit(1); }

    const nonce = Math.trunc(new Float64Array(mem.buffer.slice(retptr+8, retptr+16))[0]);
    process.stdout.write(String(nonce) + '\n');
}).catch(e => { process.stderr.write(e.message+'\n'); process.exit(1); });
"""

# ─────────────────────────────────────────────────────────────────────────────
# POW SOLVER
# ─────────────────────────────────────────────────────────────────────────────
def ensure_wasm():
    if WASM_FILE.exists():
        return
    print(f"  {dim('Downloading POW solver (~25 KB)...')}", end="", flush=True)
    try:
        urllib.request.urlretrieve(WASM_URL, WASM_FILE)
        print(c(GREEN, " done"))
    except Exception as e:
        print(c(RED, f" FAILED: {e}"))
        raise SystemExit(1)

class _POWSolver:
    def __init__(self):
        self._mode = None
        try:
            import wasmtime
            import numpy as _np
            self._np    = _np
            engine      = wasmtime.Engine()
            module      = wasmtime.Module(engine, WASM_FILE.read_bytes())
            self.store  = wasmtime.Store(engine)
            linker      = wasmtime.Linker(engine)
            linker.define_wasi()
            self.inst   = linker.instantiate(self.store, module)
            self.mem    = self.inst.exports(self.store)["memory"]
            self._mode  = "wasmtime"
            return
        except ImportError:
            pass
        except Exception:
            pass
        for cmd in ("node", "nodejs"):
            try:
                r = subprocess.run([cmd, "--version"], capture_output=True, timeout=3)
                if r.returncode == 0:
                    self._node = cmd
                    self._mode = "node"
                    js = DATA_DIR / "pow_solver.js"
                    if not js.exists():
                        js.write_text(_NODE_POW_JS)
                    return
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
        raise SystemExit(
            "POW solver unavailable.\n"
            "  Termux:  pkg install nodejs\n"
            "  Desktop: pip install wasmtime"
        )

    def _write(self, text: str):
        enc = text.encode()
        ptr = self.inst.exports(self.store)["__wbindgen_export_0"](self.store, len(enc), 1)
        mv  = self.mem.data_ptr(self.store)
        for i, b in enumerate(enc):
            mv[ptr + i] = b
        return ptr, len(enc)

    def _solve_wasmtime(self, cfg: dict) -> int:
        prefix = f"{cfg['salt']}_{cfg['expire_at']}_"
        stack  = self.inst.exports(self.store)["__wbindgen_add_to_stack_pointer"]
        retptr = stack(self.store, -16)
        try:
            cp, cl = self._write(cfg["challenge"])
            pp, pl = self._write(prefix)
            self.inst.exports(self.store)["wasm_solve"](
                self.store, retptr, cp, cl, pp, pl, float(cfg["difficulty"]))
            mv     = self.mem.data_ptr(self.store)
            status = int.from_bytes(bytes(mv[retptr:retptr+4]), "little", signed=True)
            if status == 0:
                raise RuntimeError("POW solve failed")
            return int(self._np.frombuffer(bytes(mv[retptr+8:retptr+16]), dtype=self._np.float64)[0])
        finally:
            stack(self.store, 16)

    def _solve_node(self, cfg: dict) -> int:
        js = DATA_DIR / "pow_solver.js"
        if not js.exists():
            js.write_text(_NODE_POW_JS)
        r = subprocess.run(
            [self._node, str(js), json.dumps(cfg), str(WASM_FILE)],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            raise RuntimeError(f"Node POW: {r.stderr.strip()}")
        return int(r.stdout.strip())

    def solve(self, cfg: dict) -> str:
        val    = self._solve_wasmtime(cfg) if self._mode == "wasmtime" else self._solve_node(cfg)
        result = {k: cfg[k] for k in ("algorithm","challenge","salt","signature","target_path")}
        result["answer"] = val
        return base64.b64encode(json.dumps(result).encode()).decode()

# ─────────────────────────────────────────────────────────────────────────────
# DEEPSEEK API CLIENT
# ─────────────────────────────────────────────────────────────────────────────
class DeepSeekClient:
    def __init__(self, auth_token: str):
        try:
            from curl_cffi import requests as _r
            self._req = _r
        except ImportError:
            raise SystemExit(
                "Run: pip install curl-cffi\n"
                "Termux: pkg install libcurl && pip install curl-cffi"
            )
        self.token   = auth_token
        self._pow    = _POWSolver()
        self._cookies, self._ua = load_cookies()
        profile      = "chrome131_android" if IS_ANDROID else "chrome131"
        self._sess   = self._req.Session(impersonate=profile)

    def _headers(self, pow_resp: str = None) -> dict:
        h = {
            "Authorization":   f"Bearer {self.token}",
            "User-Agent":      self._ua,
            "Content-Type":    "application/json",
            "Accept":          "text/event-stream",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer":         "https://chat.deepseek.com/",
            "Origin":          "https://chat.deepseek.com",
        }
        if pow_resp:
            h["x-ds-pow-response"] = pow_resp
        return h

    def _post(self, path: str, body: dict, stream=False, pow_resp=None):
        r = self._sess.post(
            f"{API_BASE}{path}",
            headers  = self._headers(pow_resp),
            cookies  = self._cookies,
            json     = body,
            stream   = stream,
            timeout  = 60,
        )
        if r.status_code == 401:
            raise SystemExit("Auth error — run: deepseek --login")
        if r.status_code == 403:
            raise RuntimeError("Cloudflare blocked. Re-run --login to refresh cookies.")
        r.raise_for_status()
        return r

    def _get(self, path: str, params: dict = None):
        try:
            return self._sess.get(
                f"{API_BASE}{path}",
                headers = self._headers(),
                cookies = self._cookies,
                params  = params or {},
                timeout = 10,
            )
        except Exception:
            return None

    def _delete(self, path: str):
        try:
            return self._sess.delete(
                f"{API_BASE}{path}",
                headers = self._headers(),
                cookies = self._cookies,
                timeout = 10,
            )
        except Exception:
            return None

    def _pow_response(self) -> str:
        r   = self._post("/chat/create_pow_challenge",
                         {"target_path": "/api/v0/chat/completion"})
        cfg = r.json()["data"]["biz_data"]
        return self._pow.solve(cfg)

    def create_session(self) -> str:
        r = self._post("/chat_session/create", {"character_id": None})
        try:
            return r.json()["data"]["biz_data"]["id"]
        except (KeyError, TypeError):
            raise AuthError("invalid token")

    def list_chats(self) -> list:
        r = self._get("/chat_sessions", {"count": 50, "page": 0})
        if r is None:
            return []
        try:
            d = r.json().get("data", {}).get("biz_data", {})
            return d.get("chat_sessions", d.get("sessions", [])) or []
        except Exception:
            return []

    def delete_chat(self, chat_id: str) -> bool:
        r = self._delete(f"/chat_session/{chat_id}")
        return r is not None and r.status_code < 300

    def get_user(self) -> dict:
        """Fetch current user profile. Returns dict with name/email or {}."""
        for path in ("/users/current_user", "/user/current_user", "/user"):
            r = self._get(path)
            if r is None or r.status_code != 200:
                continue
            try:
                d = r.json().get("data", {}).get("biz_data", {})
                if d:
                    return d
            except Exception:
                pass
        return {}

    def stream(self, chat_id: str, prompt: str,
               parent_id=None, thinking=False, search=False) -> Generator:
        pow_resp = self._pow_response()
        payload  = dict(
            chat_session_id   = chat_id,
            parent_message_id = parent_id,
            prompt            = prompt,
            ref_file_ids      = [],
            thinking_enabled  = thinking,
            search_enabled    = search,
        )
        r = self._post("/chat/completion", payload, stream=True, pow_resp=pow_resp)
        for raw in r.iter_lines():
            if not raw or not raw.startswith(b"data: "):
                continue
            try:
                d = json.loads(raw[6:])
            except json.JSONDecodeError:
                continue
            choice = (d.get("choices") or [{}])[0]
            delta  = choice.get("delta", {})
            yield {
                "type":          delta.get("type", "text"),
                "content":       delta.get("content", ""),
                "finish_reason": choice.get("finish_reason"),
                "message_id":    d.get("id"),
            }

# ─────────────────────────────────────────────────────────────────────────────
# BROWSER LOGIN
# ─────────────────────────────────────────────────────────────────────────────
_LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>DeepSeek Code — Login</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,sans-serif;background:#0d0d0d;color:#e0e0e0;
     min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:#161616;border:1px solid #2a2a2a;border-radius:14px;
      padding:44px 40px;max-width:540px;width:100%;box-shadow:0 8px 40px #0008}
h1{font-size:1.35rem;font-weight:700;color:#fff;margin-bottom:6px}
.sub{color:#666;font-size:.82rem;margin-bottom:36px}
.step{display:flex;gap:14px;margin-bottom:26px;align-items:flex-start}
.num{background:#1a2e4a;color:#60a5fa;border-radius:50%;width:30px;height:30px;
     display:flex;align-items:center;justify-content:center;font-size:.72rem;
     font-weight:700;flex-shrink:0;margin-top:1px}
.sb h3{font-size:.85rem;font-weight:600;color:#fff;margin-bottom:5px}
.sb p{font-size:.78rem;color:#777;line-height:1.55;margin-bottom:8px}
.code{background:#0a0a0a;border:1px solid #252525;border-radius:7px;
      padding:9px 13px;font-family:monospace;font-size:.76rem;color:#7dd3fc;
      display:flex;align-items:center;justify-content:space-between;gap:8px;
      cursor:pointer;transition:border-color .18s}
.code:hover{border-color:#3b82f6}
.cp{font-size:.68rem;color:#555;background:none;border:none;cursor:pointer;
    white-space:nowrap;padding:2px 6px;border-radius:4px;transition:all .15s}
.cp:hover{background:#1e293b;color:#93c5fd}
.cp.ok{color:#4ade80}
input{width:100%;background:#0a0a0a;border:1px solid #252525;border-radius:7px;
      padding:10px 13px;color:#e0e0e0;font-size:.85rem;outline:none;
      transition:border-color .18s;margin-top:7px;font-family:monospace}
input:focus{border-color:#3b82f6}
input::placeholder{color:#444}
.btn{width:100%;margin-top:24px;padding:12px;background:#1d4ed8;border:none;
     border-radius:8px;color:#fff;font-size:.9rem;font-weight:600;cursor:pointer;
     transition:background .18s;letter-spacing:.01em}
.btn:hover{background:#2563eb}
.btn:disabled{background:#1e293b;color:#4b5563;cursor:not-allowed}
.status{margin-top:14px;font-size:.78rem;text-align:center;min-height:18px;
        letter-spacing:.01em}
.ok{color:#4ade80} .err{color:#f87171}
a{color:#60a5fa;text-decoration:none}
a:hover{text-decoration:underline}
hr{border:none;border-top:1px solid #1e1e1e;margin:28px 0}
</style>
</head>
<body>
<div class="card">
  <h1>Connect DeepSeek Code</h1>
  <p class="sub">Link your DeepSeek account — 30 seconds</p>

  <div class="step">
    <div class="num">1</div>
    <div class="sb">
      <h3>Log in to DeepSeek</h3>
      <p>A new tab just opened. Sign in with your account.</p>
      <a href="https://chat.deepseek.com" target="_blank">Open chat.deepseek.com →</a>
    </div>
  </div>

  <div class="step">
    <div class="num">2</div>
    <div class="sb">
      <h3>Run this in DevTools Console</h3>
      <p>Press <b>F12</b> → Console, paste and press Enter:</p>
      <div class="code" onclick="copyCode(this)">
        <span id="jscmd">copy(JSON.stringify({t:JSON.parse(localStorage.getItem("userToken")).value,c:document.cookie}))</span>
        <button class="cp" tabindex="-1">Copy</button>
      </div>
      <p style="margin-top:7px">This copies your credentials to clipboard.</p>
    </div>
  </div>

  <div class="step">
    <div class="num">3</div>
    <div class="sb">
      <h3>Paste here and connect</h3>
      <p>Paste the copied text into the field below:</p>
      <input id="inp" type="text" placeholder='{"t":"eyJ...","c":"cf_clearance=..."}' autocomplete="off" spellcheck="false"/>
    </div>
  </div>

  <button class="btn" id="btn" onclick="connect()">Connect</button>
  <div class="status" id="st"></div>
</div>

<script>
function copyCode(el){
  const txt = document.getElementById('jscmd').textContent;
  navigator.clipboard.writeText(txt).then(()=>{
    const b = el.querySelector('.cp');
    b.textContent='Copied!'; b.classList.add('ok');
    setTimeout(()=>{ b.textContent='Copy'; b.classList.remove('ok'); }, 2000);
  });
}

async function connect(){
  const raw = document.getElementById('inp').value.trim();
  const st  = document.getElementById('st');
  const btn = document.getElementById('btn');
  if(!raw){ st.textContent='Paste your credentials first.'; st.className='status err'; return; }

  let payload;
  try { payload = JSON.parse(raw); }
  catch { payload = { t: raw }; }

  btn.disabled=true; btn.textContent='Connecting...'; st.textContent='';
  try{
    const r = await fetch('/callback',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(payload)
    });
    const d = await r.json();
    if(d.ok){
      st.textContent='✓ Connected! You can close this tab.';
      st.className='status ok';
      btn.textContent='Connected ✓';
    } else { throw new Error(d.error||'Unknown error'); }
  } catch(e){
    st.textContent='Error: '+e.message;
    st.className='status err';
    btn.disabled=false; btn.textContent='Connect';
  }
}

window.open('https://chat.deepseek.com','_blank');
document.getElementById('inp').addEventListener('keydown', e=>{ if(e.key==='Enter') connect(); });
</script>
</body>
</html>"""


def _login_terminal(cfg: dict) -> tuple:
    print(f"\n{bold('DeepSeek Login')} {dim('(terminal mode)')}")
    print()
    print("  1. Open  https://chat.deepseek.com  in your browser and log in")
    print()
    print("  2. Get your token using one of:")
    print()
    print(f"  {c(CYAN,'Android Chrome / Kiwi Browser')} — type in address bar:")
    js_alert = "javascript:prompt('Copy token:',JSON.parse(localStorage.getItem('userToken')).value+'|||'+document.cookie)"
    print(f"    {c(YELLOW, js_alert)}")
    print()
    print(f"  {c(CYAN,'Firefox / any DevTools console')}:")
    js_copy  = "copy(JSON.stringify({t:JSON.parse(localStorage.getItem('userToken')).value,c:document.cookie}))"
    print(f"    {c(YELLOW, js_copy)}")
    print()
    print("  3. Paste the result below.")
    print(f"     {dim('Accepts: plain token  OR  {\"t\":\"...\",\"c\":\"cf_clearance=...\"}')} ")
    print()

    raw = input("  Paste here: ").strip()
    if not raw:
        raise ValueError("Nothing pasted")

    token, cookies_str = raw, ""
    if raw.startswith("{"):
        try:
            d = json.loads(raw)
            token       = d.get("t") or d.get("token") or raw
            cookies_str = d.get("c") or d.get("cookies") or ""
        except json.JSONDecodeError:
            pass
    elif "|||" in raw:
        parts       = raw.split("|||", 1)
        token       = parts[0].strip()
        cookies_str = parts[1].strip() if len(parts) > 1 else ""

    cookies = {}
    for part in cookies_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()

    ua = (
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
        if IS_ANDROID else ""
    )
    return token.strip(), cookies, ua


def _login_via_html(cfg: dict) -> tuple:
    result = [None]
    PORT   = 51423

    class H(BaseHTTPRequestHandler):
        def log_message(self, *_): pass

        def do_GET(self):
            body = _LOGIN_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/callback":
                self.send_response(404); self.end_headers(); return
            length = int(self.headers.get("Content-Length", 0))
            data   = json.loads(self.rfile.read(length))
            token  = (data.get("t") or data.get("token") or "").strip()
            cookies_str = (data.get("c") or data.get("cookies") or "").strip()
            ua     = data.get("ua", "")

            if token:
                cookies = {}
                for part in cookies_str.split(";"):
                    part = part.strip()
                    if "=" in part:
                        k, v = part.split("=", 1)
                        cookies[k.strip()] = v.strip()
                result[0] = (token, cookies, ua)
                resp = json.dumps({"ok": True}).encode()
            else:
                resp = json.dumps({"ok": False, "error": "empty token"}).encode()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

    class _S(HTTPServer):
        allow_reuse_address = True
    srv = None
    for _p in range(PORT, PORT + 10):
        try: srv = _S(("127.0.0.1", _p), H); PORT = _p; break
        except OSError: continue
    if srv is None: raise OSError("No port available for login server")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    url = f"http://localhost:{PORT}"
    print(f"\n{bold('DeepSeek Login')}")
    print(f"  Opening {c(CYAN, url)} in your browser…")
    print(f"  {dim('Waiting — follow the 3 steps shown in the browser.')}")
    webbrowser.open(url)

    while result[0] is None:
        time.sleep(0.3)

    srv.shutdown()
    return result[0]


async def _login_via_nodriver() -> tuple:
    import nodriver as uc

    print(f"\n{bold('DeepSeek Login')} {dim('(automated browser)')}")
    print(f"  {dim('A browser window will open — log in normally, then wait.')}")

    browser = await uc.start(
        headless     = False,
        browser_args = ["--window-size=1100,780", "--window-position=100,80"],
    )
    page = await browser.get("https://chat.deepseek.com")

    print(f"  {dim('Waiting for login…')} ", end="", flush=True)
    token = None
    for _ in range(600):
        await page.sleep(1)
        try:
            token = await page.evaluate(
                "(()=>{try{return JSON.parse(localStorage.getItem('userToken'))?.value||null;}catch{return null;}})()"
            )
            if token:
                break
        except Exception:
            pass
    print()

    if not token:
        browser.stop()
        raise TimeoutError("Login timed out (10 min)")

    cookie_str = await page.evaluate("document.cookie")
    ua         = await page.evaluate("navigator.userAgent")
    browser.stop()

    cookies = {}
    for part in (cookie_str or "").split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()

    return token, cookies, ua


def _has_display() -> bool:
    for var in ("SSH_CLIENT", "SSH_TTY", "SSH_CONNECTION"):
        if os.environ.get(var): return False
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"): return False
    return not IS_TERMUX and not IS_ANDROID

def do_login(cfg: dict):
    if IS_TERMUX or not _has_display():
        token, cookies, ua = _login_terminal(cfg)
    else:
        try:
            import nodriver  # noqa
            import asyncio
            token, cookies, ua = asyncio.run(_login_via_nodriver())
        except Exception:
            token, cookies, ua = _login_via_html(cfg)

    cfg["auth_token"] = token
    save_cfg(cfg)
    save_cookies(cookies, ua or "")

    cf = cookies.get("cf_clearance", "")
    print(c(GREEN, f"\n  Logged in ✓"))
    print(f"  Token saved → {CONFIG_FILE}")
    if cf:
        print(f"  Cloudflare cookie captured ✓  {dim('(cf_clearance)')}")
    else:
        print(f"  {dim('No cf_clearance captured — may need re-login if Cloudflare blocks')}")

# ─────────────────────────────────────────────────────────────────────────────
# TOOLS
# ─────────────────────────────────────────────────────────────────────────────
_DANGER = re.compile(
    r"\brm\s+-[rf]|\bsudo\b|>\s*/dev/[sh]|\bmkfs\b|\bdd\b.*of=|"
    r"curl\b.*\|\s*(ba)?sh|wget\b.*\|\s*(ba)?sh|:\(\)\{.*\}|"
    r"chmod\s+[0-7]*7[0-7]*\s+/|\bshutdown\b|\breboot\b"
)

def _confirm(msg: str) -> bool:
    try:
        return input(f"\n{c(RED+BOLD,'⚠')} {msg} {c(YELLOW,'[y/N] ')}").strip().lower() == "y"
    except (EOFError, KeyboardInterrupt):
        return False

def tool_bash(inp: dict, cfg: dict) -> dict:
    cmd     = inp.get("command","").strip()
    timeout = inp.get("timeout", cfg["bash_timeout"])
    if not cmd: return {"error":"empty command"}
    if cfg["confirm_bash"] and _DANGER.search(cmd):
        if not _confirm(f"Run dangerous command?  {dim(cmd[:120])}"):
            return {"stderr":"Cancelled by user.","exit_code":-1}
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=timeout, cwd=os.getcwd())
        return {
            "stdout":    r.stdout[-8000:] if len(r.stdout)>8000 else r.stdout,
            "stderr":    r.stderr[-2000:] if len(r.stderr)>2000 else r.stderr,
            "exit_code": r.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"error":f"Timed out after {timeout}s","exit_code":-1}
    except Exception as e:
        return {"error":str(e),"exit_code":-1}

def tool_read_file(inp: dict, cfg: dict) -> dict:
    p      = Path(inp.get("path",""))
    offset = max(0, inp.get("offset",0))
    limit  = min(inp.get("limit",400), 2000)
    maxb   = cfg["max_file_kb"]*1024
    if not p.exists(): return {"error":f"Not found: {p}"}
    if p.stat().st_size > maxb:
        return {"error":f"File too large ({p.stat().st_size//1024}KB). Use offset/limit."}
    try:
        lines   = p.read_text(errors="replace").splitlines()
        chunk   = lines[offset:offset+limit]
        content = "\n".join(f"{offset+i+1:5d}  {l}" for i,l in enumerate(chunk))
        return {"content":content,"total_lines":len(lines),"range":f"{offset+1}-{offset+len(chunk)}"}
    except Exception as e:
        return {"error":str(e)}

def tool_write_file(inp: dict, cfg: dict) -> dict:
    p       = Path(inp.get("path",""))
    content = inp.get("content","")
    if not p.name: return {"error":"No path given"}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return {"ok":True,"path":str(p),"bytes":len(content.encode())}
    except Exception as e:
        return {"error":str(e)}

def tool_edit_file(inp: dict, cfg: dict) -> dict:
    p   = Path(inp.get("path",""))
    old = inp.get("old","")
    new = inp.get("new","")
    if not p.exists(): return {"error":f"Not found: {p}"}
    if not old: return {"error":"'old' required"}
    try:
        text = p.read_text(errors="replace")
        if old not in text:
            return {"error":f"Exact string not found in {p}",
                    "hint":f"File starts with: {text[:300]!r}"}
        p.write_text(text.replace(old, new, 1))
        return {"ok":True,"other_occurrences":text.count(old)-1}
    except Exception as e:
        return {"error":str(e)}

def tool_list_dir(inp: dict, cfg: dict) -> dict:
    root  = Path(inp.get("path","."))
    depth = min(inp.get("depth",2), 4)
    SKIP  = {"node_modules","__pycache__",".git","venv",".venv","dist","build",".next"}
    if not root.exists(): return {"error":f"Not found: {root}"}
    lines = [str(root.resolve())]
    def walk(p, d, pfx):
        if d > depth: return
        try: entries = sorted(p.iterdir(), key=lambda x:(x.is_file(), x.name.lower()))
        except PermissionError: return
        for i, e in enumerate(entries):
            last = i==len(entries)-1
            ln   = f"{pfx}{'└── ' if last else '├── '}{e.name}"
            if e.is_file(): ln += f"  {e.stat().st_size:,}B"
            lines.append(ln)
            if e.is_dir() and e.name not in SKIP and not e.name.startswith("."):
                walk(e, d+1, pfx+("    " if last else "│   "))
    walk(root, 1, "")
    return {"tree":"\n".join(lines)}

def tool_web_search(inp: dict, cfg: dict) -> dict:
    q = inp.get("query","").strip()
    if not q: return {"error":"No query"}
    try:
        url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(q)}&format=json&no_html=1&skip_disambig=1"
        req = urllib.request.Request(url, headers={"User-Agent":"deepseek/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        results = []
        if data.get("AbstractText"):
            results.append({"title":data.get("Heading",""),"snippet":data["AbstractText"],
                            "url":data.get("AbstractURL","")})
        for t in data.get("RelatedTopics",[])[:8]:
            if isinstance(t,dict) and t.get("Text"):
                results.append({"snippet":t["Text"],"url":t.get("FirstURL","")})
        return {"results":results,"note":"" if results else "No results. Try web_fetch with a URL."}
    except Exception as e:
        return {"error":str(e)}

def _strip_html(raw: str) -> str:
    raw = re.sub(r'<script[^>]*>.*?</script>','',raw,flags=re.DOTALL|re.I)
    raw = re.sub(r'<style[^>]*>.*?</style>','',raw,flags=re.DOTALL|re.I)
    raw = re.sub(r'<[^>]+>',' ',raw)
    raw = html.unescape(raw)
    return re.sub(r'\s+',' ',raw).strip()

def tool_web_fetch(inp: dict, cfg: dict) -> dict:
    url    = inp.get("url","").strip()
    maxch  = min(inp.get("max_chars",6000), 30000)
    if not url: return {"error":"No URL"}
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            ct  = r.headers.get("Content-Type","")
        text = _strip_html(raw.decode("utf-8","replace")) if "html" in ct \
               else raw.decode("utf-8","replace")
        return {"content":text[:maxch],"truncated":len(text)>maxch,"url":url}
    except Exception as e:
        return {"error":str(e)}

def tool_python(inp: dict, cfg: dict) -> dict:
    code    = inp.get("code","").strip()
    timeout = inp.get("timeout",15)
    if not code: return {"error":"No code"}
    try:
        r = subprocess.run([sys.executable,"-c",code],
                           capture_output=True,text=True,timeout=timeout)
        return {"stdout":r.stdout[-5000:],"stderr":r.stderr[-2000:],"exit_code":r.returncode}
    except subprocess.TimeoutExpired:
        return {"error":f"Timed out after {timeout}s"}
    except Exception as e:
        return {"error":str(e)}

TOOLS = {
    "bash":       tool_bash,
    "read_file":  tool_read_file,
    "write_file": tool_write_file,
    "edit_file":  tool_edit_file,
    "list_dir":   tool_list_dir,
    "web_search": tool_web_search,
    "web_fetch":  tool_web_fetch,
    "python":     tool_python,
}

# ─────────────────────────────────────────────────────────────────────────────
# SYSTEM PROMPT
# ─────────────────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """\
You are DeepSeek Code, a powerful AI assistant running on the user's local machine. \
You take REAL actions: run shell commands, read/write files, search the web, execute Python.

## Tool call format

Output a <tool_call> block with JSON. One tool per block. Wait for <tool_result> before the next.

## Available tools

bash — run a shell command
<tool_call>
{"name": "bash", "input": {"command": "ls -la"}}
</tool_call>

read_file — read file with line numbers (offset/limit for large files)
<tool_call>
{"name": "read_file", "input": {"path": "src/main.py", "offset": 0, "limit": 200}}
</tool_call>

write_file — create or overwrite a file
<tool_call>
{"name": "write_file", "input": {"path": "hello.py", "content": "print('hi')"}}
</tool_call>

edit_file — exact find-and-replace (first occurrence)
<tool_call>
{"name": "edit_file", "input": {"path": "app.py", "old": "def foo():", "new": "def bar():"}}
</tool_call>

list_dir — directory tree
<tool_call>
{"name": "list_dir", "input": {"path": ".", "depth": 2}}
</tool_call>

web_search — DuckDuckGo (no key needed)
<tool_call>
{"name": "web_search", "input": {"query": "python asyncio tutorial"}}
</tool_call>

web_fetch — download any URL as plain text
<tool_call>
{"name": "web_fetch", "input": {"url": "https://docs.python.org/3/", "max_chars": 5000}}
</tool_call>

python — execute Python and capture output
<tool_call>
{"name": "python", "input": {"code": "import math; print(math.pi)"}}
</tool_call>

## Rules
1. Brief plan first, then act.
2. One tool at a time — read the result, then decide next step.
3. Read files before writing or editing them.
4. Test code after writing it (bash or python tool).
5. On errors: diagnose, fix, retry.
6. Use relative paths from the working directory below.

CWD: {cwd}
Platform: {platform}
"""

_TOOL_RE = re.compile(r'<tool_call>\s*(\{.*?\})\s*</tool_call>', re.DOTALL)

def parse_calls(text: str) -> list:
    calls = []
    for m in _TOOL_RE.finditer(text):
        try: calls.append(json.loads(m.group(1)))
        except json.JSONDecodeError: pass
    return calls

# ─────────────────────────────────────────────────────────────────────────────
# AGENT
# ─────────────────────────────────────────────────────────────────────────────
class Agent:
    def __init__(self, cfg: dict):
        self.cfg         = cfg
        self.client      = DeepSeekClient(cfg["auth_token"])
        self.chat_id     = self.client.create_session()
        self.chat_title  = "New chat"
        self.parent_id   = None
        self._first_turn = True
        try:
            _u = self.client.get_user()
            self.user_name = (
                _u.get("nickname") or _u.get("name") or
                _u.get("username") or _u.get("email") or ""
            ).split("@")[0]  # strip domain from email if needed
        except Exception:
            self.user_name = ""

    def _new_session(self):
        self.chat_id     = self.client.create_session()
        self.chat_title  = "New chat"
        self.parent_id   = None
        self._first_turn = True

    def _load_chat(self, chat_id: str, title: str = ""):
        self.chat_id     = chat_id
        self.chat_title  = title or "Loaded chat"
        self.parent_id   = None
        self._first_turn = False

    def _stream(self, prompt: str) -> str:
        r1       = self.cfg["model"] == "r1"
        thinking = self.cfg["thinking"] and r1
        search   = self.cfg["search"]
        buf      = []
        in_think = False

        print()

        try:
            for chunk in self.client.stream(
                self.chat_id, prompt, self.parent_id, thinking, search
            ):
                kind, content = chunk["type"], chunk["content"]
                if kind == "thinking":
                    if not in_think:
                        print(f"{dim('╭─ thinking ─────────────────')}", flush=True)
                        in_think = True
                    print(dim(content), end="", flush=True)
                elif kind == "text":
                    if in_think:
                        print(f"\n{dim('╰────────────────────────────')}\n", flush=True)
                        in_think = False
                    print(content, end="", flush=True)
                    buf.append(content)
                if chunk.get("finish_reason") == "stop":
                    mid = chunk.get("message_id")
                    if mid: self.parent_id = mid
        except Exception as e:
            print(c(RED, f"\nStream error: {e}"))
        print()
        return "".join(buf)

    def _run_tool(self, name: str, inp: dict) -> str:
        fn = TOOLS.get(name)
        if fn is None:
            result = {"error": f"Unknown tool '{name}'"}
        else:
            label = {
                "bash":      f"$ {inp.get('command','')[:90]}",
                "read_file": inp.get("path",""),
                "write_file":f"→ {inp.get('path','')}",
                "edit_file": inp.get("path",""),
                "list_dir":  inp.get("path","."),
                "web_search":f'"{inp.get("query","")[:70]}"',
                "web_fetch": inp.get("url","")[:80],
                "python":    inp.get("code","")[:60].replace("\n"," "),
            }.get(name, "")
            print(f"\n  {c(YELLOW,'▶')} {bold(name)}  {dim(label)}", flush=True)
            try:
                result = fn(inp, self.cfg)
            except Exception:
                result = {"error": traceback.format_exc()}

        if "error" in result:
            print(f"  {c(RED,'✗')} {result['error'][:200]}")
        else:
            out = (result.get("stdout") or result.get("content") or
                   result.get("tree") or "")
            if out:
                lines = out.strip().splitlines()
                for ln in lines[:14]: print(f"  {dim('│')} {ln}")
                if len(lines) > 14: print(f"  {dim('│')} … +{len(lines)-14} lines")
            ec = result.get("exit_code")
            if ec is not None:
                mark = c(GREEN,"✓") if ec==0 else c(RED,f"✗ exit {ec}")
                print(f"  {mark}")
            elif result.get("ok"):
                print(f"  {c(GREEN,'✓')}")

        return json.dumps(result, ensure_ascii=False, indent=2)

    def turn(self, user_msg: str):
        if self._first_turn:
            self.chat_title  = user_msg[:50].replace('\n', ' ')
            _track_chat(self.chat_id, self.chat_title, self.cfg["model"])
            prompt = (SYSTEM_PROMPT.replace("{cwd}", os.getcwd()).replace("{platform}", sys.platform)
                      + "\n\n---\nUser: " + user_msg)
            self._first_turn = False
        else:
            prompt = user_msg

        for _ in range(25):
            response = self._stream(prompt)
            if not response: break
            calls = parse_calls(response)
            if not calls: break
            parts = []
            for call in calls:
                name = call.get("name","")
                inp  = call.get("input",{})
                res  = self._run_tool(name, inp)
                parts.append(f'<tool_result name="{name}">\n{res}\n</tool_result>')
            prompt = "\n\n".join(parts)

# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
_MNAMES = {"flash": "deepseek-v4-flash", "pro": "deepseek-v4-pro", "r1": "deepseek-r1"}


def _tty():  return sys.stdout.isatty()
def _cols(): return shutil.get_terminal_size((80, 24)).columns
def _rows(): return shutil.get_terminal_size((80, 24)).lines

def _enter_app():
    if _tty(): sys.stdout.write("\033[?1049h\033[H"); sys.stdout.flush()

def _exit_app():
    if _tty(): sys.stdout.write("\033[?1049l"); sys.stdout.flush()

def _cls():
    if _tty(): sys.stdout.write("\033[2J\033[H"); sys.stdout.flush()

def _vis_len(s: str) -> int:
    return len(re.sub(r'\033\[[^m]*m', '', s))

def _box(lines, title=""):
    cols = _cols()
    W    = max(40, min(cols - 2, 120))
    d    = W - 2
    cw   = W - 4

    if title:
        t   = f" {title} "
        top = c(BCYAN, "╭─" + t + "─" * max(0, d - len(t) - 1) + "╮")
    else:
        top = c(BCYAN, "╭" + "─" * d + "╮")

    rows = [top]
    for ln in lines:
        vl  = _vis_len(ln)
        pad = " " * max(0, cw - vl)
        rows.append(c(BCYAN, "│") + " " + ln + pad + " " + c(BCYAN, "│"))
    rows.append(c(BCYAN, "╰" + "─" * d + "╯"))
    return "\n".join(rows)

def _sep(label=""):
    cols = _cols()
    s    = f"── {label} " if label else "── "
    fill = "─" * max(0, cols - len(s) - 1)
    return c(DBLUE, s + fill)

def _kv(k, v, w=11):
    return f"  {c(BLUE+DIM, k)}{' ' * max(0, w - len(k))}{v}"

def _welcome_lines(cfg, chat_id, chat_title, user_name=""):
    mn  = _MNAMES.get(cfg["model"], cfg["model"])
    cwd = os.getcwd().replace(str(Path.home()), "~")
    if user_name:
        uname = user_name
    else:
        try:    uname = getpass.getuser()
        except: uname = os.environ.get("USER") or os.environ.get("USERNAME") or "there"
    av1 = c(BCYAN+BOLD, "▐▟██▛▌")
    av2 = c(BCYAN+BOLD, "▐█▄██▌")
    return [
        "",
        f"  {av1}  {bold('Welcome back, ' + uname + '!')}",
        f"  {av2}  {c(DIM, 'Send /help for help · /exit to quit.')}",
        "",
        f"  {c(DIM, 'directory:')}  {c(DIM, cwd)}",
        f"  {c(DIM, 'model:')}      {c(DIM, mn)}",
        f"  {c(DIM, 'chat:')}       {c(DIM, chat_title[:48] if chat_title else 'New chat')}",
        "",
    ]

def _show_welcome(cfg, chat_id, chat_title, user_name=""):
    _cls()
    # Store render function for live SIGWINCH redraws
    def _render():
        return ("\n" +
                _box(_welcome_lines(cfg, chat_id, chat_title, user_name),
                     title=f"DeepSeek Code  v{VERSION}") +
                "\n\n" + _sep("input") + "\n")
    _live_state["fn"] = _render
    print(_render(), end="")

def _show_update_page(new_ver):
    _cls()
    print()
    print(_box([
        "",
        f"  {c(GREEN+BOLD, '↑')}  {bold('Update available')}",
        "",
        f"  {c(DIM, VERSION)}  {c(BCYAN,'→')}  {c(BCYAN+BOLD, new_ver)}",
        "",
    ], title="DeepSeek Code"))
    print()
    try:
        ans = input(f"  {c(BBLUE, 'Restart now?')} {c(DIM, '[y/n]')} ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        ans = "n"
    if ans == "y":
        _exit_app()
        os.execv(sys.executable, [sys.executable] + sys.argv)

def _help_box():
    def row(cmd, desc, w=22):
        pad = " " * max(0, w - _vis_len(cmd))
        return f"  {c(BCYAN, cmd)}{pad}{c(DIM, desc)}"
    def sec(name):
        return f"\n  {c(BBLUE+BOLD, name)}"
    return _box([
        sec("model"),
        row("/model flash",  "deepseek-v4-flash  · fast, default"),
        row("/model pro",    "deepseek-v4-pro    · smarter"),
        row("/model r1",     "deepseek-r1        · reasoning"),
        sec("chat"),
        row("/new",          "start a new chat"),
        row("/chats",        "list all chats"),
        row("/chat <n>",     "switch to chat by number"),
        row("/delete [n]",   "delete current or nth chat"),
        row("/cwd [path]",   "change working directory"),
        row("/login",        "re-authenticate"),
        sec("settings"),
        row("/search",       "toggle web search (on by default)"),
        row("/thinking",     "toggle r1 reasoning trace"),
        row("/confirm",      "toggle shell confirmation"),
        row("/status",       "show current settings"),
        sec("other"),
        row("/help",         "this page"),
        row("/exit",         "quit"),
        "",
        f"  {c(DIM, 'tools: bash · read_file · write_file · edit_file')}",
        f"  {c(DIM, '       list_dir · web_search · web_fetch · python')}",
        "",
    ], title="DeepSeek Code  /help")

def _status_box(cfg, chat_id="", chat_title=""):
    mn  = _MNAMES.get(cfg["model"], cfg["model"])
    cwd = os.getcwd().replace(str(Path.home()), "~")
    on  = c(GREEN+BOLD, "on")
    off = c(DIM, "off")
    lines = [
        "",
        _kv("model",     c(BCYAN+BOLD, mn)),
        _kv("search",    on if cfg["search"]       else off),
        _kv("thinking",  on if cfg["thinking"]     else off),
        _kv("confirm",   on if cfg["confirm_bash"] else off),
        _kv("directory", c(DIM, cwd)),
        _kv("version",   c(DIM, VERSION)),
    ]
    if chat_title:
        lines.append(_kv("chat", c(DIM, chat_title[:48])))
    lines.append("")
    return _box(lines, title="DeepSeek Code  /status")

def _show_chats(agent) -> list:
    """Show chat list and return the entries for /chat <n> and /delete <n>."""
    api_chats   = agent.client.list_chats()
    local_chats = _load_local_chats()

    if api_chats:
        entries = []
        for ch in api_chats[:30]:
            cid   = ch.get("id", "")
            title = (ch.get("title") or ch.get("name") or "Untitled")[:50]
            model = ch.get("model", "")
            entries.append({"id": cid, "title": title, "model": model})
    else:
        entries = [{"id": ch.get("id",""), "title": ch.get("title","Untitled")[:50],
                    "model": ch.get("model","")} for ch in local_chats[:30]]

    lines = [
        "",
        f"  {c(BBLUE+BOLD, 'Chats')}  {c(DIM, str(len(entries)) + ' total')}",
        "",
    ]
    for i, e in enumerate(entries):
        num   = c(DBLUE+BOLD, f"{i+1:2d}")
        curr  = c(BCYAN+BOLD, " ◆") if e["id"] == agent.chat_id else "  "
        title = e["title"]
        tid   = c(DIM, "  " + e["id"][:8] + "…")
        lines.append(f"  {num}{curr}  {title}{tid}")
    if not entries:
        lines.append(f"  {c(DIM, 'No chats yet — start a conversation to create one.')}")
    lines += [
        "",
        f"  {c(DIM, '/chat <n>  switch  ·  /new  new chat  ·  /delete [n]  delete')}",
        "",
    ]
    _cls()
    print()
    print(_box(lines, title="DeepSeek Code  /chats"))
    print()
    print(_sep("input"))
    print()
    return entries

def _delete_chat_cmd(agent, cfg, arg, entries):
    if arg:
        try:
            n = int(arg) - 1
            target = entries[n]
        except (ValueError, IndexError, TypeError):
            print(c(RED, "  Usage: /delete [n]  (run /chats to see numbers)"))
            return
    else:
        target = {"id": agent.chat_id, "title": agent.chat_title}

    is_current = target["id"] == agent.chat_id
    try:
        ans = input(f"  Delete {c(YELLOW, repr(target['title'][:40]))}? {c(DIM,'[y/N]')} ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        return
    if ans != "y":
        return

    ok = agent.client.delete_chat(target["id"])
    _remove_local_chat(target["id"])

    if ok:
        print(f"  {c(GREEN,'✓')}  deleted")
    else:
        print(f"  {c(YELLOW,'⚠')}  removed locally (API delete may have failed)")

    if is_current:
        agent._new_session()
        _show_welcome(cfg, agent.chat_id, agent.chat_title, getattr(agent, "user_name", ""))

def main():
    cfg  = load_cfg()
    args = sys.argv[1:]

    if "--update" not in args:
        _upd = threading.Thread(target=_check_update, daemon=True)
        _upd.start()
        _upd.join(timeout=5)

    if "--login" in args or not cfg["auth_token"]:
        do_login(cfg)
        if not cfg["auth_token"]:
            raise SystemExit("Login failed — no token captured.")

    ensure_wasm()
    _enter_app()

    # SIGWINCH: immediately redraw header in-place (live resize)
    def _sigwinch_handler(sig, frame):
        fn = _live_state.get("fn")
        if fn is None or not sys.stdout.isatty():
            return
        try:
            content = fn()
            lines   = content.splitlines()
            out = [b"\0337\033[?25l"]          # DECSC save cursor + hide cursor
            for i, ln in enumerate(lines):
                out.append(f"\033[{i+1};1H\033[K{ln}".encode())
            out.append(b"\033[?25h\0338")      # show cursor + DECRC restore cursor
            os.write(1, b"".join(out))
            try:
                import readline as _rl; _rl.redisplay()
            except Exception:
                pass
        except Exception:
            pass
    try:
        signal.signal(signal.SIGWINCH, _sigwinch_handler)
    except (AttributeError, OSError):
        pass

    try:
        if _PENDING_UPDATE:
            _show_update_page(_PENDING_UPDATE)

        _cls()
        print()

        _running = [True]
        def _spin():
            frames = ['⠋','⠙','⠹','⠸','⠼','⠴','⠦','⠧','⠇','⠏']
            i = 0
            while _running[0]:
                sys.stdout.write(f"\r  {c(BCYAN, frames[i % 10])}  {c(DIM, 'connecting...')}")
                sys.stdout.flush()
                time.sleep(0.08)
                i += 1
            sys.stdout.write("\r" + " " * 30 + "\r")
            sys.stdout.flush()

        spin_t = threading.Thread(target=_spin, daemon=True)
        spin_t.start()

        try:
            agent = Agent(cfg)
        except AuthError:
            _running[0] = False; spin_t.join(timeout=1)
            cfg["auth_token"] = ""; save_cfg(cfg)
            _cls()
            print()
            print(_box([
                "",
                f"  {c(RED+BOLD, '✗')}  {bold('Token invalid — please log in again')}",
                "",
            ], title="DeepSeek Code"))
            print()
            do_login(cfg)
            if not cfg["auth_token"]: raise SystemExit("Login failed.")
            _cls(); print()
            _running[0] = True
            spin_t = threading.Thread(target=_spin, daemon=True)
            spin_t.start()
            agent = Agent(cfg)

        _running[0] = False
        spin_t.join(timeout=1)

        _show_welcome(cfg, agent.chat_id, agent.chat_title, getattr(agent, "user_name", ""))

        def toggle(key, arg):
            cfg[key] = arg=="on" if arg in ("on","off") else not cfg[key]
            save_cfg(cfg); return "on" if cfg[key] else "off"

        _last_chats: list = []

        while True:
            try:
                line = input(f"{c(BBLUE+BOLD, '❯')} ").strip()
            except (KeyboardInterrupt, EOFError):
                print(f"\n{c(DIM, 'bye')}")
                break
            if not line: continue

            if line.startswith("/"):
                parts = line[1:].split(maxsplit=1)
                cmd   = parts[0].lower()
                arg   = parts[1].strip() if len(parts)>1 else ""

                if cmd == "exit":
                    print(c(DIM, "bye")); break

                elif cmd == "help":
                    _cls(); print(); print(_help_box()); print()
                    print(_sep("input")); print()

                elif cmd == "status":
                    _cls(); print(); print(_status_box(cfg, agent.chat_id, agent.chat_title)); print()
                    print(_sep("input")); print()

                elif cmd in ("new", "clear"):
                    agent._new_session()
                    _show_welcome(cfg, agent.chat_id, agent.chat_title, getattr(agent, "user_name", ""))

                elif cmd == "chats":
                    _last_chats = _show_chats(agent)

                elif cmd == "chat":
                    if not arg:
                        print(c(YELLOW, "  Usage: /chat <n>  (run /chats first)"))
                    else:
                        try:
                            n = int(arg) - 1
                            if _last_chats and 0 <= n < len(_last_chats):
                                e = _last_chats[n]
                                agent._load_chat(e["id"], e["title"])
                                _show_welcome(cfg, agent.chat_id, agent.chat_title, getattr(agent, "user_name", ""))
                            else:
                                print(c(YELLOW, "  Run /chats first to see chat numbers"))
                        except ValueError:
                            # treat as raw ID
                            agent._load_chat(arg)
                            _show_welcome(cfg, agent.chat_id, agent.chat_title, getattr(agent, "user_name", ""))

                elif cmd == "delete":
                    _delete_chat_cmd(agent, cfg, arg, _last_chats)

                elif cmd == "login":
                    do_login(cfg)
                    agent.client._cookies, agent.client._ua = load_cookies()
                    print(f"  {c(GREEN,'✓')}  {c(DIM, 'logged in')}")

                elif cmd == "model":
                    if arg in ("flash","pro","r1"):
                        cfg["model"] = arg; save_cfg(cfg)
                    print(f"  {c(BCYAN+BOLD, _MNAMES.get(cfg['model'], cfg['model']))}")

                elif cmd == "thinking":
                    v = toggle("thinking", arg)
                    print(f"  thinking  {c(GREEN+BOLD,'on') if v=='on' else c(DIM,'off')}")

                elif cmd == "search":
                    v = toggle("search", arg)
                    print(f"  search  {c(GREEN+BOLD,'on') if v=='on' else c(DIM,'off')}")

                elif cmd == "confirm":
                    v = toggle("confirm_bash", arg)
                    print(f"  confirm  {c(GREEN+BOLD,'on') if v=='on' else c(DIM,'off')}")

                elif cmd == "cwd":
                    if arg:
                        try:
                            os.chdir(arg)
                            cwd = os.getcwd().replace(str(Path.home()), "~")
                            print(f"  {c(BLUE+DIM,'directory')}  {c(DIM, cwd)}")
                        except Exception as e:
                            print(c(RED, f"  {e}"))
                    else:
                        cwd = os.getcwd().replace(str(Path.home()), "~")
                        print(f"  {c(BLUE+DIM,'directory')}  {c(DIM, cwd)}")

                else:
                    print(c(YELLOW, "  unknown command — /help"))
                continue

            try:
                agent.turn(line)
            except KeyboardInterrupt:
                print(f"\n{c(YELLOW,'[interrupted]')}")

    finally:
        _exit_app()

if __name__ == "__main__":
    main()
