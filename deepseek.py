#!/usr/bin/env python3
"""
deepseek.py — DeepSeek Code Agent
"""

# ─────────────────────────────────────────────────────────────────────────────
# stdlib
# ─────────────────────────────────────────────────────────────────────────────
import os, sys, json, re, base64, html, time, threading, shutil, signal, getpass
import subprocess, traceback, urllib.request, urllib.parse
from pathlib import Path
from typing import Generator

# ─────────────────────────────────────────────────────────────────────────────
# ANSI
# ─────────────────────────────────────────────────────────────────────────────
R, BOLD, DIM, ITALIC = "\033[0m", "\033[1m", "\033[38;5;245m", "\033[3m"
CYAN, GREEN, YELLOW, RED, BLUE = "\033[38;5;44m", "\033[38;5;35m", "\033[38;5;220m", "\033[38;5;196m", "\033[38;5;33m"
BCYAN = "\033[38;5;33m"   # accent blue (256-color, SSH-safe)
BBLUE = "\033[38;5;33m"   # accent blue (256-color, SSH-safe)
DBLUE = "\033[38;5;240m"  # gray — used for input separator line
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
HIST_DIR    = DATA_DIR / "history"

WASM_URL = ("https://raw.githubusercontent.com/tr1xx-tech/deepseek-code"
            "/main/sha3.wasm")
API_BASE = "https://chat.deepseek.com/api/v0"

VERSION   = "0.57"
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
    model        = "chat",
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
    hist = HIST_DIR / f"{chat_id}.json"
    try: hist.unlink(missing_ok=True)
    except: pass

def _load_history(chat_id: str) -> list:
    try:
        f = HIST_DIR / f"{chat_id}.json"
        return json.loads(f.read_text()) if f.exists() else []
    except: return []

def _append_history(chat_id: str, role: str, text: str):
    try:
        HIST_DIR.mkdir(parents=True, exist_ok=True)
        f    = HIST_DIR / f"{chat_id}.json"
        msgs = _load_history(chat_id)
        msgs.append({"role": role, "text": text, "ts": int(time.time())})
        f.write_text(json.dumps(msgs, ensure_ascii=False))
    except: pass

def _update_chat_title(chat_id: str, title: str):
    if not title: return
    chats = _load_local_chats()
    for ch in chats:
        if ch.get("id") == chat_id:
            ch["title"] = title[:60]
            break
    _save_local_chats(chats)

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
        biz = r.json()["data"]["biz_data"]
        cfg = biz.get("challenge", biz)
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
        # DeepSeek SSE patch format:
        #   {"p":"response/content","o":"APPEND","v":"text"}  — text chunk
        #   {"v":"text"}                                       — continuation chunk
        #   {"p":"response/thinking_content","o":"APPEND","v":"..."} — thinking chunk
        #   {"p":"response/status","v":"FINISHED"}            — done
        # SSE events: "event: title / data: {"content":"..."}" — chat title
        message_id  = None
        cur_path    = "response/content"
        cur_event   = None
        _SENTINEL   = object()
        for raw in r.iter_lines():
            if not raw:
                cur_event = None
                continue
            if raw.startswith(b"event: "):
                cur_event = raw[7:].decode().strip()
                continue
            if not raw.startswith(b"data: "):
                continue
            try:
                d = json.loads(raw[6:])
            except json.JSONDecodeError:
                continue
            if cur_event == "title":
                title = d.get("content", "")
                if title:
                    yield {"type": "title", "content": title, "finish_reason": None, "message_id": None}
                cur_event = None
                continue
            p = d.get("p", _SENTINEL)
            v = d.get("v")
            # init block: {"v": {"response": {"message_id": N, ...}}}
            if p is _SENTINEL and isinstance(v, dict) and "response" in v:
                mid = v["response"].get("message_id")
                if mid is not None:
                    message_id = mid
                continue
            # status/control patches
            if p is not _SENTINEL and p in ("response/status", "response/accumulated_token_usage"):
                if p == "response/status" and v == "FINISHED":
                    yield {"type": "text", "content": "", "finish_reason": "stop", "message_id": message_id}
                continue
            if not isinstance(v, str) or not v:
                continue
            # skip non-content patches
            if p is not _SENTINEL and not str(p).startswith("response/content") and "thinking" not in str(p):
                continue
            # determine path
            path = p if p is not _SENTINEL else cur_path
            if "thinking" in str(path):
                cur_path = path
                yield {"type": "thinking", "content": v, "finish_reason": None, "message_id": None}
            else:
                if p is not _SENTINEL:
                    cur_path = path
                kind = "thinking" if "thinking" in cur_path else "text"
                yield {"type": kind, "content": v, "finish_reason": None, "message_id": None}

# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# LOGIN
# ─────────────────────────────────────────────────────────────────────────────
def _login_terminal(cfg: dict) -> tuple:
    print(f"\n{bold('DeepSeek Login')}")
    print()
    print("  1. Open  https://chat.deepseek.com  and log in")
    print()
    print(f"  {c(CYAN, 'F12 → Console')} on chat.deepseek.com — paste and press Enter:")
    js = "console.log(JSON.stringify({t:JSON.parse(localStorage.getItem('userToken')).value,c:document.cookie}))"
    print(f"    {c(YELLOW, js)}")
    print()
    print(f"  {c(CYAN, 'Android (address bar)')}:")
    js_bar = "javascript:prompt('token:',JSON.parse(localStorage.getItem('userToken')).value+'|||'+document.cookie)"
    print(f"    {c(YELLOW, js_bar)}")
    print()
    print("  2. Paste the result below.")
    print(f"     {dim('Accepts: plain token  OR  {\"t\":\"...\",\"c\":\"cookie=...\"}')}")
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


def do_login(cfg: dict):
    token, cookies, ua = _login_terminal(cfg)
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

    def print_history(self):
        msgs = _load_history(self.chat_id)
        if not msgs:
            return
        W = _cols()
        BG_U = "\033[48;5;238m"
        FG_U = "\033[97m"
        AI_COL = "\033[38;5;75m"
        IND = "  "
        for msg in msgs:
            role = msg.get("role", "")
            text = msg.get("text", "").rstrip()
            if not text:
                continue
            print()
            if role == "user":
                lines = text.splitlines() or [""]
                for line in lines:
                    ln  = IND + line
                    pad = max(0, W - len(ln))
                    print(f"{BG_U}{FG_U}{ln}{' ' * pad}{R}")
            else:
                print(f"{c(DBLUE, '●')} {AI_COL}{text.replace(chr(10), chr(10) + '  ')}{R}")
        print()

    def _stream(self, prompt: str) -> str:
        thinking   = self.cfg["model"] == "r1"
        search     = self.cfg["search"]
        buf        = []
        in_think   = False
        first_text = True
        cancelled  = threading.Event()
        done       = threading.Event()
        err        = [None]
        out_lock   = threading.Lock()
        stream_write_fn: list = [None]  # set by _show_stream_bar

        print()

        def _sw(text: str):
            fn = stream_write_fn[0]
            if fn is not None:
                fn(text)
            else:
                sys.stdout.write(text)
                sys.stdout.flush()

        def _run():
            nonlocal in_think, first_text
            try:
                for chunk in self.client.stream(
                    self.chat_id, prompt, self.parent_id, thinking, search
                ):
                    if cancelled.is_set():
                        break
                    kind, content = chunk["type"], chunk["content"]
                    with out_lock:
                        if kind == "thinking":
                            if not in_think:
                                _sw(f"{dim('╭─ thinking ─────────────────')}\n")
                                in_think = True
                            _sw(dim(content))
                        elif kind == "text":
                            if in_think:
                                _sw(f"\n{dim('╰────────────────────────────')}\n\n")
                                in_think = False
                            if first_text and content:
                                _sw(f"{c(DBLUE, '●')} ")
                                first_text = False
                            _sw(c("\033[38;5;75m", content.replace("\n", "\n  ")))
                            buf.append(content)
                    if kind == "title":
                        self.chat_title = content
                        _update_chat_title(self.chat_id, content)
                    if chunk.get("finish_reason") == "stop":
                        mid = chunk.get("message_id")
                        if mid: self.parent_id = mid
            except Exception as e:
                err[0] = e
            finally:
                done.set()

        t = threading.Thread(target=_run, daemon=True)
        t.start()

        was_cancelled = _show_stream_bar(done, out_lock, stream_write_fn)
        if was_cancelled:
            cancelled.set()
        done.wait()

        if err[0]:
            sys.stdout.write(c(RED, f"\nStream error: {err[0]}"))
        if was_cancelled:
            sys.stdout.write(f"\n{c(DIM, '[cancelled]')}")
        print("\n")
        result = "".join(buf)
        if result:
            _append_history(self.chat_id, "assistant", result)
        return result

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
        _append_history(self.chat_id, "user", user_msg)
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
_MNAMES  = {"chat": "DeepSeek V4", "r1": "DeepSeek R1"}
_MODELS  = [
    ("chat", "DeepSeek V4", "default"),
    ("r1",   "DeepSeek R1", "reasoning · thinking mode"),
]

def _pick_model(current: str) -> str | None:
    """Interactive arrow-key model picker. Returns chosen key or None if cancelled."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return None
    try:
        import termios, tty as _tty_mod
    except ImportError:
        return None

    fd       = sys.stdin.fileno()
    old_attr = termios.tcgetattr(fd)
    keys     = [m[0] for m in _MODELS]
    sel      = keys.index(current) if current in keys else 0
    N        = len(_MODELS)
    H        = N + 2   # top separator + N rows + bottom separator

    def _w(): return sys.stdout.write
    def _flush(): sys.stdout.flush()

    def _render():
        W = _cols()
        out = []
        out.append(f"\r\033[K{c(DBLUE, '── model ' + '─' * max(0, W - 10))}")
        for i, (key, name, desc) in enumerate(_MODELS):
            if i == sel:
                row = f" {c(BCYAN+BOLD,'❯')} {c(BCYAN+BOLD, name)}  {c(BCYAN, desc)}"
            else:
                row = f"   {c(DIM, name)}  {c(DIM, desc)}"
            row += " " * max(0, W - _vis_len(row) - 1)
            out.append(f"\r\033[K{row}")
        out.append(f"\r\033[K{c(DBLUE, '─' * max(0, W - 1))}")
        # join with \n, then move cursor back up to top of block
        _w()("\n".join(out))
        _w()(f"\033[{H - 1}A\r")
        _flush()

    def _erase():
        # erase all H lines and leave cursor on first line
        for i in range(H):
            _w()(f"\r\033[K")
            if i < H - 1:
                _w()("\n")
        _w()(f"\033[{H - 1}A\r\033[K")
        _flush()

    # reserve H lines, hide cursor
    _w()("\033[?25l")           # hide cursor
    _w()("\n" * H)              # push H blank lines
    _w()(f"\033[{H}A\r")       # go back to top of block
    _flush()
    _render()

    try:
        _tty_mod.setraw(fd)
        while True:
            ch = os.read(fd, 1)
            if ch == b'\x1b':
                try:    seq = os.read(fd, 2)
                except: seq = b''
                if seq == b'[A':   sel = (sel - 1) % N
                elif seq == b'[B': sel = (sel + 1) % N
                else:
                    _erase()
                    _w()("\033[?25h"); _flush()
                    return None
                _render()
                continue
            if ch in (b'\r', b'\n'):
                _erase()
                _w()("\033[?25h"); _flush()   # show cursor
                return keys[sel]
            if ch in (b'\x03', b'\x04', b'q'):
                _erase()
                _w()("\033[?25h"); _flush()
                if ch == b'\x03': raise KeyboardInterrupt
                return None
    finally:
        _w()("\033[?25h"); _flush()           # always restore cursor
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)


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

def _box(lines, title="", close=True):
    cols = _cols()
    W    = max(40, cols)
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
    if close:
        rows.append(c(BCYAN, "╰" + "─" * d + "╯"))
    return "\n".join(rows)

def _sep(label=""):
    cols = _cols()
    s    = f"── {label} " if label else "─" * cols
    if label:
        fill = "─" * max(0, cols - len(s) - 1)
        s = s + fill
    return c(DBLUE, s)

def _draw_input_field(text=""):
    """Draw the sticky 3-line input field at bottom of screen via absolute positioning."""
    rows = _rows(); cols = _cols()
    bar  = c(DBLUE, "─" * cols)
    pr   = c(BBLUE+BOLD, "❯") + " "
    vis  = _vis_len(pr) + _vis_len(text) + 1
    sys.stdout.write(
        "\0337"                                      # save cursor
        + f"\033[{rows-2};1H\033[K{bar}"            # top bar
        + f"\033[{rows-1};1H\033[K{pr}{text}"       # ❯ line
        + f"\033[{rows};1H\033[K{bar}"              # bottom bar
        + f"\033[{rows-1};{vis}H"                   # cursor after text
        + "\0338"                                    # restore cursor
    ); sys.stdout.flush()

def _set_scroll_region():
    rows = _rows()
    sys.stdout.write(f"\033[1;{rows-3}r"); sys.stdout.flush()

def _clear_scroll_region():
    sys.stdout.write("\033[r"); sys.stdout.flush()

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
    av1 = c(BCYAN+BOLD, "▐▛██▛▌")
    av2 = c(BCYAN+BOLD, "▐█▟██▌")
    return [
        "",
        f"  {av1}  {c(BCYAN+BOLD, 'Welcome back, ' + uname + '!')}",
        f"  {av2}  {c(DIM, 'Send /help for help · /exit to quit.')}",
        "",
        f"  {c(DIM, 'Directory:')}  {c(DIM, cwd)}",
        f"  {c(DIM, 'Model:')}      {c(DIM, mn)}",
        f"  {c(DIM, 'Chat:')}       {c(DIM, chat_title[:48] if chat_title else 'New chat')}",
        "",
    ]

def _show_welcome(cfg, chat_id, chat_title_or_fn, user_name=""):
    _cls()
    def _render():
        title = chat_title_or_fn() if callable(chat_title_or_fn) else chat_title_or_fn
        return ("\n" +
                _box(_welcome_lines(cfg, chat_id, title, user_name),
                     title=f"DeepSeek Code  v{VERSION}") +
                "\n")
    _live_state["fn"] = _render
    print(_render(), end="", flush=True)

def _manual_update():
    """Check for update on demand (/update command)."""
    frames = ['⠋','⠙','⠹','⠸','⠼','⠴','⠦','⠧','⠇','⠏']
    done   = [False]
    remote = [None]

    def _fetch():
        try:
            req = urllib.request.Request(f"{_RAW_BASE}/VERSION",
                                         headers={"User-Agent": "deepseek/1.0"})
            remote[0] = urllib.request.urlopen(req, timeout=6).read().decode().strip()
        except Exception as e:
            remote[0] = f"ERROR:{e}"
        done[0] = True

    t = threading.Thread(target=_fetch, daemon=True)
    t.start()
    i = 0
    while not done[0]:
        sys.stdout.write(f"\r  {c(BCYAN, frames[i % len(frames)])}  {c(DIM, 'checking...')}")
        sys.stdout.flush()
        time.sleep(0.08)
        i += 1
    sys.stdout.write("\r\033[K")
    sys.stdout.flush()

    rv = remote[0]
    if rv is None or (rv and rv.startswith("ERROR:")):
        err = rv[6:] if rv else "timeout"
        print(f"  {c(RED, '✗')}  {c(DIM, str(err))}")
        return

    if rv == VERSION:
        print(f"  {c(GREEN+BOLD, '✓')}  {c(DIM, 'already up to date')}  {c(DIM, VERSION)}")
        return

    # newer version available — download and offer restart
    try:
        sys.stdout.write(f"  {c(BCYAN, '↓')}  {c(DIM, 'downloading ' + rv + '...')}")
        sys.stdout.flush()
        req2    = urllib.request.Request(f"{_RAW_BASE}/deepseek.py",
                                          headers={"User-Agent": "deepseek/1.0"})
        new_src = urllib.request.urlopen(req2, timeout=15).read()
        Path(__file__).resolve().write_bytes(new_src)
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()
    except Exception as e:
        sys.stdout.write("\r\033[K")
        print(f"  {c(RED, '✗')}  download failed: {c(DIM, str(e))}")
        return

    _show_update_page(rv)


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

# ── command autocomplete definitions ─────────────────────────────────────────
_CMDS = [
    ("/model",         "choose model"),
    ("/new",          "start a new chat"),
    ("/chats",        "browse and switch chats"),
    ("/delete",       "delete current chat"),
    ("/cwd",          "change working directory"),
    ("/login",        "re-authenticate"),
    ("/search",       "toggle web search on/off"),
    ("/confirm",      "toggle shell confirmation"),
    ("/status",       "show current settings"),
    ("/update",        "check for updates"),
    ("/help",         "show help page"),
    ("/exit",         "quit"),
]

MENU_H = len(_CMDS)   # reserve enough lines for all commands

def _cmd_matches(text: str):
    if not text.startswith("/"):
        return []
    # once user types a space (adding arguments) — hide menu
    if " " in text:
        return []
    q = text[1:].lower()
    if not q:
        return list(_CMDS)
    starts  = [(cmd, desc) for cmd, desc in _CMDS if cmd[1:].lower().startswith(q)]
    in_desc = [(cmd, desc) for cmd, desc in _CMDS
               if (cmd, desc) not in starts and q in desc.lower()]
    return starts + in_desc

def _hl(text: str, query: str, base: str = "") -> str:
    """Highlight first occurrence of query inside text. base is the color for the rest."""
    def _b(s): return c(base, s) if base and s else s
    if not query:
        return _b(text)
    lo, q = text.lower(), query.lower()
    idx = lo.find(q)
    if idx == -1:
        return _b(text)
    HL = "\033[38;5;75m"    # muted blue — subtle highlight
    return (_b(text[:idx]) +
            c(HL, text[idx:idx+len(query)]) +
            _b(text[idx+len(query):]))

_input_history: list[str] = []   # submitted prompts, newest last

def _show_stream_bar(done: threading.Event,
                     out_lock: "threading.Lock | None" = None,
                     stream_write_fn: "list | None" = None) -> bool:
    """Draw fixed 4-line input panel; AI text scrolls above. Returns True if cancelled."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        done.wait()
        return False
    try:
        import termios, tty as _tty_mod, select
    except ImportError:
        done.wait()
        return False

    fd       = sys.stdin.fileno()
    old_attr = termios.tcgetattr(fd)
    rows     = _rows()
    cols     = _cols()

    def _bar():    return c(DBLUE, "─" * cols)
    def _flush(s): sys.stdout.write(s); sys.stdout.flush()

    PR         = c(BBLUE+BOLD, "❯") + " "
    hint       = f"  {c(DIM, 'esc · cancel')}"
    cursor_row = rows - 2   # row where ❯ lives (1-based)

    # Query real cursor position before drawing panel (raw mode temporarily).
    def _query_cursor_pos() -> tuple:
        try:
            import re as _re
            _tty_mod.setraw(fd)
            sys.stdout.write("\033[6n"); sys.stdout.flush()
            rdy, _, _ = select.select([fd], [], [], 0.2)
            resp = b""
            if rdy:
                os.set_blocking(fd, False)
                try:
                    while True:
                        ch = os.read(fd, 1); resp += ch
                        if ch == b'R': break
                except BlockingIOError:
                    pass
                os.set_blocking(fd, True)
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)
            m = _re.search(rb'\[(\d+);(\d+)R', resp)
            if m:
                return int(m.group(1)), int(m.group(2))
        except Exception:
            pass
        return (rows - 4, 1)

    ai_pos = list(_query_cursor_pos())   # where AI text starts

    # Reserve bottom 4 rows for panel; AI text scrolls in rows 1..(rows-4).
    _flush(
        f"\033[1;{rows-4}r"                    # scroll region: rows 1 to rows-4
        f"\033[{rows-3};1H\033[K{_bar()}"      # rows-3: top bar
        f"\033[{rows-2};1H\033[K{PR}"          # rows-2: ❯ row
        f"\033[{rows-1};1H\033[K{_bar()}"      # rows-1: bottom bar
        f"\033[{rows};1H\033[K{hint}"          # rows:   hint
        f"\033[{cursor_row};3H"                # cursor to ❯ row col 3
    )

    # Set up panel-aware writer for the AI stream thread.
    # Writes text at ai_pos, updates ai_pos, then restores cursor to ❯ row.
    if stream_write_fn is not None:
        def _advance(pos: list, text: str):
            i = 0; r, col = pos; max_r = rows - 4
            while i < len(text):
                ch = text[i]
                if ch == '\033':
                    i += 1
                    if i < len(text) and text[i] == '[':
                        i += 1
                        while i < len(text) and text[i] not in 'ABCDEFGHJKSTmsu':
                            i += 1
                    i += 1
                elif ch == '\n':
                    r = min(r + 1, max_r); col = 1; i += 1
                elif ch == '\r':
                    col = 1; i += 1
                else:
                    col += 1
                    if col > cols:
                        r = min(r + 1, max_r); col = 1
                    i += 1
            pos[0] = r; pos[1] = col

        def _panel_write(text: str):
            r, col = ai_pos
            _flush(f"\033[{r};{col}H" + text + f"\033[{cursor_row};3H")
            _advance(ai_pos, text)

        stream_write_fn[0] = _panel_write

    cancelled = False
    try:
        _tty_mod.setraw(fd)
        while not done.is_set():
            r, _, _ = select.select([fd], [], [], 0.05)
            if not r:
                continue
            ch = os.read(fd, 1)
            if ch == b'\x1b':
                try:
                    os.set_blocking(fd, False)
                    seq = os.read(fd, 2)
                    os.set_blocking(fd, True)
                except:
                    seq = b''
                if not seq or seq not in (b'[A', b'[B', b'[C', b'[D'):
                    cancelled = True
                    break
            elif ch == b'\x03':
                cancelled = True
                break
    finally:
        if stream_write_fn is not None:
            stream_write_fn[0] = None
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)
        _flush(
            f"\033[1;{rows}r"           # restore full scroll region
            f"\033[{rows-3};1H\033[K"  # clear top bar
            f"\033[{rows-2};1H\033[K"  # clear ❯ row
            f"\033[{rows-1};1H\033[K"  # clear bottom bar
            f"\033[{rows};1H\033[K"    # clear hint
            "\033[?7h"
        )

    return cancelled


def _prompt_with_autocomplete(_unused: str = "") -> str:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return input()
    try:
        import termios, tty as _tty_mod
    except ImportError:
        return input()

    fd       = sys.stdin.fileno()
    old_attr = termios.tcgetattr(fd)
    buf      = []
    sel      = 0
    SEL_COL  = "\033[38;5;75m"
    PR       = c(BBLUE+BOLD, "❯") + " "

    hist_idx  = [len(_input_history)]  # points past end = not browsing
    saved_buf = [""]                   # draft saved when entering history

    def _bar(): return c(DBLUE, "─" * _cols())
    def _flush(s): sys.stdout.write(s); sys.stdout.flush()

    # Draw 3-line field: top bar / ❯ row / bottom bar
    # Disable terminal auto-wrap so we control line breaks manually
    # Cursor ends on ❯ row (one \033[1A from bottom bar)
    sys.stdout.write(f"\033[?7l{_bar()}\r\n{PR}\r\n{_bar()}\033[1A\r{PR}")
    sys.stdout.flush()

    # All positioning is relative — no ESC[6n needed.
    # Cursor is always on the ❯ row after every operation.

    prev_rows = [1]  # rows occupied by prompt last draw
    PAD_R = 2        # right padding on every row
    IND   = "  "     # left indent for continuation rows (replaces ❯ )

    def _wrap_rows(text: str, cols: int) -> list:
        """Split text into display rows respecting left indent and right pad."""
        usable1 = cols - 2 - PAD_R       # first row: after ❯ , before right pad
        usableN = cols - len(IND) - PAD_R # continuation rows
        rows = []
        if not text:
            return [""]
        rows.append(text[:usable1])
        pos = usable1
        while pos < len(text):
            rows.append(text[pos:pos + usableN])
            pos += usableN
        return rows

    def _lc(text: str) -> int:
        return len(_wrap_rows(text, _cols()))

    def _redraw(text: str):
        cols   = _cols()
        wrows  = _wrap_rows(text, cols)
        new_lc = len(wrows)
        old_lc = prev_rows[0]
        # total rows to clear: old text rows + old bottom bar
        total_old = old_lc + 1
        out = []
        # go up to first text row
        if old_lc > 1:
            out.append(f"\033[{old_lc - 1}A")
        # clear all old rows including old bottom bar
        out.append("\r\033[K")
        for _ in range(total_old - 1):
            out.append("\033[1B\r\033[K")
        # back to first text row
        out.append(f"\033[{total_old - 1}A")
        # print text rows
        out.append(f"\r{PR}{wrows[0]}")
        for row in wrows[1:]:
            out.append(f"\r\n{IND}{row}")
        # draw bottom bar right below last text row
        out.append(f"\033[1B\r\033[K{_bar()}")
        # return cursor to last text row, position after typed text
        last_row = wrows[-1]
        if new_lc == 1:
            col = 2 + len(last_row)   # ❯ + space + text
        else:
            col = len(IND) + len(last_row)
        out.append(f"\033[1A\r\033[{col}C")
        prev_rows[0] = new_lc
        _flush("".join(out))

    def _pr_len(): return 2 + len("".join(buf))  # ❯ + space + text

    def _redraw_menu(text: str):
        cols = _cols()
        q    = text[1:] if text.startswith("/") else ""
        hits = _cmd_matches(text)
        sel2 = max(0, min(sel, len(hits)-1)) if hits else 0
        out  = [f"\r\033[K{PR}{text}"]       # redraw ❯ row
        out.append("\033[1B\r\033[K")          # bottom bar row
        out.append(_bar())
        for i in range(MENU_H):
            out.append("\033[1B\r\033[K")
            if i < len(hits):
                cmd, desc = hits[i]
                if i == sel2:
                    row = f" {c(SEL_COL,'❯')} {_hl(cmd,'/' + q if q else '',SEL_COL)}  {_hl(desc,q,SEL_COL)}"
                else:
                    row = f"   {_hl(cmd,'/' + q if q else '',DIM)}  {_hl(desc,q,DIM)}"
                row += " " * max(0, cols - _vis_len(row) - 1)
                out.append(row)
        # return to ❯ row, position cursor after typed text
        out.append(f"\033[{MENU_H + 1}A\r\033[{_pr_len()}C")
        _flush("".join(out))

    def _clear_menu(text: str):
        out = [f"\r\033[K{PR}{text}"]
        out.append("\033[1B\r\033[K")
        out.append(_bar())
        for _ in range(MENU_H):
            out.append("\033[1B\r\033[K")
        out.append(f"\033[{MENU_H + 1}A\r\033[{_pr_len()}C")
        _flush("".join(out))

    menu_open  = False
    ctrlc_once = [False]
    _ctrlc_timer = [None]

    def _show_ctrlc_hint():
        _flush(f"\033[1B\r\033[K  {c(DIM, 'press ctrl+c again to exit')}\033[1A\r\033[{2 + len(''.join(buf))}C")

    def _clear_ctrlc_hint():
        _flush(f"\033[1B\r\033[K{_bar()}\033[1A\r\033[{2 + len(''.join(buf))}C")

    def _arm_ctrlc_clear():
        if _ctrlc_timer[0]:
            _ctrlc_timer[0].cancel()
        def _expire():
            ctrlc_once[0] = False
            _clear_ctrlc_hint()
        t = threading.Timer(3.0, _expire)
        t.daemon = True
        t.start()
        _ctrlc_timer[0] = t

    def _done(text: str):
        BG   = "\033[48;5;238m"
        FG   = "\033[97m"
        cols = _cols()
        rows = prev_rows[0]
        out  = []
        # go up to top bar
        out.append(f"\033[{rows}A\r\033[K")
        out.append("\033[1B\r\033[K")
        if text:
            wrows = _wrap_rows(text, cols)
            for i, row in enumerate(wrows):
                if i > 0:
                    out.append("\033[1B\r\033[K")
                prefix = IND if i > 0 else IND  # both use IND (❯ hidden)
                ln     = prefix + row
                pad    = max(0, cols - len(ln))
                out.append(f"{BG}{FG}{ln}{' ' * pad}{R}")
        # clear bottom bar
        out.append("\033[1B\r\033[K")
        out.append("\033[1A\r\n")
        _flush("".join(out))
        _flush("\033[?7h\033[?25h")

    try:
        _tty_mod.setraw(fd)
        while True:
            ch = os.read(fd, 1)

            if ch != b'\x03' and ctrlc_once[0]:
                ctrlc_once[0] = False
                if _ctrlc_timer[0]: _ctrlc_timer[0].cancel()
                _clear_ctrlc_hint()

            if ch == b'\x1b':
                try:    seq = os.read(fd, 2)
                except: seq = b''
                if seq == b'[A':
                    hits = _cmd_matches("".join(buf))
                    if hits:
                        sel = max(0, sel - 1); menu_open = True
                        _redraw_menu("".join(buf))
                    elif _input_history:
                        if hist_idx[0] == len(_input_history):
                            saved_buf[0] = "".join(buf)
                        hist_idx[0] = max(0, hist_idx[0] - 1)
                        buf[:] = list(_input_history[hist_idx[0]]); sel = 0
                        if menu_open: _clear_menu("".join(buf)); menu_open = False
                        _redraw("".join(buf))
                elif seq == b'[B':
                    hits = _cmd_matches("".join(buf))
                    if hits:
                        sel = min(len(hits)-1, sel+1); menu_open = True
                        _redraw_menu("".join(buf))
                    elif hist_idx[0] < len(_input_history):
                        hist_idx[0] += 1
                        if hist_idx[0] == len(_input_history):
                            buf[:] = list(saved_buf[0])
                        else:
                            buf[:] = list(_input_history[hist_idx[0]])
                        sel = 0
                        if menu_open: _clear_menu("".join(buf)); menu_open = False
                        _redraw("".join(buf))
                else:
                    if menu_open:
                        _clear_menu("".join(buf)); menu_open = False
                    else:
                        buf = []; sel = 0
                        _redraw("".join(buf))
                continue

            if ch in (b'\r', b'\n'):
                text = "".join(buf)
                hits = _cmd_matches(text)
                if menu_open and hits:
                    text = hits[min(sel, len(hits)-1)][0]
                if menu_open:
                    _clear_menu(text); menu_open = False
                if text and (not _input_history or _input_history[-1] != text):
                    _input_history.append(text)
                    if len(_input_history) > 200:
                        _input_history.pop(0)
                hist_idx[0] = len(_input_history)
                _done(text)
                return text

            if ch == b'\x03':
                if menu_open: _clear_menu(""); menu_open = False
                if ctrlc_once[0]:
                    if _ctrlc_timer[0]: _ctrlc_timer[0].cancel()
                    _done(""); raise KeyboardInterrupt
                ctrlc_once[0] = True
                _show_ctrlc_hint()
                _arm_ctrlc_clear()
                continue
            if ch == b'\x04':
                if menu_open: _clear_menu(""); menu_open = False
                _done(""); raise EOFError

            if ch == b'\t':
                hits = _cmd_matches("".join(buf))
                if hits:
                    buf = list(hits[sel % len(hits)][0]); sel = 0
                    menu_open = True; _redraw_menu("".join(buf))
                continue

            if ch in (b'\x7f', b'\x08'):
                if buf:
                    buf.pop(); sel = 0
                    text = "".join(buf)
                    if text.startswith("/"):
                        menu_open = True; _redraw_menu(text)
                    else:
                        if menu_open:
                            _clear_menu(text); menu_open = False
                        _redraw(text)
                continue

            b0 = ch[0]
            if b0 >= 0xF0:   extra = 3
            elif b0 >= 0xE0: extra = 2
            elif b0 >= 0xC0: extra = 1
            else:            extra = 0
            for _ in range(extra):
                try:    ch += os.read(fd, 1)
                except: break
            try:    char = ch.decode('utf-8')
            except: continue
            if char < ' ': continue
            buf.append(char); sel = 0
            text = "".join(buf)
            if text.startswith("/"):
                menu_open = True; _redraw_menu(text)
            else:
                if menu_open:
                    _clear_menu(text); menu_open = False
                _redraw(text)

    finally:
        if _ctrlc_timer[0]: _ctrlc_timer[0].cancel()
        sys.stdout.write("\033[?7h\033[?25h"); sys.stdout.flush()  # restore autowrap
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)


def _help_box():
    def row(cmd, desc, w=22):
        pad = " " * max(0, w - _vis_len(cmd))
        return f"  {c(BCYAN, cmd)}{pad}{c(DIM, desc)}"
    def sec(name):
        return f"\n  {c(BBLUE+BOLD, name)}"
    return _box([
        sec("model"),
        row("/model",        "choose model interactively"),
        sec("chat"),
        row("/new",          "start a new chat"),
        row("/chats",        "browse and switch chats"),
        row("/delete",       "delete current chat"),
        row("/cwd [path]",   "change working directory"),
        row("/login",        "re-authenticate"),
        sec("settings"),
        row("/search",       "toggle web search (on by default)"),
        row("/confirm",      "toggle shell confirmation"),
        row("/status",       "show current settings"),
        sec("other"),
        row("/update",       "check for updates"),
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
        _kv("confirm",   on if cfg["confirm_bash"] else off),
        _kv("directory", c(DIM, cwd)),
        _kv("version",   c(DIM, VERSION)),
    ]
    if chat_title:
        lines.append(_kv("chat", c(DIM, chat_title[:48])))
    lines.append("")
    return _box(lines, title="DeepSeek Code  /status")

def _pick_chat(agent) -> dict | None:
    """Interactive arrow-key chat picker. Returns chosen entry dict or None."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return None
    try:
        import termios, tty as _tty_mod
    except ImportError:
        return None

    # fetch list from local storage
    local_chats = _load_local_chats()
    entries = [{"id": ch.get("id",""), "title": ch.get("title","Untitled")[:50],
                "model": ch.get("model","")} for ch in local_chats[:30]]

    if not entries:
        print(f"  {c(DIM, 'No chats yet.')}")
        return None

    fd       = sys.stdin.fileno()
    old_attr = termios.tcgetattr(fd)
    sel      = next((i for i, e in enumerate(entries) if e["id"] == agent.chat_id), 0)

    def _w(): return sys.stdout.write
    def _flush(): sys.stdout.flush()

    def _H(): return len(entries) + 3   # top sep + hint + rows + bottom sep

    def _render(confirm_del=False):
        H = _H()
        W = _cols()
        out = []
        # full-width top separator
        label = " chats "
        out.append(f"\r\033[K{c(DBLUE, '──' + label + '─' * max(0, W - len(label) - 3))}")
        # hint line
        if confirm_del:
            hint = f" {c(RED, '✗')}  {c(DIM, 'delete? [y/n]')}"
        else:
            hint = f"   {c(DIM, 'n · new chat   d · delete selected')}"
        out.append(f"\r\033[K{hint}")
        for i, e in enumerate(entries):
            is_cur = e["id"] == agent.chat_id
            title  = e["title"][:W - 8]
            if i == sel:
                row = f" {c('\033[38;5;75m', '❯')} {c(RED if confirm_del else '\033[38;5;75m', ('◆ ' if is_cur else '') + title)}"
            else:
                row = f"   {c(DIM, ('◆ ' if is_cur else '') + title)}"
            row += " " * max(0, W - _vis_len(row) - 1)
            out.append(f"\r\033[K{row}")
        out.append(f"\r\033[K{c(DBLUE, '─' * max(0, W - 1))}")
        _w()("\n".join(out))
        _w()(f"\033[{H - 1}A\r")
        _flush()

    def _erase():
        H = _H()
        for i in range(H):
            _w()(f"\r\033[K")
            if i < H - 1: _w()("\n")
        _w()(f"\033[{H - 1}A\r\033[K")
        _flush()

    def _reserve():
        H = _H()
        _w()("\033[?25l")
        _w()("\n" * H)
        _w()(f"\033[{H}A\r")
        _flush()

    _reserve()
    _render()

    try:
        _tty_mod.setraw(fd)
        confirm_del = False
        while True:
            ch = os.read(fd, 1)
            if ch == b'\x1b':
                try:    seq = os.read(fd, 2)
                except: seq = b''
                confirm_del = False
                if seq == b'[A':   sel = max(0, sel - 1)
                elif seq == b'[B': sel = min(len(entries) - 1, sel + 1)
                else:
                    _erase()
                    _w()("\033[?25h"); _flush()
                    return None
                _render()
                continue

            if confirm_del:
                if ch in (b'y', b'Y'):
                    target = entries[sel]
                    # remove from list first, adjust sel
                    entries.pop(sel)
                    sel = max(0, min(sel, len(entries) - 1))
                    confirm_del = False
                    # do the actual delete (after exiting raw so API can run)
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)
                    agent.client.delete_chat(target["id"])
                    _remove_local_chat(target["id"])
                    if target["id"] == agent.chat_id:
                        agent._new_session()
                    # re-enter raw
                    _tty_mod.setraw(fd)
                    if not entries:
                        _erase()
                        _w()("\033[?25h"); _flush()
                        return None
                    _erase()
                    _reserve()
                    _render()
                else:
                    confirm_del = False
                    _render()
                continue

            if ch == b'n':
                _erase()
                _w()("\033[?25h"); _flush()
                return {"new": True}

            if ch == b'd':
                confirm_del = True
                _render(confirm_del=True)
                continue

            if ch in (b'\r', b'\n'):
                _erase()
                _w()("\033[?25h"); _flush()
                return entries[sel] if entries else None
            if ch in (b'\x03', b'\x04', b'q'):
                _erase()
                _w()("\033[?25h"); _flush()
                if ch == b'\x03': raise KeyboardInterrupt
                return None
    finally:
        _w()("\033[?25h"); _flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)

def _delete_current_chat(agent, cfg):
    title = agent.chat_title or "Untitled"
    try:
        ans = input(f"  Delete {c(YELLOW, repr(title[:40]))}? {c(DIM,'[y/N]')} ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        return
    if ans != "y":
        return
    ok = agent.client.delete_chat(agent.chat_id)
    _remove_local_chat(agent.chat_id)
    if ok:
        print(f"  {c(GREEN,'✓')}  deleted")
    else:
        print(f"  {c(YELLOW,'⚠')}  removed locally (API delete may have failed)")
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

    # SIGWINCH: redraw welcome box on resize
    def _sigwinch_handler(sig, frame):
        if not sys.stdout.isatty():
            return
        try:
            fn = _live_state.get("fn")
            if fn:
                cols = _cols()
                content = fn()
                lines = content.splitlines()
                out = [b"\0337\033[?25l"]
                for i, ln in enumerate(lines):
                    out.append(f"\033[{i+1};1H\033[K{ln}".encode())
                out.append(b"\033[?25h\0338")
                os.write(1, b"".join(out))
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

        def _wtitle(): return agent.chat_title
        def _wuser():  return getattr(agent, "user_name", "")

        def _welcome():
            _show_welcome(cfg, agent.chat_id, _wtitle, _wuser())

        _welcome()

        def toggle(key, arg):
            cfg[key] = arg=="on" if arg in ("on","off") else not cfg[key]
            save_cfg(cfg); return "on" if cfg[key] else "off"

        def _bar():
            return c(DBLUE, "─" * _cols())

        while True:
            try:
                line = _prompt_with_autocomplete().strip()
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

                elif cmd == "update":
                    print()
                    _manual_update()
                    print()

                elif cmd == "help":
                    _cls(); print(); print(_help_box()); print()
                    continue

                elif cmd == "status":
                    _cls(); print(); print(_status_box(cfg, agent.chat_id, agent.chat_title)); print()
                    continue

                elif cmd in ("new", "clear"):
                    agent._new_session()
                    _welcome()
                    continue

                elif cmd == "chats":
                    chosen = _pick_chat(agent)
                    if chosen and chosen.get("new"):
                        agent._new_session()
                        _welcome()
                    elif chosen:
                        agent._load_chat(chosen["id"], chosen["title"])
                        _cls()
                        _show_welcome(cfg, agent.chat_id, _wtitle, _wuser())
                        agent.print_history()
                    else:
                        print()
                    continue

                elif cmd == "delete":
                    _delete_current_chat(agent, cfg)

                elif cmd == "login":
                    do_login(cfg)
                    agent.client._cookies, agent.client._ua = load_cookies()
                    print(f"  {c(GREEN,'✓')}  {c(DIM, 'logged in')}")

                elif cmd == "model":
                    if arg in ("chat", "r1"):
                        cfg["model"] = arg; save_cfg(cfg)
                        mn = next(n for k,n,_ in _MODELS if k==arg)
                        print(f"  {c(GREEN+BOLD,'✓')}  {c(BCYAN+BOLD, mn)}")
                    else:
                        chosen = _pick_model(cfg["model"])
                        if chosen:
                            cfg["model"] = chosen; save_cfg(cfg)
                            mn = next(n for k,n,_ in _MODELS if k==chosen)
                            print(f"  {c(GREEN+BOLD,'✓')}  {c(BCYAN+BOLD, mn)}")
                        else:
                            print(f"  {c(DIM, _MNAMES.get(cfg['model'], cfg['model']))}")

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
