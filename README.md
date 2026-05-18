<div align="center">

# DeepSeek Code

**AI coding agent powered by your DeepSeek account — no API key needed**

[![Python](https://img.shields.io/badge/python-3.9%2B-blue?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20Termux-lightgrey?style=flat-square)](https://github.com/tr1xx-tech/deepseek-code)
[![License](https://img.shields.io/badge/license-Apache%202.0-green?style=flat-square)](LICENSE)
[![DeepSeek](https://img.shields.io/badge/powered%20by-DeepSeek-4B6EF5?style=flat-square)](https://chat.deepseek.com)

A self-contained terminal agent similar to Claude Code.  
Uses your existing **chat.deepseek.com** session — browser login, zero config, zero cost.

</div>

---

## Quick Start

```bash
curl -fsSL https://raw.githubusercontent.com/tr1xx-tech/deepseek-code/main/install.sh | bash
```

That's it. The launcher installs all dependencies, opens a browser for login, and drops you into the agent.  
After first run, use `deepseek` from anywhere.

---

## What it does

DeepSeek Code is a ReAct-style agent that loops through reasoning and tool calls until a task is complete. You talk to it like Claude Code or GitHub Copilot Workspace — it reads your files, writes code, runs commands, searches the web, and fixes its own mistakes.

```
❯ build a fastapi server with /hello and /time endpoints, add tests, run them

◆ DeepSeek  I'll create the FastAPI server, then write and run the tests.

  ▶ write_file  → server.py
  ✓
  ▶ write_file  → test_server.py
  ✓
  ▶ bash  $ pip install fastapi httpx pytest pytest-asyncio -q
  ✓ exit 0
  ▶ bash  $ pytest test_server.py -v
  │ test_server.py::test_hello PASSED
  │ test_server.py::test_time PASSED
  ✓ exit 0

All done — server.py and test_server.py created, both tests pass.
```

---

## Features

- **No API key** — authenticates through your browser session at chat.deepseek.com
- **DeepSeek V4, V4 Pro and R1** — switch models mid-session with `/model v4pro` or `/model r1`
- **8 built-in tools** — shell, file read/write/edit, directory tree, web search, web fetch, Python execution
- **Streaming output** — see responses token by token as they arrive
- **R1 thinking mode** — watch the model reason step-by-step before answering
- **Built-in web search** — toggle DeepSeek's native search with `/search on`
- **Works on Termux** — runs natively on Android phones via Termux (ARM64)
- **Self-installing** — `start` puts `deepseek` in your PATH automatically

---

## Requirements

| | Desktop (Linux / macOS) | Termux (Android) |
|---|---|---|
| Python | 3.9+ | via `pkg install python` |
| pip | auto-installed if missing | auto-installed |
| curl_cffi | auto-installed | auto-installed |
| wasmtime | auto-installed | auto-installed |
| numpy | auto-installed | via `pkg install python-numpy` |
| Chrome | optional (for auto-login) | not needed |

Everything is handled by the `start` script. You don't need to install anything manually.

---

## Installation

### Desktop (Linux / macOS)

```bash
curl -fsSL https://raw.githubusercontent.com/tr1xx-tech/deepseek-code/main/install.sh | bash
```

After install:
```bash
deepseek           # start the agent
deepseek --login   # re-authenticate
```

### Termux (Android)

```bash
# In Termux terminal:
curl -fsSL https://raw.githubusercontent.com/tr1xx-tech/deepseek-code/main/install.sh | bash
```

After install:
```bash
deepseek
```

> Termux puts `deepseek` in `$PREFIX/bin` which is already in PATH — no extra steps needed.

---

## Login

### Desktop
A browser window opens automatically. Log in to your DeepSeek account and the agent captures your session. Done.

> Requires `nodriver` (installed automatically). Uses your existing Chrome browser.

### Termux / no browser
The agent shows a terminal login flow:

```
DeepSeek Login (terminal mode)

1. Open  https://chat.deepseek.com  in your browser and log in

Android Chrome / Kiwi Browser — type in address bar:
  javascript:alert(JSON.parse(localStorage.getItem("userToken")).value+'|||'+document.cookie)

Firefox / DevTools Console:
  copy(JSON.stringify({t:JSON.parse(localStorage.getItem("userToken")).value,c:document.cookie}))

Paste here: _
```

Paste the output and you're connected. The session is saved to `~/.deepseek/` and reused on future runs.

**Re-authenticate at any time:**
```bash
deepseek --login
# or inside the agent:
/login
```

---

## Usage

```bash
deepseek                    # start
deepseek --login            # re-login (refresh session)
```

Once inside, just type your task in plain English:

```
❯ explain what this repo does
❯ refactor utils.py to use dataclasses
❯ find all TODO comments in the project
❯ write a dockerfile for this app and test it
❯ search the web for the latest stable version of fastapi and update requirements.txt
```

---

## Commands

| Command | Description |
|---|---|
| `/model v4` | Switch to DeepSeek-V4 (fast, default) |
| `/model v4pro` | Switch to DeepSeek-V4 Pro (smarter) |
| `/model r1` | Switch to DeepSeek-R1 (deep reasoning) |
| `/thinking on` | Show R1's chain-of-thought output |
| `/search on` | Enable DeepSeek built-in web search |
| `/confirm off` | Skip confirmation for shell commands |
| `/cwd ./myproject` | Change working directory |
| `/clear` | Start a new conversation session |
| `/login` | Re-authenticate |
| `/help` | Show all commands |
| `/exit` | Quit |

---

## Agent Tools

The agent has access to these tools and picks the right one automatically:

| Tool | What it does |
|---|---|
| `bash` | Run any shell command (with confirmation for dangerous ones) |
| `read_file` | Read file contents with line numbers, supports offset/limit |
| `write_file` | Create or overwrite files, creates parent directories |
| `edit_file` | Exact find-and-replace inside a file |
| `list_dir` | Pretty directory tree up to depth 4 |
| `web_search` | DuckDuckGo search — no API key needed |
| `web_fetch` | Download any URL and read it as plain text |
| `python` | Execute Python code in a subprocess, capture output |

---

## Models

**V4** (`/model v4`) — default. Fast, handles most coding tasks well.

**V4 Pro** (`/model v4pro`) — smarter variant of V4. Better reasoning and code quality, slightly slower.

**R1** (`/model r1`) — full chain-of-thought reasoning. Best for complex multi-step tasks, architecture decisions, hard debugging. Slowest but most thorough.

Enable thinking output to see R1 reason step-by-step:
```
/model r1
/thinking on
```

---

## Files

```
deepseek-code/
├── deepseek.py    # agent: DeepSeek API client, POW solver, tools, ReAct loop
├── install.sh    # one-line installer
└── VERSION       # current version
```

Installed to:
```
~/.local/share/deepseek-code/<version>/   # agent files
~/.local/bin/deepseek                       # launcher
~/.deepseek/                                # session data (config, cookies, wasm)
```

---

## How it works

1. **Auth** — your DeepSeek browser session token is captured once and stored locally
2. **POW** — each request solves a proof-of-work challenge using a WASM binary (same mechanism the DeepSeek web app uses)
3. **TLS fingerprinting** — `curl_cffi` impersonates Chrome's TLS handshake so requests look like a real browser
4. **ReAct loop** — the agent streams a response, parses any `<tool_call>` blocks, executes them, feeds results back, and continues until no more tools are needed (up to 25 iterations per turn)

---

## Troubleshooting

**`Cloudflare blocked` error**
```bash
deepseek --login   # refresh your session
```

**`curl_cffi` install fails on an old system**
```bash
pip install requests   # requests works as a fallback (no TLS fingerprinting)
```

**`wasmtime` install fails**

wasmtime is required for the POW solver. Check that your Python is 3.9+ and pip is up to date:
```bash
pip install --upgrade pip
pip install wasmtime
```

**Termux: `pkg` command not found**

You're not in Termux — use the desktop install path instead.

**Token expired / auth error**
```bash
deepseek --login
```

---

## License

Apache 2.0 — see [LICENSE](LICENSE) for details.

---

<div align="center">

Made by [tr1xx-tech](https://github.com/tr1xx-tech)

</div>
