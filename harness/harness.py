#!/usr/bin/env python3
"""Ultimate AI - a full-permission local agentic REPL harness for an Ollama model.

Single-file harness: streams chat from a local Ollama server, executes native
tool calls (or the JSON fallback protocol for weak-tool models), and drives the
agent loop until the model calls task_done. Works on ANY Windows drive/path,
auto-approves every tool call, and never asks for confirmation.

Usage:
    python harness.py --model qwen3:8b
    python harness.py --model qwen3:8b --host http://127.0.0.1:11434 --max-iters 80
    python harness.py --self-test
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime
from pathlib import Path

# --- Core dependencies (critical path) -------------------------------------
# Heavy modules are lazy-loaded (sub-second startup matters): the REPL banner
# must not wait on httpx/psutil/pyperclip, which are only needed on first tool use.
httpx = None
psutil = None
pyperclip = None


def _httpx():
    global httpx
    if httpx is None:
        import httpx as _mod
        httpx = _mod
    return httpx


def _psutil():
    global psutil
    if psutil is None:
        import psutil as _mod
        psutil = _mod
    return psutil


def _pyperclip():
    global pyperclip
    if pyperclip is None:
        import pyperclip as _mod
        pyperclip = _mod
    return pyperclip


from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# --- Optional dependencies (guarded; degrade with a one-time notice) --------
try:
    from ddgs import DDGS
except Exception:  # pragma: no cover - optional
    try:
        from duckduckgo_search import DDGS  # type: ignore
    except Exception:
        DDGS = None

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover - optional
    BeautifulSoup = None

try:
    import playwright  # noqa: F401  # optional; reserved for browser automation
except Exception:  # pragma: no cover - optional
    playwright = None

MISSING_OPTIONAL = []
if DDGS is None:
    MISSING_OPTIONAL.append(("ddgs", "web_search will use the DuckDuckGo HTML fallback"))
if BeautifulSoup is None:
    MISSING_OPTIONAL.append(("beautifulsoup4", "HTML parsing falls back to regex"))
if playwright is None:
    MISSING_OPTIONAL.append(("playwright", "advanced browser automation unavailable"))

# --- Constants / state -------------------------------------------------------

UAI_DIR = Path(os.path.expanduser("~")) / ".ultimate-ai"
NOTES_FILE = UAI_DIR / "notes.md"
SHOTS_DIR = UAI_DIR / "shots"
SESSIONS_DIR = UAI_DIR / "sessions"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

# Verified machine facts passed by Start-UltimateAI.ps1 (survives fast launches).
# The system prompt injects these as GROUND TRUTH so the model can never misreport
# the hardware (WMI AdapterRAM is 32-bit and wrong for GPUs bigger than 4 GB).
HW_GROUND_TRUTH = os.environ.get("ULTIMATEAI_HW", "").strip()


def _browser_headers():
    return {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

TOOL_ARG_TRUNC = 400
TOOL_RESULT_TRUNC = 1200

STATE = {
    "cwd": os.getcwd(),
    "console": None,
    "model": None,
    "host": "http://127.0.0.1:11434",
    "num_ctx": None,
    "messages": [],
    "session_file": None,
    "done": False,
}

TOOLS = {}


# --- Small helpers -----------------------------------------------------------


def _expand(path: str) -> str:
    return os.path.expandvars(os.path.expanduser(path))


def _as_timeout(value, default: float = 120.0) -> float:
    try:
        out = float(value)
        return out if out > 0 else default
    except (TypeError, ValueError):
        return default


def fmt_ts() -> str: return datetime.now().strftime("%H:%M:%S.") + f"{datetime.now().microsecond // 1000:03d}"


def fmt_elapsed() -> str:
    s = max(0, int(time.monotonic() - STATE.get("t_start_mono", time.monotonic())))
    return f"{s // 60:02d}:{s % 60:02d}"


class Heartbeat:
    """Guarantees fresh, human-readable progress at least every `interval` seconds.

    Every beat is UNIQUE (timestamp + update counter + rotating phrase + elapsed
    seconds), so the console always shows new information - never a repeat - and
    never goes silent for more than `interval` while a phase runs. Beats shorter
    than one interval stay silent to avoid noise.
    """

    PHRASES = [
        "working on it",
        "still active",
        "making progress",
        "continuing",
        "in progress",
        "busy on the current step",
        "processing",
        "on it",
    ]

    def __init__(self, console: "Console", interval: float = 3.0, action=None, schedule=None):
        self.console = console
        self.interval = interval
        self.schedule = list(schedule) if schedule else None
        self.action = action  # callable(line_text) or None -> console print
        self._stop = threading.Event()
        self._thread = None
        self._n = 0
        self._phase = ""
        self._detail = ""
        self._t0 = 0.0
        self._i = 0
        self._lock = threading.Lock()

    def start(self, phase: str, detail: str = "") -> None:
        with self._lock:
            self._phase = phase
            self._detail = detail
            self._t0 = time.monotonic()
            self._n = 0
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # Announce the phase immediately (beat #0) so a new phase is visible at once.
        line = self._compose()
        try:
            if self.action is not None:
                self.action(line)
            elif getattr(self.console, "is_terminal", False):
                self.console.print(f"[dim]{line}[/]")
            else:
                print(line, file=(self.console.file or sys.stdout), flush=True)
        except Exception:
            pass

    def update(self, detail: str) -> None:
        with self._lock:
            self._detail = detail

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _compose(self) -> str:
        with self._lock:
            elapsed = time.monotonic() - self._t0
            self._n += 1
            phrase = self.PHRASES[(self._n - 1) % len(self.PHRASES)]
            detail = self._detail
            phase = self._phase
            n = self._n
        line = f"{fmt_ts()} | {phase} | {phrase}"
        if detail:
            line += f" | {detail}"
        line += f" | {elapsed:.0f}s on this step | update #{n}"
        return line

    def _run(self) -> None:
        while not self._stop.wait(self._next_delay()):
            line = self._compose()
            try:
                if self.action is not None:
                    self.action(line)
                elif getattr(self.console, "is_terminal", False):
                    self.console.print(f"[dim]{line}[/]")
                else:
                    # Not a terminal (piped/logged run): print straight to the
                    # underlying stream - a rich Live context would swallow
                    # console.print() output, and the <=5s guarantee must hold.
                    print(line, file=(self.console.file or sys.stdout), flush=True)
            except Exception:
                pass

    def _next_delay(self) -> float:
        if not self.schedule:
            return self.interval
        delay = self.schedule[min(self._i, len(self.schedule) - 1)]
        self._i += 1
        return delay


def _read_text_fallback(path: Path, max_bytes=None) -> str:
    raw = path.read_bytes()
    if max_bytes is not None:
        raw = raw[: int(max_bytes)]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return raw.decode("latin-1")
        except UnicodeDecodeError:
            return raw.decode("utf-8", errors="replace")


def _resolve_cwd(args) -> str:
    """Per-call working directory (default: session cwd). Validates existence."""
    raw = str((args or {}).get("cwd", "") or "").strip()
    if raw:
        p = Path(_expand(raw))
        if p.is_dir():
            return str(p)
    return STATE["cwd"]


def _run_captured(cmd, timeout_s: float, shell: bool = False, cwd: str = None) -> str:
    workdir = cwd or STATE["cwd"]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_s,
            cwd=workdir,
            shell=shell,
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout_s:g}s"
    except FileNotFoundError as exc:
        return f"ERROR: executable not found: {exc}"
    except OSError as exc:
        return f"ERROR: {exc}"
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return f"[cwd: {workdir}]\n" + (out or "(no output)") + f"\n[exit code: {proc.returncode}]"


def register_tool(name: str, description: str, parameters: dict):
    """Decorator: register a tool handler with its name, description and JSON schema."""

    def deco(fn):
        TOOLS[name] = {"fn": fn, "description": description, "parameters": parameters}
        return fn

    return deco


# --- Tools (exact registry: 24) ----------------------------------------------


@register_tool(
    "run_powershell",
    "Execute a PowerShell command on this Windows machine (any drive/path allowed; full permission). "
    "Returns combined stdout/stderr and the exit code.",
    {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "PowerShell command/pipeline to run"},
            "cwd": {"type": "string", "description": "Working directory for this command - set it to the folder of the script/file so its relative paths resolve"},
            "timeout_s": {"type": "integer", "description": "Timeout in seconds (default 120)"},
        },
        "required": ["command"],
    },
)
def tool_run_powershell(args: dict) -> str:
    try:
        command = str(args.get("command", ""))
        if not command.strip():
            return "ERROR: command required"
        return _run_captured(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            _as_timeout(args.get("timeout_s")),
            cwd=_resolve_cwd(args),
        )
    except Exception as exc:
        return f"ERROR: {exc}"


@register_tool(
    "run_cmd",
    "Execute a Windows cmd.exe command (cmd /c) on any drive/path; full permission. "
    "Returns combined stdout/stderr and the exit code.",
    {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "cmd.exe command line to run"},
            "cwd": {"type": "string", "description": "Working directory for this command - set it to the folder of the script/file so its relative paths resolve"},
            "timeout_s": {"type": "integer", "description": "Timeout in seconds (default 120)"},
        },
        "required": ["command"],
    },
)
def tool_run_cmd(args: dict) -> str:
    try:
        command = str(args.get("command", ""))
        if not command.strip():
            return "ERROR: command required"
        return _run_captured(["cmd", "/c", command], _as_timeout(args.get("timeout_s")), cwd=_resolve_cwd(args))
    except Exception as exc:
        return f"ERROR: {exc}"


@register_tool(
    "run_python",
    "Run Python source code with the current interpreter on any path. The code is written to a temp "
    "file and executed; returns stdout/stderr plus the exit code.",
    {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python source code to execute"},
            "cwd": {"type": "string", "description": "Working directory for this command - set it to the folder of the script/file so its relative paths resolve"},
            "timeout_s": {"type": "integer", "description": "Timeout in seconds (default 120)"},
        },
        "required": ["code"],
    },
)
def tool_run_python(args: dict) -> str:
    code = str(args.get("code", ""))
    if not code.strip():
        return "ERROR: code required"
    timeout_s = _as_timeout(args.get("timeout_s"))
    tmp_path = UAI_DIR / f"tmp_run_{os.getpid()}_{int(time.time() * 1000)}.py"
    try:
        UAI_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(code, encoding="utf-8")
        return _run_captured([sys.executable, str(tmp_path)], timeout_s, cwd=_resolve_cwd(args))
    except Exception as exc:
        return f"ERROR: {exc}"
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


@register_tool(
    "read_file",
    "Read a text file from any drive/path with encoding fallback (utf-8 -> latin-1 -> replace). "
    "Returns up to max_bytes bytes of content.",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute or relative file path"},
            "max_bytes": {"type": "integer", "description": "Maximum bytes to read (default 200000)"},
        },
        "required": ["path"],
    },
)
def tool_read_file(args: dict) -> str:
    try:
        raw_path = str(args.get("path", ""))
        if not raw_path.strip():
            return "ERROR: path required"
        path = Path(_expand(raw_path))
        try:
            max_bytes = max(1, int(args.get("max_bytes") or 200000))
        except (TypeError, ValueError):
            max_bytes = 200000
        if not path.exists():
            return f"ERROR: file not found: {path}"
        if path.is_dir():
            return f"ERROR: path is a directory (use list_dir): {path}"
        size = path.stat().st_size
        text = _read_text_fallback(path, max_bytes=max_bytes)
        note = f"\n[... truncated {size - max_bytes} more bytes; raise max_bytes to read more]" if size > max_bytes else ""
        return f"FILE: {path} ({size} bytes)\n\n{text}{note}"
    except Exception as exc:
        return f"ERROR: {exc}"


@register_tool(
    "write_file",
    "Write (create or overwrite) a file with UTF-8 text on any drive/path. Parent directories are created automatically.",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to write"},
            "content": {"type": "string", "description": "Full file content to write"},
        },
        "required": ["path", "content"],
    },
)
def tool_write_file(args: dict) -> str:
    try:
        raw_path = str(args.get("path", ""))
        if not raw_path.strip():
            return "ERROR: path required"
        content = args.get("content", "")
        path = Path(_expand(raw_path))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content), encoding="utf-8")
        return f"Wrote {len(str(content))} chars to {path}"
    except Exception as exc:
        return f"ERROR: {exc}"


@register_tool(
    "edit_file",
    "Exact-string replace inside a text file. Errors if old_string is not found. Set replace_all=true to replace every occurrence.",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to edit"},
            "old_string": {"type": "string", "description": "Exact text to find (must exist)"},
            "new_string": {"type": "string", "description": "Replacement text"},
            "replace_all": {"type": "boolean", "description": "Replace all occurrences (default false)"},
        },
        "required": ["path", "old_string", "new_string"],
    },
)
def tool_edit_file(args: dict) -> str:
    try:
        raw_path = str(args.get("path", ""))
        old = args.get("old_string")
        new = args.get("new_string", "")
        replace_all = bool(args.get("replace_all", False))
        if not raw_path.strip():
            return "ERROR: path required"
        if old is None or str(old) == "":
            return "ERROR: old_string required"
        path = Path(_expand(raw_path))
        if not path.is_file():
            return f"ERROR: file not found: {path}"
        text = _read_text_fallback(path)
        old_s, new_s = str(old), str(new)
        count = text.count(old_s)
        if count == 0:
            return "ERROR: old_string not found in file"
        if replace_all:
            new_text = text.replace(old_s, new_s)
            made = count
        else:
            new_text = text.replace(old_s, new_s, 1)
            made = 1
        path.write_text(new_text, encoding="utf-8")
        return f"Edited {path}: {made} replacement(s) applied."
    except Exception as exc:
        return f"ERROR: {exc}"


@register_tool(
    "list_dir",
    "List a directory tree on any drive/path up to the given depth, with file sizes. Directories are listed first.",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory path (default: session working directory)"},
            "depth": {"type": "integer", "description": "Recursion depth 1-8 (default 1)"},
        },
        "required": [],
    },
)
def tool_list_dir(args: dict) -> str:
    try:
        root = Path(_expand(str(args.get("path", "") or STATE["cwd"])))
        try:
            depth = max(1, min(8, int(args.get("depth", 1) or 1)))
        except (TypeError, ValueError):
            depth = 1
        if not root.exists():
            return f"ERROR: path not found: {root}"
        lines = []

        def walk(directory: Path, level: int) -> None:
            try:
                entries = sorted(directory.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
            except OSError as exc:
                lines.append("  " * level + f"[access denied: {exc}]")
                return
            for entry in entries:
                if len(lines) >= 400:
                    lines.append("  " * level + "... [truncated]")
                    return
                try:
                    if entry.is_dir():
                        lines.append("  " * level + entry.name + "/")
                        if level + 1 < depth:
                            walk(entry, level + 1)
                    else:
                        size = entry.stat().st_size
                        lines.append("  " * level + f"{entry.name}  ({size} B)")
                except OSError as exc:
                    lines.append("  " * level + f"{entry.name}  [error: {exc}]")

        walk(root, 0)
        listing = "\n".join(lines) if lines else "(empty directory)"
        return f"DIR: {root} (depth {depth})\n{listing}"
    except Exception as exc:
        return f"ERROR: {exc}"


@register_tool(
    "find_files",
    "Find files by glob pattern under a root directory, recursively, on any drive. Results are capped at max_results.",
    {
        "type": "object",
        "properties": {
            "root": {"type": "string", "description": "Root directory to search (default: session cwd)"},
            "pattern": {"type": "string", "description": "Glob pattern, e.g. *.py (default *)"},
            "max_results": {"type": "integer", "description": "Maximum number of results (default 200)"},
        },
        "required": ["pattern"],
    },
)
def tool_find_files(args: dict) -> str:
    try:
        root = Path(_expand(str(args.get("root", "") or STATE["cwd"])))
        pattern = str(args.get("pattern", "") or "*")
        try:
            max_results = max(1, int(args.get("max_results", 200) or 200))
        except (TypeError, ValueError):
            max_results = 200
        if not root.exists():
            return f"ERROR: root not found: {root}"
        found = []
        stopped = ""
        try:
            for path in root.rglob(pattern):
                try:
                    if path.is_file():
                        found.append(str(path))
                except OSError:
                    pass
                if len(found) >= max_results:
                    break
        except OSError as exc:
            stopped = f" [scan stopped early: {exc}]"
        if not found:
            return f"No files matching '{pattern}' under {root}.{stopped}"
        trunc = " (truncated at max_results)" if len(found) >= max_results else ""
        return f"{len(found)} file(s) matching '{pattern}' under {root}{trunc}{stopped}:\n" + "\n".join(found)
    except Exception as exc:
        return f"ERROR: {exc}"


@register_tool(
    "search_files",
    "Find files under a root directory. TWO MODES: (1) provide 'query' to grep file CONTENTS for text "
    "(returns path:line: text); (2) omit 'query' and set 'glob_pattern' to find FILES BY NAME "
    "(e.g. glob_pattern='*report*' returns matching file paths). Recursive, any drive.",
    {
        "type": "object",
        "properties": {
            "root": {"type": "string", "description": "Root directory to search (default: session cwd)"},
            "query": {"type": "string", "description": "Text to search for INSIDE files (omit for name-only search)"},
            "glob_pattern": {"type": "string", "description": "Only consider files matching this glob (default *)"},
            "max_results": {"type": "integer", "description": "Maximum matches (default 100)"},
            "max_seconds": {"type": "integer", "description": "Scan deadline in seconds (default 60)"},
        },
        "required": [],
    },
)
def tool_search_files(args: dict) -> str:
    try:
        root = Path(_expand(str(args.get("root", "") or STATE["cwd"])))
        query = str(args.get("query", "")).strip()
        glob_pattern = str(args.get("glob_pattern", "") or "*")
        try:
            max_results = max(1, int(args.get("max_results", 100) or 100))
        except (TypeError, ValueError):
            max_results = 100
        try:
            deadline_s = max(5.0, float(args.get("max_seconds", 60) or 60))
            deadline = time.monotonic() + deadline_s
        except (TypeError, ValueError):
            deadline_s = 60.0
            deadline = time.monotonic() + 60.0
        if not root.exists():
            return f"ERROR: root not found: {root}"

        # NAME-ONLY MODE: no query -> glob filename search (what callers usually
        # mean when they pass only glob_pattern). Never an error.
        if not query:
            base_glob = glob_pattern if any(ch in glob_pattern for ch in "*?[") else f"*{glob_pattern}*"
            found = []
            timed_out = False
            try:
                for path in root.rglob(base_glob):
                    try:
                        if path.is_file():
                            found.append(str(path))
                    except OSError:
                        pass
                    if len(found) >= max_results or time.monotonic() > deadline:
                        timed_out = time.monotonic() > deadline and len(found) < max_results
                        break
            except OSError as exc:
                return f"ERROR: scan failed: {exc}"
            note = ""
            if timed_out:
                note = f" [scan stopped at its {deadline_s:.0f}s deadline before finishing - narrow the root or pattern]"
            if not found:
                return (f"No files matching '{base_glob}' under {root}.{note}\n"
                        "Tip: this was a FILE-NAME search. To search text INSIDE files, pass 'query'.")
            trunc = " (truncated at max_results)" if len(found) >= max_results else ""
            return (f"{len(found)} file(s) matching '{base_glob}' under {root}{trunc}{note}:\n"
                    + "\n".join(found))

        needle = query.lower()
        results = []
        stopped = ""
        try:
            for path in root.rglob(glob_pattern):
                if len(results) >= max_results:
                    break
                if time.monotonic() > deadline:
                    stopped = f" [scan hit its deadline - partial results; narrow 'root' to search the rest]"
                    break
                try:
                    if not path.is_file() or path.stat().st_size > 2_000_000:
                        continue
                    raw = path.read_bytes()
                    if b"\x00" in raw[:4096]:
                        continue
                    text = raw.decode("utf-8", errors="replace")
                    for lineno, line in enumerate(text.splitlines(), 1):
                        if needle in line.lower():
                            results.append(f"{path}:{lineno}: {line.strip()[:200]}")
                            if len(results) >= max_results:
                                break
                except OSError:
                    continue
        except OSError as exc:
            stopped = f" [scan stopped early: {exc}]"
        if not results:
            return f"No matches for '{query}' under {root}.{stopped}"
        trunc = " (truncated at max_results)" if len(results) >= max_results else ""
        return f"{len(results)} match(es) for '{query}' under {root}{trunc}{stopped}:\n" + "\n".join(results)
    except Exception as exc:
        return f"ERROR: {exc}"


def _engine_ddgs(query: str, max_results: int):
    """ddgs package (DuckDuckGo) -> [(title, url, snippet)]."""
    if DDGS is None:
        return []
    with DDGS() as ddgs:
        out = []
        for r in ddgs.text(query, max_results=max_results):
            url = _clean_ddg_url(r.get("href") or r.get("url") or r.get("link") or "")
            out.append((r.get("title") or "", url, (r.get("body") or r.get("snippet") or "")[:300]))
        return out


def _readable_from_html(text: str) -> str:
    """HTML -> readable text: drops chrome (nav/menu/footer/...), converts
    tables to markdown rows, prefers <main>/<article> content."""
    if BeautifulSoup is not None:
        soup = BeautifulSoup(text, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "nav", "header", "footer", "aside", "form"]):
            tag.decompose()
        for tag in soup.find_all(attrs={"class": re.compile(r"(^|\s)(nav|menu|sidebar|footer|header)(\s|$)", re.I)}):
            tag.decompose()
        for table in soup.find_all("table"):
            rows = []
            for tr in table.find_all("tr"):
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
                if any(cells):
                    rows.append("| " + " | ".join(cells) + " |")
            if rows:
                table.replace_with(soup.new_string("\n" + "\n".join(rows) + "\n"))
        main = soup.find("main") or soup.find("article") or soup.find(id="content") or soup.find(role="main")
        return re.sub(r"\n{3,}", "\n\n", (main or soup).get_text("\n"))
    text = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text)


def _http_get_readable(url: str, max_bytes: int = 600000):
    """GET a URL and return (kind, text, status, ctype, total_bytes).
    kind is 'text' or 'pdf'. HTML is converted to readable text (tables become
    markdown rows). Raises httpx/HTTP errors to the caller."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    with _httpx().Client(timeout=30.0, follow_redirects=True, headers=_browser_headers()) as client:
        resp = client.get(url)
    resp.raise_for_status()
    ctype = resp.headers.get("content-type", "")
    if "pdf" in ctype.lower() or url.lower().split("?")[0].endswith(".pdf"):
        return ("pdf", "", resp.status_code, ctype, len(resp.content))
    raw = resp.content[:max_bytes]
    text = raw.decode(resp.encoding or "utf-8", errors="replace")
    looks_html = (
        "html" in ctype.lower()
        or text.lstrip()[:15].lower().startswith("<!doctype")
        or "<html" in text[:500].lower()
    )
    if looks_html:
        text = _readable_from_html(text)
    else:
        text = re.sub(r"\n{3,}", "\n\n", text)
    return ("text", text, resp.status_code, ctype, len(resp.content))


def _clean_ddg_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    if "uddg=" in href:
        m = re.search(r"uddg=([^&]+)", href)
        if m:
            return urllib.parse.unquote(m.group(1))
    return href


def _ddg_html_engine(query: str, max_results: int):
    """DuckDuckGo HTML endpoint -> [(title, url, snippet)]."""
    resp = _httpx().get(
        "https://html.duckduckgo.com/html/",
        params={"q": query},
        headers=_browser_headers(),
        timeout=15.0,
        follow_redirects=True,
    )
    resp.raise_for_status()
    out = []
    if BeautifulSoup is not None:
        soup = BeautifulSoup(resp.text, "html.parser")
        for res in soup.select("div.result, div.web-result")[:max_results]:
            a = res.select_one("a.result__a") or res.find("a", href=True)
            if a is None:
                continue
            snip = res.select_one(".result__snippet")
            out.append(
                (
                    a.get_text(" ", strip=True),
                    _clean_ddg_url(a.get("href", "")),
                    (snip.get_text(" ", strip=True) if snip else "")[:300],
                )
            )
    else:
        for m in re.finditer(
            r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', resp.text, re.DOTALL
        ):
            title = html_lib.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
            out.append((title, _clean_ddg_url(m.group(1)), ""))
            if len(out) >= max_results:
                break
    return [r for r in out if r[1]]


def _engine_bing(query: str, max_results: int):
    """Bing web results -> [(title, url, snippet)]."""
    resp = _httpx().get(
        "https://www.bing.com/search",
        params={"q": query, "count": str(max(10, max_results))},
        headers=_browser_headers(),
        timeout=15.0,
        follow_redirects=True,
    )
    resp.raise_for_status()
    out = []
    if BeautifulSoup is not None:
        soup = BeautifulSoup(resp.text, "html.parser")
        for li in soup.select("li.b_algo")[:max_results]:
            a = li.select_one("h2 a") or li.find("a", href=True)
            if a is None or not a.get("href"):
                continue
            cap = li.select_one(".b_caption p") or li.select_one("p")
            out.append(
                (
                    a.get_text(" ", strip=True),
                    a.get("href", ""),
                    (cap.get_text(" ", strip=True) if cap else "")[:300],
                )
            )
    return [r for r in out if r[1].startswith("http")]


def _engine_mojeek(query: str, max_results: int):
    """Mojeek (independent index, scrape-friendly) -> [(title, url, snippet)]."""
    resp = _httpx().get(
        "https://www.mojeek.com/search",
        params={"q": query},
        headers=_browser_headers(),
        timeout=15.0,
        follow_redirects=True,
    )
    resp.raise_for_status()
    out = []
    if BeautifulSoup is not None:
        soup = BeautifulSoup(resp.text, "html.parser")
        for li in soup.select("ul.results-standard li, li.result")[:max_results]:
            a = li.select_one("a.title") or li.find("a", href=True)
            if a is None:
                continue
            url = a.get("href", "")
            if not url.startswith("http"):
                continue
            p = li.select_one("p.s") or li.find("p")
            out.append(
                (
                    a.get_text(" ", strip=True),
                    url,
                    (p.get_text(" ", strip=True) if p else "")[:300],
                )
            )
    return [r for r in out if r[1]]


def _search_merge(query: str, max_results: int):
    """Multi-engine search with automatic failover: ddgs package -> DuckDuckGo
    HTML -> Bing -> Mojeek. Results are deduplicated by URL and the list of
    engines that contributed is returned alongside."""
    engines = (
        ("ddgs", lambda: _engine_ddgs(query, max_results)),
        ("duckduckgo", lambda: _ddg_html_engine(query, max_results)),
        ("bing", lambda: _engine_bing(query, max_results)),
        ("mojeek", lambda: _engine_mojeek(query, max_results)),
    )
    merged = []
    seen = set()
    used = []
    errors = []
    for name, fn in engines:
        if len(merged) >= max_results:
            break
        try:
            got = fn()
        except Exception as exc:
            got = []
            errors.append(f"{name}: {type(exc).__name__}")
        got = [r for r in got if r[1].startswith("http")]
        if got:
            used.append(name)
        for title, url, snip in got:
            key = url.rstrip("/").lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append((title, url, snip))
            if len(merged) >= max_results:
                break
    return merged, used, errors


@register_tool(
    "web_search",
    "Search the public web across MULTIPLE engines (DuckDuckGo, Bing, Mojeek) with automatic failover - "
    "no browser needed. Returns numbered title/URL/snippet results. For a ready-made digest of the top "
    "pages prefer web_research.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "max_results": {"type": "integer", "description": "Maximum results (default 8, max 20)"},
        },
        "required": ["query"],
    },
)
def tool_web_search(args: dict) -> str:
    try:
        query = str(args.get("query", "")).strip()
        if not query:
            return "ERROR: query required"
        try:
            max_results = max(1, min(20, int(args.get("max_results", 8) or 8)))
        except (TypeError, ValueError):
            max_results = 8
        merged, used, errors = _search_merge(query, max_results)
        if not merged:
            detail = ("; engine errors: " + "; ".join(errors)) if errors else ""
            return (
                "ERROR: web_search returned no results from any engine"
                + detail
                + ". Try rewording the query, or fetch a known page directly with fetch_url."
            )
        lines = []
        for i, (title, url, snip) in enumerate(merged[:max_results], 1):
            lines.append(f"{i}. {title}\n   {url}\n   {snip}")
        head = f"Web results for '{query}' (engines: {', '.join(used) or 'none'}):"
        return head + "\n" + "\n".join(lines)
    except Exception as exc:
        return f"ERROR: web_search failed: {exc}"


@register_tool(
    "web_research",
    "One-shot headless deep research: searches MULTIPLE engines, then downloads and reads the top "
    "sources in parallel (no browser window) and returns a readable digest of each with its URL - "
    "the fastest way to gather relevant material about any topic.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Research topic or question"},
            "max_sources": {"type": "integer", "description": "Top sources to open and digest (default 5, max 8)"},
            "chars_per_source": {"type": "integer", "description": "Readable characters per source (default 2800)"},
        },
        "required": ["query"],
    },
)
def tool_web_research(args: dict) -> str:
    try:
        query = str(args.get("query", "")).strip()
        if not query:
            return "ERROR: query required"
        try:
            max_sources = max(1, min(8, int(args.get("max_sources", 5) or 5)))
        except (TypeError, ValueError):
            max_sources = 5
        try:
            chars_per_source = max(800, min(8000, int(args.get("chars_per_source", 2800) or 2800)))
        except (TypeError, ValueError):
            chars_per_source = 2800
        from concurrent.futures import ThreadPoolExecutor

        merged, used, errors = _search_merge(query, max(max_sources + 3, 8))
        if not merged:
            detail = ("; engine errors: " + "; ".join(errors)) if errors else ""
            return "ERROR: web_research found no sources from any engine" + detail + "."
        picked = merged[:max_sources]

        def _digest(entry):
            title, url, snip = entry
            try:
                kind, text, status, ctype, _total = _http_get_readable(url, max_bytes=600000)
                if kind == "pdf":
                    body = "(PDF document - fetch and read it with pypdf via run_python if the contents are essential.)"
                else:
                    body = text[:chars_per_source]
                return f"### {title}\nURL: {url}\n{body}\n"
            except Exception as exc:
                return f"### {title}\nURL: {url}\n(could not be read: {type(exc).__name__}: {str(exc)[:120]}; search snippet: {snip})\n"

        with ThreadPoolExecutor(max_workers=min(6, len(picked))) as pool:
            digests = list(pool.map(_digest, picked))
        head = (
            f"Research digest for '{query}' - top {len(digests)} sources "
            f"(search engines: {', '.join(used) or 'none'}; read headlessly, no browser):\n"
        )
        tail = (
            "\nUse these digests as your sources (cite the URLs). Need more of one page? "
            "fetch_url that URL with start_char."
        )
        return head + "\n".join(digests) + tail
    except Exception as exc:
        return f"ERROR: web_research failed: {exc}"


@register_tool(
    "fetch_url",
    "Download a URL over HTTP(S) headlessly (no browser) and return READABLE text: page chrome is "
    "stripped, tables become markdown rows, and a note tells you the total length so you can read "
    "further sections with start_char. PDFs are detected with reading instructions.",
    {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "start_char": {"type": "integer", "description": "Readable-text offset to start from (read long pages in 45k chunks)"},
            "max_chars": {"type": "integer", "description": "Max readable characters returned (default 45000)"},
            "max_bytes": {"type": "integer", "description": "Maximum bytes to download (default 600000)"},
        },
        "required": ["url"],
    },
)
def tool_fetch_url(args: dict) -> str:
    try:
        url = str(args.get("url", "")).strip()
        if not url:
            return "ERROR: url required"
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        try:
            max_bytes = max(1000, int(args.get("max_bytes", 600000) or 600000))
        except (TypeError, ValueError):
            max_bytes = 600000
        try:
            start_char = max(0, int(args.get("start_char", 0) or 0))
        except (TypeError, ValueError):
            start_char = 0
        kind, text, status, ctype, total_bytes = _http_get_readable(url, max_bytes=max_bytes)
        if kind == "pdf":
            return (
                f"URL: {url} (HTTP {status}, PDF document, {total_bytes} bytes)\n\n"
                "This is a PDF (binary). To read its text: run_python with `pip install pypdf` then "
                "`from pypdf import PdfReader; print(PdfReader(url_downloaded).pages[i].extract_text())`."
            )
        try:
            cap = max(12000, min(60000, int(args.get("max_chars", 45000) or 45000)))
        except (TypeError, ValueError):
            cap = 45000
        total_chars = len(text)
        note = ""
        if start_char:
            end = min(start_char + cap, total_chars)
            if start_char >= total_chars:
                return (
                    f"URL: {url} (HTTP {status})\n\nstart_char {start_char} is beyond the end of this page "
                    f"({total_chars} readable characters)."
                )
            text = text[start_char:end]
            note = f"\n[SHOWING chars {start_char + 1}-{end} of {total_chars}. Use start_char={end} for the next part.]"
        elif total_chars > cap:
            text = text[:cap]
            note = (
                f"\n[SHOWING chars 1-{cap} of {total_chars} for this page. Refetching the same URL returns the "
                f"same first part - to read the next part call fetch_url again with start_char={cap}, "
                f"or fetch a more specific page.]"
            )
        size_note = f"\n[download truncated at max_bytes={max_bytes} of {total_bytes} bytes]" if total_bytes > max_bytes else ""
        return f"URL: {url} (HTTP {status}, {ctype or 'unknown type'})\n\n{text}{note}{size_note}"
    except Exception as exc:
        return f"ERROR: fetch_url failed: {exc}"


@register_tool(
    "open_in_browser",
    "Open a URL in the user's default VISUAL browser window. Last resort only - use web_search / web_research / fetch_url for anything text-based; open_in_browser is for pages that genuinely need interactive visual rendering.",
    {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to open"},
        },
        "required": ["url"],
    },
)
def tool_open_in_browser(args: dict) -> str:
    try:
        url = str(args.get("url", "")).strip()
        if not url:
            return "ERROR: url required"
        if not url.startswith(("http://", "https://", "file://")):
            url = "https://" + url
        ok = webbrowser.open(url)
        if ok:
            return f"Opened in default browser: {url}"
        return f"ERROR: the browser could not open {url}"
    except Exception as exc:
        return f"ERROR: {exc}"


@register_tool(
    "start_process",
    "Start a command as a detached background process (returns immediately with the pid). Use for servers, apps and long-running jobs.",
    {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Command line to start"},
            "detached": {"type": "boolean", "description": "Run detached (default true)"},
        },
        "required": ["command"],
    },
)
def tool_start_process(args: dict) -> str:
    try:
        command = str(args.get("command", "")).strip()
        if not command:
            return "ERROR: command required"
        detached = bool(args.get("detached", True))
        kwargs = {}
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP
            if detached:
                flags |= subprocess.DETACHED_PROCESS
            kwargs["creationflags"] = flags
        else:
            kwargs["start_new_session"] = True
        if detached:
            kwargs.update(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc = subprocess.Popen(command, shell=True, cwd=STATE["cwd"], **kwargs)
        kind = "detached " if detached else ""
        return f"Started {kind}process pid={proc.pid}: {command}"
    except Exception as exc:
        return f"ERROR: {exc}"


@register_tool(
    "list_processes",
    "List running processes (name, pid, memory) via psutil, optionally filtered by a name substring; returns the top 50 by memory.",
    {
        "type": "object",
        "properties": {
            "filter": {"type": "string", "description": "Case-insensitive name substring filter (default: all)"},
        },
        "required": [],
    },
)
def tool_list_processes(args: dict) -> str:
    try:
        flt = str(args.get("filter", "") or "").strip().lower()
        rows = []
        for proc in _psutil().process_iter(["pid", "name", "memory_info"]):
            try:
                name = proc.info.get("name") or "?"
                if flt and flt not in name.lower():
                    continue
                mem = proc.info.get("memory_info")
                mem_mb = (mem.rss / (1024 * 1024)) if mem else 0.0
                rows.append((mem_mb, name, proc.info.get("pid")))
            except Exception:
                continue
            if len(rows) >= 300:
                break
        if not rows:
            return f"No processes matching filter '{flt}'."
        rows.sort(reverse=True)
        lines = [f"{name}  pid={pid}  mem={mem:,.1f} MB" for mem, name, pid in rows[:50]]
        suffix = f" matching '{flt}'" if flt else ""
        return f"Top {len(lines)} process(es){suffix} by memory:\n" + "\n".join(lines)
    except Exception as exc:
        return f"ERROR: {exc}"


@register_tool(
    "stop_process",
    "Stop (terminate) a process by pid or by name substring via psutil. Escalates to kill if terminate does not work within 5s.",
    {
        "type": "object",
        "properties": {
            "pid": {"type": "integer", "description": "Process id to stop"},
            "name": {"type": "string", "description": "Process name substring to stop (all matches)"},
        },
        "required": [],
    },
)
def tool_stop_process(args: dict) -> str:
    try:
        target = str(args.get("pid") or args.get("name") or args.get("target") or "").strip()
        if not target:
            return "ERROR: provide 'pid' or 'name' of the process to stop"
        if re.fullmatch(r"\d+", target):
            try:
                proc = _psutil().Process(int(target))
                pname = proc.name()
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except _psutil().TimeoutExpired:
                    proc.kill()
                return f"Stopped process {pname} (pid {target})."
            except _psutil().NoSuchProcess:
                return f"ERROR: no process with pid {target}"
        victims = []
        for proc in _psutil().process_iter(["pid", "name"]):
            pname = proc.info.get("name") or ""
            if target.lower() in pname.lower():
                victims.append(proc)
        if not victims:
            return f"ERROR: no process matching '{target}'"
        stopped, failed = [], 0
        for proc in victims:
            try:
                proc.terminate()
                stopped.append(proc.info.get("pid"))
            except Exception:
                failed += 1
        return f"Sent terminate to {len(stopped)} process(es) matching '{target}' (pids: {stopped[:20]}); {failed} failed."
    except Exception as exc:
        return f"ERROR: {exc}"


@register_tool(
    "screenshot",
    "Capture the full virtual screen to a PNG file (pure PowerShell, no extra dependencies). "
    "Default save location: ~/.ultimate-ai/shots/. Returns the saved path.",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Output PNG path (default: auto-named under ~/.ultimate-ai/shots/)"},
        },
        "required": [],
    },
)
def tool_screenshot(args: dict) -> str:
    try:
        out_raw = str(args.get("path") or "")
        if out_raw.strip():
            out = Path(_expand(out_raw))
        else:
            out = SHOTS_DIR / f"screenshot-{datetime.now():%Y%m%d-%H%M%S}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        out_ps = str(out).replace("'", "''")
        script = UAI_DIR / "tmp_screenshot.ps1"
        script.write_text(
            "Add-Type -AssemblyName System.Windows.Forms\n"
            "Add-Type -AssemblyName System.Drawing\n"
            "$vs = [System.Windows.Forms.SystemInformation]::VirtualScreen\n"
            "$bmp = New-Object System.Drawing.Bitmap($vs.Width, $vs.Height)\n"
            "$gfx = [System.Drawing.Graphics]::FromImage($bmp)\n"
            "$gfx.CopyFromScreen($vs.X, $vs.Y, 0, 0, $bmp.Size)\n"
            f"$bmp.Save('{out_ps}', [System.Drawing.Imaging.ImageFormat]::Png)\n"
            "Write-Output 'ok'\n",
            encoding="utf-8-sig",
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=90,
        )
        try:
            script.unlink(missing_ok=True)
        except Exception:
            pass
        if out.exists() and out.stat().st_size > 0:
            return f"Screenshot saved: {out} ({out.stat().st_size} bytes)"
        detail = ((proc.stderr or "") + (proc.stdout or "")).strip() or "screenshot file was not created"
        return f"ERROR: screenshot failed: {detail[:500]}"
    except Exception as exc:
        return f"ERROR: {exc}"


@register_tool(
    "clipboard_get",
    "Read the current clipboard text (pyperclip, PowerShell fallback).",
    {"type": "object", "properties": {}, "required": []},
)
def tool_clipboard_get(args: dict) -> str:
    try:
        try:
            text = _pyperclip().paste()
            if text is not None:
                return text if text else "(clipboard is empty)"
        except Exception:
            pass
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=20,
        )
        return (proc.stdout or "") if proc.stdout else "(clipboard is empty)"
    except Exception as exc:
        return f"ERROR: {exc}"


@register_tool(
    "clipboard_set",
    "Copy text to the clipboard (pyperclip, PowerShell fallback).",
    {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to put on the clipboard"},
        },
        "required": ["text"],
    },
)
def tool_clipboard_set(args: dict) -> str:
    try:
        text = args.get("text")
        if text is None:
            return "ERROR: text required"
        text = str(text)
        try:
            _pyperclip().copy(text)
            return f"Copied {len(text)} chars to the clipboard."
        except Exception:
            pass
        tmp = UAI_DIR / "tmp_clipboard.txt"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        literal = str(tmp).replace("'", "''")
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"Get-Content -Raw -LiteralPath '{literal}' | Set-Clipboard"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=20,
        )
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return f"Copied {len(text)} chars to the clipboard (PowerShell fallback)."
    except Exception as exc:
        return f"ERROR: {exc}"


@register_tool(
    "notify_user",
    "Notify the user: shows an in-terminal notification + sound (console beep). Use to grab attention or report milestones.",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short notification title"},
            "message": {"type": "string", "description": "Notification message body"},
        },
        "required": ["title", "message"],
    },
)
def tool_notify_user(args: dict) -> str:
    title = str(args.get("title", "Ultimate AI"))
    message = str(args.get("message", ""))
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", "[console]::beep(880,220); [console]::beep(1180,220)"],
            capture_output=True,
            timeout=15,
        )
    except Exception:
        pass
    console = STATE.get("console")
    if console is not None:
        try:
            console.print(Panel(f"[bold]{title}[/]\n{message}", title="NOTIFICATION", border_style="magenta"))
        except Exception:
            pass
    return f"Notification shown (in-terminal + sound): {title} - {message}"


@register_tool(
    "git_run",
    "Run any git command inside a repository path (git -C <repo_path> <arguments>). Returns output and exit code.",
    {
        "type": "object",
        "properties": {
            "repo_path": {"type": "string", "description": "Repository working tree path (default: session cwd)"},
            "arguments": {"type": "string", "description": "Git arguments, e.g. 'status' or 'commit -m \"msg\"'"},
        },
        "required": ["arguments"],
    },
)
def tool_git_run(args: dict) -> str:
    try:
        repo = _expand(str(args.get("repo_path", "") or STATE["cwd"]))
        if not Path(repo).exists():
            return f"ERROR: repo path not found: {repo}"
        arguments = args.get("arguments", args.get("args", ""))
        if isinstance(arguments, list):
            cmd = ["git", "-C", repo] + [str(a) for a in arguments]
            return _run_captured(cmd, 180.0)
        arguments = str(arguments or "").strip() or "status"
        return _run_captured(f'git -C "{repo}" {arguments}', 180.0, shell=True)
    except Exception as exc:
        return f"ERROR: {exc}"


GITIGNORE_BASIC = (
    "__pycache__/\n*.pyc\n.venv/\n.env\nnode_modules/\ndist/\nbuild/\n*.log\n"
)

README_GENERIC = "# Project\n\nScaffolded by Ultimate AI (scaffold_project).\n"

README_PY = (
    "# Python Project\n\n"
    "Scaffolded by Ultimate AI (scaffold_project).\n\n"
    "## Setup (recommended venv)\n\n"
    "    python -m venv .venv\n"
    "    .venv\\Scripts\\activate\n"
    "    pip install -r requirements.txt\n\n"
    "## Run\n\n"
    "    python main.py\n"
)

MAIN_PY = (
    'def main():\n'
    '    print("Hello from Ultimate AI!")\n'
    '\n\n'
    'if __name__ == "__main__":\n'
    "    main()\n"
)

PACKAGE_JSON = (
    '{\n'
    '  "name": "scaffolded-app",\n'
    '  "version": "1.0.0",\n'
    '  "description": "Created by Ultimate AI scaffold_project",\n'
    '  "main": "index.js",\n'
    '  "scripts": {\n'
    '    "start": "node index.js"\n'
    "  }\n"
    "}\n"
)

MAIN_PS1 = '# Created by Ultimate AI scaffold_project\nWrite-Host "Hello from Ultimate AI!"\n'

WEB_HTML = (
    "<!DOCTYPE html>\n"
    '<html lang="en">\n'
    "<head>\n"
    '  <meta charset="utf-8">\n'
    '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
    "  <title>Scaffolded Web App</title>\n"
    '  <link rel="stylesheet" href="style.css">\n'
    "</head>\n"
    "<body>\n"
    "  <main>\n"
    "    <h1>Hello from Ultimate AI</h1>\n"
    "    <p>Edit index.html, style.css and script.js to build your app.</p>\n"
    '    <button id="go">Click me</button>\n'
    "  </main>\n"
    '  <script src="script.js"></script>\n'
    "</body>\n"
    "</html>\n"
)

WEB_CSS = "body { font-family: system-ui, sans-serif; margin: 3rem; }\nbutton { padding: .5rem 1rem; cursor: pointer; }\n"

WEB_JS = 'document.getElementById("go")?.addEventListener("click", () => alert("It works!"));\n'

SCAFFOLDS = {
    "python": {
        "README.md": README_PY,
        ".gitignore": GITIGNORE_BASIC,
        "main.py": MAIN_PY,
    },
    "node": {
        "README.md": README_GENERIC,
        ".gitignore": GITIGNORE_BASIC,
        "package.json": PACKAGE_JSON,
        "index.js": 'console.log("Hello from Ultimate AI!");\n',
    },
    "powershell": {
        "README.md": README_GENERIC,
        ".gitignore": GITIGNORE_BASIC,
        "main.ps1": MAIN_PS1,
    },
    "web": {
        "README.md": README_GENERIC,
        ".gitignore": GITIGNORE_BASIC,
        "index.html": WEB_HTML,
        "style.css": WEB_CSS,
        "script.js": WEB_JS,
    },
    "empty": {
        "README.md": README_GENERIC,
        ".gitignore": GITIGNORE_BASIC,
    },
}


@register_tool(
    "scaffold_project",
    "Create a starter project directory with README.md, .gitignore and starter files for the given kind "
    "(python: main.py + venv note; node: package.json + index.js; powershell: main.ps1; web: index.html + style.css + script.js; empty).",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Project directory to create (any drive/path)"},
            "kind": {"type": "string", "description": "One of: python, node, powershell, web, empty"},
        },
        "required": ["path", "kind"],
    },
)
def tool_scaffold_project(args: dict) -> str:
    try:
        raw_path = str(args.get("path", ""))
        kind = str(args.get("kind", "")).strip().lower()
        if not raw_path.strip():
            return "ERROR: path required"
        if kind not in SCAFFOLDS:
            return f"ERROR: kind must be one of {sorted(SCAFFOLDS)}"
        root = Path(_expand(raw_path))
        root.mkdir(parents=True, exist_ok=True)
        created = []
        for fname, content in SCAFFOLDS[kind].items():
            fpath = root / fname
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(content, encoding="utf-8")
            created.append(fname)
        listing = "\n".join(f"  - {c}" for c in created)
        return f"Scaffolded '{kind}' project at {root}\nFiles created:\n{listing}"
    except Exception as exc:
        return f"ERROR: {exc}"


@register_tool(
    "memory_read",
    "Read persistent notes from ~/.ultimate-ai/notes.md; with query, return only lines containing it (case-insensitive).",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Optional filter substring (default: return all notes)"},
        },
        "required": [],
    },
)
def tool_memory_read(args: dict) -> str:
    try:
        if not NOTES_FILE.exists():
            return "(memory is empty - nothing stored yet)"
        content = NOTES_FILE.read_text(encoding="utf-8", errors="replace")
        query = str(args.get("query", "") or "").strip().lower()
        if query:
            hits = [line for line in content.splitlines() if query in line.lower()]
            return "\n".join(hits) if hits else f"(no memory entries matching '{query}')"
        return content[:20000] or "(memory is empty)"
    except Exception as exc:
        return f"ERROR: {exc}"


@register_tool(
    "memory_write",
    "Append a timestamped note to persistent memory (~/.ultimate-ai/notes.md). Use for facts worth remembering across turns.",
    {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Note text to remember"},
        },
        "required": ["text"],
    },
)
def tool_memory_write(args: dict) -> str:
    try:
        text = str(args.get("text") or args.get("content") or "").strip()
        if not text:
            return "ERROR: text required"
        NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(NOTES_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"- [{datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n")
        return f"Memory saved: {text[:200]}"
    except Exception as exc:
        return f"ERROR: {exc}"


@register_tool(
    "task_done",
    "Call this ONLY when the user's goal is fully complete and verified. Pass a concise summary of what was accomplished; "
    "calling it ends the agent loop.",
    {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Summary of the completed work"},
        },
        "required": ["summary"],
    },
)
def tool_task_done(args: dict) -> str:
    summary = str(args.get("summary", "") or "").strip()
    if not summary or summary.lower() in ("done", "complete", "finished"):
        return "ERROR: task_done rejected - provide a real summary stating what was accomplished and verified"
    STATE["done"] = True
    return summary


# --- Registry / schemas ------------------------------------------------------


def ollama_tools_schema() -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": tool["description"],
                "parameters": tool["parameters"],
            },
        }
        for name, tool in TOOLS.items()
    ]


# --- System prompt -----------------------------------------------------------


def build_system_prompt(override_path=None) -> str:
    if override_path:
        return Path(override_path).read_text(encoding="utf-8", errors="replace")
    now = datetime.now()
    tool_docs = "\n".join(
        f"- {name}({', '.join((tool['parameters'].get('properties') or {}).keys())}): {tool['description']}"
        for name, tool in TOOLS.items()
    )
    return (
        "You are Ultimate AI, a full-permission autonomous agent running locally on this Windows machine.\n"
        "You have unrestricted permission to use every tool on ANY drive or path. Never ask for confirmation\n"
        "and never request permission: auto-approve everything and act.\n"
        "\n"
        f"Today's date: {now:%Y-%m-%d} ({now:%A})\n"
        f"Working directory: {STATE['cwd']}\n"
        f"OS: {platform.platform()} | Python {platform.python_version()} | Ollama host: {STATE['host']}\n"
        + (
            "\nGROUND TRUTH ABOUT THIS MACHINE (authoritative - NEVER contradict these facts and NEVER\n"
            "misreport the hardware; WMI AdapterRAM is 32-bit and WRONG for GPUs larger than 4 GB - use\n"
            "these verified facts, or live nvidia-smi output, for any hardware claim):\n"
            + HW_GROUND_TRUTH
            + "\n"
            if HW_GROUND_TRUTH
            else ""
        )
        + "\n"
        "BEHAVIOR CONTRACT:\n"
        "- NEVER argue with, question, or push back on the user's request. Execute it immediately and\n"
        "  completely; if something is genuinely impossible, say exactly what you need in one line and\n"
        "  try the closest achievable alternative anyway.\n"
        "- NEVER stop before the goal is achieved and verified - work through every error.\n"
        "- ANSWER STYLE: every answer must be beautifully readable for a human - a short lead line first,\n"
        "  then markdown headings/tables/bullets, bold the key facts, no raw dumps unless asked.\n"
        "\n"
        "MANDATE: Never stop until the user's goal is fully done. Use tools proactively instead of asking\n"
        "questions. Verify your own work (re-read files, re-run commands, run tests) before claiming success.\n"
        "When the goal is fully complete and verified, call the task_done tool with a concise summary.\n"
        "\n"
        "HOW TO ACT - native tool calling:\n"
        "When you decide to act, emit a native tool call for one of the tools listed below. After each tool\n"
        "result you may emit the next tool call. Multiple sequential tool calls are expected; keep going\n"
        "until the goal is complete.\n"
        "\n"
        "HOW TO ACT - JSON fallback protocol (for models with weak native tool support):\n"
        "If you cannot emit native tool calls, reply with EXACTLY ONE fenced JSON block and nothing else:\n"
        '```json\n{"tool": "<tool_name>", "args": {"<param>": "<value>"}}\n```\n'
        "The harness executes it and appends the result as a tool message. One tool call per reply.\n"
        "\n"
        "AVAILABLE TOOLS:\n"
        f"{tool_docs}"
        "\n\n"
        "CRITICAL OPERATION RULES (violating any of these = mission failure):\n"
        "1. ALWAYS use absolute paths (e.g. F:\\study\\AI_ML\\LocalAI\\ultimate-llm\\missions\\M02\\data\\file.csv) -\n"
        "   in every tool call AND inside every script you write. Relative paths resolve to an unexpected\n"
        "   working directory and silently create files in the wrong place.\n"
        "2. A tool result containing ERROR is a failure you must fix and retry. Never ignore an error; never\n"
        "   report success while any error is unresolved.\n"
        "3. Use the EXACT file names, folder names and output names the mission specifies. Never invent\n"
        "   variants (report.md is not report_final.md; solutions.txt is not solutions_clean.txt).\n"
        "4. Verify using the EXACT command and working directory the mission requires. A different command\n"
        "   that looks equivalent does not count as verification.\n"
        "5. run_powershell and run_cmd already execute inside PowerShell/cmd: pass raw commands directly.\n"
        "   Never wrap them in another powershell -Command / cmd /c layer. All three run tools accept a\n"
        "   'cwd' argument - when a command or script uses relative paths, pass cwd = that script's folder\n"
        "   so its relative paths resolve where you intend.\n"
        "6. A plain text reply without a tool call is treated as idle: keep making tool calls until the goal\n"
        "   is fully done, then call task_done.\n"
        "7. CHECKLIST DISCIPLINE: treat every numbered requirement in the mission as a hard checklist item.\n"
        "   Before task_done, walk through the mission requirement by requirement and verify EACH one with a\n"
        "   tool call (count the files, check each number, confirm each name). If any item is unmet, fix it\n"
        "   first - partial completion is failure.\n"
        "8. When a tool call fails to parse or errors repeatedly while writing long content, write the file\n"
        "   in smaller pieces or via run_python instead of one huge call, then re-read it to confirm.\n"
        "9. SCOPE ISOLATION: perform ALL work inside the exact directories the mission names. NEVER run git\n"
        "   init/commit/branch/checkout/reset or any other state-changing command against the folder that\n"
        "   contains the missions folder or any parent of it - that outer workspace is protected\n"
        "   infrastructure. Git work happens ONLY inside the mission's own sandbox repo path; if a git\n"
        "   command says something already exists or finds an unexpected repository, you are in the wrong\n"
        "   directory - target the sandbox path explicitly (git -C <sandbox-repo> ...) and git init there.\n"
        "10. TOOL SEMANTICS: search_files finds files BY NAME when you OMIT 'query' (set glob_pattern only),\n"
        "    and searches file CONTENTS when you pass 'query'. fetch_url returns the FIRST section of a page\n"
        "    (with character totals in a note at the end) - NEVER refetch the same URL expecting different\n"
        "    content; read another section with fetch_url + start_char (long pages come with a note telling "
        "you the exact next start_char). If a tool returns the same error\n"
        "    twice, change the arguments or approach immediately - the harness will also inject guidance.\n"
        "11. ONLINE RESEARCH: for anything you need from the internet use web_research (ONE call - "
        "multi-engine search plus parallel headless digests of the top sources with their URLs) or "
        "web_search + fetch_url. This reads the web directly with no browser window. Only use "
        "open_in_browser when a task explicitly requires interactive VISUAL rendering, since it opens a "
        "real window on the user's screen.\n"
        "\n"
        "VERIFICATION PROTOCOL — before calling task_done you MUST re-verify every artifact: open/read files\n"
        "you wrote, re-run scripts you created, check outputs match EVERY stated requirement (counts, sums,\n"
        "names, formats), and fix any gap found. Call task_done ONLY when all of that passed; the summary\n"
        "must briefly state what you verified and how."
    )


# --- Fallback parser + streamed tool-call assembler ---------------------------


def parse_fallback_tool_call(content):
    """Parse a {"tool": name, "args": {...}} call out of model content.

    Handles fenced ```json blocks first, then bare JSON objects (even with text
    around them). Returns (tool_name, args_dict) or None.
    """
    if not content or "{" not in content:
        return None
    candidates = []
    fence_re = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
    for m in fence_re.finditer(content):
        inner = m.group(1)
        s, e = inner.find("{"), inner.rfind("}")
        if s != -1 and e > s:
            candidates.append(inner[s : e + 1])
    s, e = content.find("{"), content.rfind("}")
    if s != -1 and e > s:
        candidates.append(content[s : e + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and isinstance(obj.get("tool"), str) and isinstance(obj.get("args"), dict):
            return obj["tool"], obj["args"]
    return None


def strip_fallback_json(content: str) -> str:
    """Remove the fallback JSON block from displayed content."""
    out = re.sub(r"```(?:json)?\s*\{.*\}\s*```", "", content, flags=re.DOTALL)
    if out == content:
        s, e = content.find("{"), content.rfind("}")
        if s != -1 and e > s:
            out = content[:s] + content[e + 1 :]
    return out.strip()


class ToolCallAssembler:
    """Merges streamed native tool_call fragments into complete calls.

    Fragments arrive per NDJSON chunk and are merged index-keyed: partial
    ``function.name`` strings are concatenated and ``function.arguments``
    fragments (string deltas or dict fragments) are accumulated until the
    final arguments parse as a JSON object.
    """

    def __init__(self):
        self._calls = {}

    def feed(self, message: dict) -> None:
        for i, tc in enumerate(message.get("tool_calls") or []):
            if not isinstance(tc, dict):
                continue
            idx = tc.get("index", i)
            slot = self._calls.setdefault(idx, {"id": "", "name": "", "args_str": "", "args_obj": None})
            if isinstance(tc.get("id"), str):
                slot["id"] += tc["id"]
            fn = tc.get("function") or {}
            if isinstance(fn.get("name"), str):
                slot["name"] += fn["name"]
            frag = fn.get("arguments")
            if isinstance(frag, str):
                slot["args_str"] += frag
            elif isinstance(frag, dict):
                if slot["args_obj"] is None:
                    slot["args_obj"] = {}
                slot["args_obj"].update(frag)

    def finalize(self) -> list:
        out = []
        for idx in sorted(self._calls):
            slot = self._calls[idx]
            if slot["args_obj"] is not None and not slot["args_str"].strip():
                args = slot["args_obj"]
            else:
                raw = slot["args_str"].strip()
                try:
                    args = json.loads(raw) if raw else {}
                except (json.JSONDecodeError, ValueError):
                    args = {"_raw": slot["args_str"]}
                if isinstance(args, dict) and slot["args_obj"]:
                    merged = dict(slot["args_obj"])
                    merged.update(args)
                    args = merged
            if not isinstance(args, dict):
                args = {"_raw": str(args)}
            out.append(
                {
                    "id": slot["id"] or f"call_{idx}",
                    "name": slot["name"],
                    "arguments": args,
                }
            )
        return out


# --- Ollama chat streaming ----------------------------------------------------


class ChatError(RuntimeError):
    """Non-fatal chat/protocol error (bad HTTP status, model error, stream error)."""


def chat_stream(console: Console):
    """Stream one assistant turn from Ollama POST {host}/api/chat (NDJSON).

    Returns (content, tool_calls). NO rich Live is used: Live redraws on a timer
    and forces Windows Terminal to scroll back to the bottom while the user is
    reading. Instead the Heartbeat prints sparse dim beat lines (5s then every
    15s on a terminal, every 3s when piped), and the caller renders the finished
    answer as full Markdown.
    """
    payload = {
        "model": STATE["model"],
        "messages": STATE["messages"],
        "stream": True,
        "keep_alive": "1h",
        "tools": ollama_tools_schema(),
    }
    if STATE.get("num_ctx"):
        payload["options"] = {"num_ctx": STATE["num_ctx"]}
    url = STATE["host"].rstrip("/") + "/api/chat"
    assembler = ToolCallAssembler()
    parts = []
    think_parts = []
    think_chars = [0]
    schedule = (3.0,) if not console.is_terminal else (5.0, 15.0)

    try:
        # The heartbeat must cover the WHOLE request: queue + prompt prefill
        # (which can take a minute+ on long conversations before the first
        # token) + streaming.
        hb = Heartbeat(console, schedule=schedule)
        hb.start("model working (queue + prefill + streaming)", f"model {STATE['model']}")
        try:
            with _httpx().Client(timeout=_httpx().Timeout(180.0, connect=10.0)) as client:
                with client.stream("POST", url, json=payload) as resp:
                    if resp.status_code != 200:
                        detail = resp.read().decode("utf-8", errors="replace")[:400]
                        raise ChatError(f"HTTP {resp.status_code} from {url}: {detail}")
                    for line in resp.iter_lines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if obj.get("error"):
                            raise ChatError(str(obj["error"]))
                        message = obj.get("message") or {}
                        think = message.get("thinking") or ""
                        if think:
                            think_parts.append(think)
                            think_chars[0] += len(think)
                        delta = message.get("content") or ""
                        if delta:
                            parts.append(delta)
                        if think or delta:
                            hb.update(
                                f"{sum(len(p) for p in parts)} answer chars, {think_chars[0]} reasoning chars so far"
                            )
                        if message.get("tool_calls"):
                            assembler.feed(message)
                        if obj.get("done"):
                            # Real context size from the server -> drives compaction.
                            pec = obj.get("prompt_eval_count")
                            if pec:
                                STATE["ctx_tokens"] = int(pec)
                            break
        finally:
            hb.stop()
    except _httpx().ConnectError as exc:
        raise ConnectionError(f"cannot reach Ollama at {STATE['host']} ({exc})") from exc
    except _httpx().ConnectTimeout as exc:
        raise ConnectionError(f"connection to Ollama at {STATE['host']} timed out ({exc})") from exc
    except _httpx().TransportError as exc:
        raise ConnectionError(f"Ollama connection dropped ({exc})") from exc
    think_text = "".join(think_parts)
    if think_text:
        # gpt-oss needs its past chain-of-thought round-tripped on tool-loop
        # follow-up requests or accuracy degrades each turn (scout finding).
        STATE["last_thinking"] = think_text
    return "".join(parts), assembler.finalize()


# --- Tool execution + UI -------------------------------------------------------


def execute_tool(name: str, args) -> str:
    entry = TOOLS.get(str(name).strip())
    if entry is None:
        return f"ERROR: unknown tool '{name}'. Available: {', '.join(sorted(TOOLS))}"
    try:
        tool_args = args if isinstance(args, dict) else {}
        return str(entry["fn"](tool_args))
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"


# Human-readable activity descriptions: used by the heartbeat so long-running
# tools always show WHAT is happening in plain English.
def _arg_str(args, key, limit=70):
    try:
        v = str((args or {}).get(key, "")).replace("\n", " ")
        return v[:limit] + ("…" if len(v) > limit else "")
    except Exception:
        return ""


def describe_tool_activity(name: str, args) -> str:
    args = args if isinstance(args, dict) else {}
    if name in ("run_powershell", "run_cmd"):
        return f"running the command: {_arg_str(args, 'command')}"
    if name == "run_python":
        return "running a Python script it wrote"
    if name == "fetch_url":
        return f"downloading {_arg_str(args, 'url')}"
    if name == "web_search":
        return f"searching the web for '{_arg_str(args, 'query')}'"
    if name == "web_research":
        return f"researching '{_arg_str(args, 'query')}' - searching several engines and reading the top sources headlessly"
    if name == "search_files":
        if str(args.get("query", "")).strip():
            return f"scanning {_arg_str(args, 'root')} for files containing '{_arg_str(args, 'query')}'"
        return f"finding files named like '{_arg_str(args, 'glob_pattern')}' under {_arg_str(args, 'root')}"
    if name == "find_files":
        return f"finding files matching '{_arg_str(args, 'pattern')}' under {_arg_str(args, 'root')}"
    if name in ("read_file", "write_file", "edit_file"):
        return {"read_file": "reading file", "write_file": "writing file", "edit_file": "editing file"}.get(name, f"processing {name}") + f" {_arg_str(args, 'path')}"
    if name == "list_dir":
        return f"listing directory {_arg_str(args, 'path')}"
    if name == "screenshot":
        return "taking a screenshot of the screen"
    if name == "list_processes":
        return "listing running processes"
    if name == "stop_process":
        return f"stopping process {_arg_str(args, 'pid') or _arg_str(args, 'name')}"
    if name == "git_run":
        return f"running git {_arg_str(args, 'arguments')}"
    if name == "scaffold_project":
        return f"scaffolding a {_arg_str(args, 'kind')} project in {_arg_str(args, 'path')}"
    if name in ("clipboard_get", "clipboard_set"):
        return "reading/writing the Windows clipboard"
    if name == "task_done":
        return "finalizing the mission"
    return f"executing {name}"


TOOL_ERROR_HINTS = {
    "search_files": (
        "search_files has TWO modes: to find files BY NAME omit 'query' and set glob_pattern "
        "(e.g. glob_pattern='*report*'); to search text INSIDE files you must pass 'query'."
    ),
    "fetch_url": "This URL is failing - do not retry it unchanged. Use web_search to find a working URL, or try a different page.",
    "web_search": "Change the search wording, try web_research, or fetch a known page directly with fetch_url.",
    "web_research": "Reword the topic, or fall back to web_search + fetch_url for individual pages.",
    "run_powershell": "Fix the command syntax (pass the raw PowerShell command - no nested 'powershell -Command').",
    "run_python": "Read the traceback, fix the code, and retry with corrected code.",
    "edit_file": "old_string must EXACTLY match text currently in the file - read_file first to get the exact text.",
    "stop_process": "Check the pid/name with list_processes first.",
}


def execute_tool_with_progress(console: "Console", name: str, args):
    """Execute a tool with a guaranteed ≤4s heartbeat of plain-English progress."""
    hb = Heartbeat(console, schedule=(3.0,) if not console.is_terminal else (15.0,))
    hb.start(f"tool {name}", describe_tool_activity(name, args))
    t0 = time.monotonic()
    try:
        result = execute_tool(name, args)
    finally:
        hb.stop()
    return result, time.monotonic() - t0


def apply_stuck_guards(console: "Console", name: str, args, result) -> None:
    """Permanently prevents stuck loops: repeated identical tool failures and
    blind same-URL refetches trigger corrective guidance injected into the
    conversation, so the model can never silently spin on the same mistake."""
    g = STATE.setdefault("stuck", {"sig": None, "run": 0, "url": "", "url_run": 0})
    result = str(result)
    if name == "task_done":
        return
    if result.startswith("ERROR"):
        sig = (name, result[:90])
        if sig == g["sig"]:
            g["run"] += 1
        else:
            g["sig"], g["run"] = sig, 1
        if g["run"] == 2:
            hint = TOOL_ERROR_HINTS.get(
                name, "Read the error text carefully, change the arguments, or pick a different tool."
            )
            console.print("[yellow](stuck-guard: repeated identical tool failure - injecting guidance)[/]")
            STATE["messages"].append(
                {
                    "role": "user",
                    "content": (
                        f"(system note: this exact {name} call has now failed twice in a row with the same "
                        f"error. {hint} Do NOT repeat the identical call - change something first.)"
                    ),
                }
            )
        elif g["run"] >= 4:
            console.print("[yellow](stuck-guard: 4+ identical failures - forcing a different strategy)[/]")
            STATE["messages"].append(
                {
                    "role": "user",
                    "content": (
                        "(system note: the same tool call has now failed 4+ times with the same error. STOP "
                        "using this tool for this purpose. Take a COMPLETELY different approach to reach the "
                        "goal, or continue with the information you already have.)"
                    ),
                }
            )
            g["sig"], g["run"] = None, 0
    else:
        g["sig"], g["run"] = None, 0
        if name == "fetch_url":
            u = ""
            try:
                u = str((args or {}).get("url", "")).strip()
            except Exception:
                u = ""
            if u:
                if u == g["url"]:
                    g["url_run"] += 1
                else:
                    g["url"], g["url_run"] = u, 1
                if g["url_run"] == 2:
                    console.print("[yellow](stuck-guard: same URL refetched - injecting guidance)[/]")
                    STATE["messages"].append(
                        {
                            "role": "user",
                            "content": (
                                "(system note: you have fetched this exact URL repeatedly and received the same "
                                "first section each time. The content is already in this conversation - USE it, "
                                "or fetch a DIFFERENT URL, or read a specific part of the page via run_python "
                                "with urllib.request, e.g. print(text[20000:40000]).)"
                            ),
                        }
                    )
                    g["url"], g["url_run"] = "", 0


def display_tool_call(console: Console, name: str, args, k: int = 0) -> None:
    try:
        arg_text = json.dumps(args, ensure_ascii=False, default=str) if isinstance(args, dict) else str(args)
    except Exception:
        arg_text = str(args)
    if len(arg_text) > TOOL_ARG_TRUNC:
        arg_text = arg_text[:TOOL_ARG_TRUNC] + " ...[truncated]"
    console.print(Panel(arg_text or "{}", title=f"TOOL CALL #{k} • {name} • {fmt_ts()}", border_style="cyan"))


def display_tool_result(console: Console, name: str, result: str, dur: float = 0.0) -> None:
    text = str(result)
    is_error = text.startswith("ERROR")
    if len(text) > TOOL_RESULT_TRUNC:
        text = text[:TOOL_RESULT_TRUNC] + " ...[truncated]"
    console.print(
        Panel(
            text,
            title=f"{name} RESULT • {dur:.1f}s • {'ERROR' if is_error else 'OK'}",
            border_style="red" if is_error else "green",
        )
    )


def print_banner(console: Console) -> None:
    text = (
        f"[bold]model[/]: {STATE['model']}\n"
        f"[bold]host [/]: {STATE['host']}\n"
        f"[bold]tools[/]: {len(TOOLS)} registered\n"
        f"[bold]cwd  [/]: {STATE['cwd']}\n"
        f"[bold]date [/]: {datetime.now():%Y-%m-%d %A}\n"
        f"[bold]mode [/]: auto-approve ALL - no confirmations"
    )
    console.print(Panel(text, title="ULTIMATE AI", subtitle="full-permission local agent", border_style="cyan"))


def print_tools(console: Console) -> None:
    table = Table(title=f"Tools ({len(TOOLS)})", border_style="cyan")
    table.add_column("tool", style="bold cyan", no_wrap=True)
    table.add_column("description")
    for name, tool in TOOLS.items():
        table.add_row(name, tool["description"])
    console.print(table)


def flush_optional_notices(console: Console) -> None:
    if not MISSING_OPTIONAL:
        return
    lines = [f"- [bold]{pkg}[/]: {note} (install: pip install {pkg})" for pkg, note in MISSING_OPTIONAL]
    console.print(Panel("\n".join(lines), title="Optional dependencies missing", border_style="yellow"))
    MISSING_OPTIONAL.clear()


# --- Persistence -----------------------------------------------------------------


def save_session() -> None:
    session_file = STATE.get("session_file")
    if not session_file:
        return
    try:
        session_file.write_text(
            json.dumps(
                {
                    "model": STATE.get("model"),
                    "saved_at": datetime.now().isoformat(timespec="seconds"),
                    "messages": STATE.get("messages", []),
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass  # crash-safe: persistence must never kill the loop


# --- Input history + line editor (Up/Down = prompts from ALL sessions) ----------


HISTORY_FILE = UAI_DIR / "history.log"


def load_history() -> list:
    """All prompts ever submitted, oldest first (one per line)."""
    try:
        if HISTORY_FILE.exists():
            return [
                line.rstrip("\n")
                for line in HISTORY_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
                if line.strip()
            ]
    except Exception:
        pass
    return []


def append_history(text: str) -> None:
    try:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(HISTORY_FILE, "a", encoding="utf-8") as fh:
            fh.write(text.replace("\n", " ").strip() + "\n")
    except Exception:
        pass


def _editor_redraw(prompt_str: str, buf: list, cursor: int) -> None:
    sys.stdout.write("\r\x1b[K" + prompt_str + "".join(buf))
    over = len(buf) - cursor
    if over > 0:
        sys.stdout.write("\x1b[" + str(over) + "D")
    sys.stdout.flush()


def prompt_with_history(prompt_str: str, history: list) -> str:
    """Interactive single-line input with Up/Down history navigation across ALL
    sessions (newest first; Down past the newest restores the draft). Requires
    a real console (msvcrt); callers fall back to console.input otherwise.
    Raises KeyboardInterrupt on Ctrl+C."""
    import msvcrt  # Windows-only, imported lazily

    sys.stdout.write(prompt_str)
    sys.stdout.flush()
    buf: list = []
    cur = 0
    hpos = len(history)
    draft: list = []

    def redraw():
        _editor_redraw(prompt_str, buf, cur)

    while True:
        ch = msvcrt.getwch()
        if ch in ("\r", "\n"):
            sys.stdout.write("\n")
            sys.stdout.flush()
            return "".join(buf)
        if ch == "\x03":  # Ctrl+C
            sys.stdout.write("\n")
            sys.stdout.flush()
            raise KeyboardInterrupt
        if ch == "\x08":  # Backspace
            if cur > 0:
                del buf[cur - 1]
                cur -= 1
                redraw()
            continue
        if ch in ("\x00", "\xe0"):  # Windows special-key prefix
            nxt = msvcrt.getwch()
            if nxt == "H":  # Up: older prompt
                if history and hpos > 0:
                    if hpos == len(history):
                        draft = list(buf)
                    hpos -= 1
                    buf = list(history[hpos])
                    cur = len(buf)
                    redraw()
            elif nxt == "P":  # Down: newer prompt / draft
                if hpos < len(history):
                    hpos += 1
                    buf = list(draft if hpos == len(history) else history[hpos])
                    cur = len(buf)
                    redraw()
            elif nxt == "K" and cur > 0:  # Left
                cur -= 1
                redraw()
            elif nxt == "M" and cur < len(buf):  # Right
                cur += 1
                redraw()
            elif nxt == "G":  # Home
                cur = 0
                redraw()
            elif nxt == "O":  # End
                cur = len(buf)
                redraw()
            elif nxt == "S" and cur < len(buf):  # Delete
                del buf[cur]
                redraw()
            continue
        if ch >= " ":
            buf.insert(cur, ch)
            cur += 1
            redraw()
        # other control characters are ignored


# --- Context compaction (infinite session memory) --------------------------------


def estimate_tokens(messages) -> int:
    return int(sum(len(str(m.get("content", ""))) for m in messages) / 3.5)


def compact_history(console: "Console", force: bool = False) -> bool:
    """Summarize older turns into a rolling context summary so the session can
    run forever inside a fixed context window. Returns True if compaction ran."""
    msgs = STATE["messages"]
    est = STATE.get("ctx_tokens") or estimate_tokens(msgs)
    if not force and est < 22000:
        return False
    if len(msgs) < 12:
        return False
    keep = 8
    cut = len(msgs) - keep
    # Atomic boundary: an assistant tool_call message and its tool results must
    # never be separated (causes API errors). Move the cut until it lands on a
    # non-tool message.
    while cut < len(msgs) and str(msgs[cut].get("role")) == "tool":
        cut += 1
    old = msgs[1:cut]
    if len(old) < 4:
        return False
    transcript = "\n".join(
        f"[{m.get('role')}]: {str(m.get('content', ''))[:400]}" for m in old
    )[:60000]
    console.print(f"[yellow](context compaction: ~{est} tokens - summarizing {len(old)} older messages...)[/]")
    hb = Heartbeat(console, schedule=(3.0,) if not console.is_terminal else (15.0,))
    hb.start("compacting session memory")
    try:
        payload = {
            "model": STATE["model"],
            "stream": False,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You compress agent conversation history into a dense CONTEXT SUMMARY. Preserve: "
                        "the user's goals and preferences, every file path created or modified, key "
                        "decisions, important results and numbers, and open threads. Under 800 words."
                    ),
                },
                {"role": "user", "content": "Summarize this agent conversation history:\n\n" + transcript},
            ],
        }
        with _httpx().Client(timeout=_httpx().Timeout(300.0, connect=10.0)) as client:
            resp = client.post(STATE["host"].rstrip("/") + "/api/chat", json=payload)
        resp.raise_for_status()
        summary = str(resp.json().get("message", {}).get("content", "")).strip()
    except Exception as exc:
        console.print(f"[red](compaction failed: {str(exc)[:140]} - continuing without compaction)[/]")
        return False
    finally:
        hb.stop()
    if not summary:
        return False
    system_msg = msgs[0]
    recent = msgs[cut:]
    STATE["messages"] = [
        system_msg,
        {
            "role": "user",
            "content": "(context summary of everything earlier in this session - treat as memory:\n" + summary + ")",
        },
        {"role": "assistant", "content": "Understood - I retain full context of our earlier work in this session."},
        *recent,
    ]
    save_session()
    console.print(
        f"[green](context compacted: {len(old)} messages -> rolling summary; next turns run with a clean window)[/]"
    )
    return True


# --- Agent loop --------------------------------------------------------------------


def run_agent_turn(user_input: str, args) -> None:
    console = STATE["console"]
    STATE["mission_text"] = user_input
    STATE["done_confirmed"] = False
    STATE["messages"].append({"role": "user", "content": user_input})
    save_session()
    tool_count = 0
    tool_errors = 0
    try:
        for _iteration in range(1, max(1, args.max_iters) + 1):
            if (STATE.get("ctx_tokens") or estimate_tokens(STATE["messages"])) > 22000:
                compact_history(console)
            console.print(
                f"[bold cyan]── STEP {_iteration}[/] [dim]• {fmt_ts()} • elapsed {fmt_elapsed()} • thinking + streaming…[/]"
            )
            chat_retries = 6
            while True:
                try:
                    content, tool_calls = chat_stream(console)
                    break
                except KeyboardInterrupt:
                    console.print("\n[yellow]Interrupted by user.[/]")
                    STATE["last_outcome"] = "interrupted"
                    return
                except ChatError as exc:
                    chat_retries -= 1
                    if chat_retries > 0:
                        console.print(f"[yellow](chat error, retrying: {str(exc)[:140]}...)[/]")
                        if "parsing tool call" in str(exc).lower() or "unexpected end of json" in str(exc).lower():
                            # The model emitted a malformed tool call. Blind re-sampling
                            # can repeat it - inject a corrective note so the retry
                            # knows what went wrong.
                            STATE["messages"].append(
                                {
                                    "role": "user",
                                    "content": (
                                        "(system note: your previous reply contained a MALFORMED tool call "
                                        "(broken JSON) and was rejected by the server. Re-emit the same action "
                                        "with valid JSON arguments - keep the call compact and prefer write_file "
                                        "for long content.)"
                                    ),
                                }
                            )
                        time.sleep(2.0)
                        continue
                    console.print(Panel(f"[red]Chat error: {exc}[/]", title="ERROR", border_style="red"))
                    STATE["last_outcome"] = "chat_error"
                    return
                except ConnectionError as exc:
                    conn_retries = STATE.get("conn_retries", 0)
                    if conn_retries < 8:
                        STATE["conn_retries"] = conn_retries + 1
                        wait = min(45.0, 5.0 * (conn_retries + 1))
                        console.print(f"[yellow](connection dropped - retrying in {wait:.0f}s: {str(exc)[:120]})[/]")
                        time.sleep(wait)
                        continue
                    console.print(Panel(f"[red]Connection failed: {exc}[/]", title="ERROR", border_style="red"))
                    STATE["last_outcome"] = "chat_error"
                    return
            STATE["conn_retries"] = 0

            assistant_msg = {"role": "assistant", "content": content}
            # Round-trip the model's own reasoning on tool-loop follow-ups
            # (gpt-oss accuracy degrades without it - scout finding).
            think_back = STATE.pop("last_thinking", None)
            if think_back:
                assistant_msg["thinking"] = think_back
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {"function": {"name": tc["name"], "arguments": tc["arguments"]}} for tc in tool_calls
                ]
            STATE["messages"].append(assistant_msg)
            save_session()

            if tool_calls:
                if content.strip():
                    console.print(Markdown(content))
                done = False
                done_summary = ""
                for call in tool_calls:
                    name = call["name"]
                    tool_count += 1
                    display_tool_call(console, name, call["arguments"], k=tool_count)
                    t_call = time.monotonic()
                    try:
                        result, dur = execute_tool_with_progress(console, name, call["arguments"])
                    except KeyboardInterrupt:
                        result = "ERROR: interrupted by user"
                        dur = time.monotonic() - t_call
                    if str(result).startswith("ERROR"):
                        tool_errors += 1
                    display_tool_result(console, name, result, dur=dur)
                    STATE["messages"].append({"role": "tool", "content": result, "name": name})
                    apply_stuck_guards(console, name, call["arguments"], result)
                    if name == "task_done" and not result.startswith("ERROR"):
                        if not STATE.get("done_confirmed"):
                            STATE["done_confirmed"] = True
                            console.print("[yellow](completion claimed - running the verification round before accepting)[/]")
                            STATE["messages"].append(
                                {
                                    "role": "user",
                                    "content": (
                                        "(verification round: re-read the mission and re-verify EVERY requirement "
                                        "against the actual work right now - run the exact commands, count the "
                                        "files, re-check every number and name. IMPORTANT: verify by INSPECTING "
                                        "(read files, count entries, check state) - do NOT re-run actions with "
                                        "side effects (running a script again can create duplicate outputs and "
                                        "break the mission's own invariants). Explicitly re-count anything the mission states as an EXACT "
                                        "number (exactly one folder, exactly N files) and delete duplicates before confirming. "
                                        "If you already produced the "
                                        "artifacts, inspect them as they are. If ANYTHING is missing or wrong, "
                                        "fix it and then call task_done again. If everything truly passes, call "
                                        "task_done again to confirm.)"
                                    ),
                                }
                            )
                        else:
                            done = True
                            done_summary = result
                save_session()
                if done:
                    STATE["last_outcome"] = "done"
                    console.print(Panel(str(done_summary)[:2000], title="TASK DONE", border_style="green"))
                    console.print(
                        f"[bold green]TASK CONFIRMED COMPLETE[/] [dim]• started {STATE['t_start_wall']:%H:%M:%S} • "
                        f"finished {fmt_ts()} • total {fmt_elapsed()} • steps {_iteration} • "
                        f"tool calls {tool_count} ({tool_errors} errors)[/]"
                    )
                    return
                continue

            fallback = parse_fallback_tool_call(content) if content else None
            if fallback:
                name, fb_args = fallback
                shown = strip_fallback_json(content)
                if shown:
                    console.print(Markdown(shown))
                tool_count += 1
                display_tool_call(console, name, fb_args, k=tool_count)
                t_call = time.monotonic()
                try:
                    result, dur = execute_tool_with_progress(console, name, fb_args)
                except KeyboardInterrupt:
                    result = "ERROR: interrupted by user"
                    dur = time.monotonic() - t_call
                if str(result).startswith("ERROR"):
                    tool_errors += 1
                display_tool_result(console, name, result, dur=dur)
                STATE["messages"].append({"role": "tool", "content": result, "name": name})
                apply_stuck_guards(console, name, fb_args, result)
                save_session()
                if name == "task_done" and not str(result).startswith("ERROR"):
                    if not STATE.get("done_confirmed"):
                        STATE["done_confirmed"] = True
                        console.print("[yellow](completion claimed - running the verification round before accepting)[/]")
                        STATE["messages"].append(
                            {
                                "role": "user",
                                "content": (
                                    "(verification round: re-read the mission and re-verify EVERY requirement "
                                    "against the actual work right now - run the exact commands, count the "
                                    "files, re-check every number and name. IMPORTANT: verify by INSPECTING "
                                    "(read files, count entries, check state) - do NOT re-run actions with "
                                    "side effects (running a script again can create duplicate outputs and "
                                    "break the mission's own invariants). Explicitly re-count anything the mission states as an EXACT "
                                        "number (exactly one folder, exactly N files) and delete duplicates before confirming. "
                                        "If you already produced the "
                                    "artifacts, inspect them as they are. If ANYTHING is missing or wrong, "
                                    "fix it and then call task_done again. If everything truly passes, call "
                                    "task_done again to confirm.)"
                                ),
                            }
                        )
                        continue
                    STATE["last_outcome"] = "done"
                    console.print(Panel(str(result)[:2000], title="TASK DONE", border_style="green"))
                    console.print(
                        f"[bold green]TASK CONFIRMED COMPLETE[/] [dim]• started {STATE['t_start_wall']:%H:%M:%S} • "
                        f"finished {fmt_ts()} • total {fmt_elapsed()} • steps {_iteration} • "
                        f"tool calls {tool_count} ({tool_errors} errors)[/]"
                    )
                    return
                continue

            if not content.strip():
                STATE["empty_streak"] = STATE.get("empty_streak", 0) + 1
                if STATE["empty_streak"] <= 3:
                    console.print("[yellow](empty response - nudging the agent to continue)[/]")
                    STATE["messages"].append(
                        {
                            "role": "user",
                            "content": "(system note: your last reply was empty. Continue the mission now with tool calls; do not stop until every requirement is met and verified.)",
                        }
                    )
                    continue
                resets = STATE.get("mission_resets", 0)
                if resets < 2:
                    STATE["mission_resets"] = resets + 1
                    STATE["empty_streak"] = 0
                    console.print(f"[yellow](agent went idle - restarting the conversation with the mission, attempt {resets + 2})[/]")
                    system_msg = STATE["messages"][0]
                    STATE["messages"] = [system_msg]
                    STATE["messages"].append(
                        {
                            "role": "user",
                            "content": STATE["mission_text"]
                            + "\n\n(system note: a previous attempt was interrupted and went idle. The target folders may already contain partial work - inspect what exists, then finish EVERY remaining requirement and verify.)",
                        }
                    )
                    save_session()
                    continue
                console.print("[dim](the model returned an empty response too many times)[/]")
                STATE["last_outcome"] = "empty"
                return
            STATE["empty_streak"] = 0
            # Plain-text answer without task_done: nudge once, then accept and end.
            console.print(Markdown(content))
            if not STATE.get("done_nudged"):
                STATE["done_nudged"] = True
                console.print("[yellow](no task_done call detected - nudging the agent to confirm completion)[/]")
                STATE["messages"].append(
                    {
                        "role": "user",
                        "content": "(system note: you replied in plain text without calling task_done. If the mission is fully complete and verified, call task_done now with a summary. Otherwise keep working with tools.)",
                    }
                )
                continue
            STATE["last_outcome"] = "answered"
            return
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user.[/]")
        STATE["last_outcome"] = "interrupted"
        return
    STATE["last_outcome"] = "max_iters"
    console.print(
        Panel(
            f"[bold red]MAX ITERATIONS[/] - the agent loop stopped after {args.max_iters} iterations without task_done.",
            border_style="red",
        )
    )


# --- CLI / REPL ----------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness.py",
        description="Ultimate AI - full-permission autonomous local agent harness for an Ollama model.",
    )
    parser.add_argument("--model", default=None, help="Ollama model tag, e.g. qwen3:8b")
    parser.add_argument("--host", default="http://127.0.0.1:11434", help="Ollama base URL")
    parser.add_argument(
        "--max-iters", dest="max_iters", type=int, default=120, help="Max agent iterations per goal (default 120)"
    )
    parser.add_argument("--num-ctx", dest="num_ctx", type=int, default=None, help="Ollama num_ctx context window override")
    parser.add_argument(
        "--yes",
        action="store_true",
        default=True,
        help="Auto-approve every tool call (this is already the default; accepted for explicitness)",
    )
    parser.add_argument("--system", default=None, help="Path to a file whose content replaces the built-in system prompt")
    parser.add_argument(
        "--mission",
        default=None,
        help="Mission text to run non-interactively (exits when done). Prefix with @ to read the text from a "
        "UTF-8 file, e.g. --mission @C:/path/mission.txt",
    )
    parser.add_argument("--self-test", dest="self_test", action="store_true", help="Run the offline self-test suite and exit")
    return parser


def run_self_test() -> int:
    """Pure offline self-test: no network, no Ollama. Prints SELF-TEST PASS / FAIL."""
    try:
        # 1) registry size and per-tool integrity
        assert len(TOOLS) >= 24, f"tool registry has {len(TOOLS)} tools, expected >= 24"
        for name, tool in TOOLS.items():
            assert isinstance(name, str) and name.strip(), "registry contains a tool with an empty name"
            assert isinstance(tool.get("description"), str) and tool["description"].strip(), (
                f"tool {name}: description must be non-empty"
            )
            params = tool.get("parameters")
            assert isinstance(params, dict) and len(params) > 0, (
                f"tool {name}: parameters must be a non-empty JSON-schema dict"
            )
            assert callable(tool.get("fn")), f"tool {name}: missing callable handler"
            try:
                json.dumps(params)
            except (TypeError, ValueError):
                raise AssertionError(f"tool {name}: parameters are not JSON-serializable")

        # 2) JSON fallback parser: fenced block and bare object
        fenced_sample = (
            "I will read that file now.\n"
            '```json {"tool": "read_file", "args": {"path": "C:/x"}}```\n'
        )
        parsed_fenced = parse_fallback_tool_call(fenced_sample)
        assert parsed_fenced == ("read_file", {"path": "C:/x"}), (
            f"fenced fallback parse failed: {parsed_fenced!r}"
        )
        bare_sample = '{"tool": "read_file", "args": {"path": "C:/x"}}'
        parsed_bare = parse_fallback_tool_call(bare_sample)
        assert parsed_bare == ("read_file", {"path": "C:/x"}), f"bare fallback parse failed: {parsed_bare!r}"

        # 3) streamed native tool_call assembler: partial name + accumulated args
        chunks = [
            {"message": {"content": "", "tool_calls": [{"function": {"name": "read_"}}]}},
            {"message": {"tool_calls": [{"index": 0, "function": {"name": "file", "arguments": '{"pa'}}]}},
            {"message": {"tool_calls": [{"index": 0, "function": {"arguments": 'th": "C:/x"}'}}]}},
        ]
        assembler = ToolCallAssembler()
        for chunk in chunks:
            assembler.feed(chunk["message"])
        calls = assembler.finalize()
        assert len(calls) == 1, f"assembler produced {len(calls)} calls, expected 1"
        assert calls[0]["name"] == "read_file", f"assembler merged name wrong: {calls[0]['name']!r}"
        assert calls[0]["arguments"] == {"path": "C:/x"}, (
            f"assembler merged arguments wrong: {calls[0]['arguments']!r}"
        )

        # 4) system prompt lists (almost) every tool and carries the verification protocol
        prompt = build_system_prompt()
        present = sum(1 for n in TOOLS if n in prompt)
        assert present >= 20, f"system prompt lists only {present} tool names, expected >= 20"
        assert "VERIFICATION PROTOCOL" in prompt, "system prompt missing VERIFICATION PROTOCOL block"

        print("SELF-TEST PASS")
        return 0
    except AssertionError as exc:
        print(f"SELF-TEST FAIL: {exc}")
        return 1


def main(argv=None) -> int:
    # Force robust UTF-8 output on Windows consoles / pipes.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.self_test:
        return run_self_test()

    console = Console()
    STATE["console"] = console
    if not args.model:
        parser.error("--model is required (or run --self-test)")

    if args.system:
        try:
            system_prompt = build_system_prompt(args.system)
        except OSError as exc:
            console.print(f"[red]Could not read --system file: {exc} - using the built-in prompt.[/]")
            system_prompt = build_system_prompt()
    else:
        system_prompt = build_system_prompt()

    STATE.update(model=args.model, host=args.host, num_ctx=args.num_ctx, done=False)
    STATE["messages"] = [{"role": "system", "content": system_prompt}]

    session_file = SESSIONS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}.json"
    try:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        session_file.touch(exist_ok=True)
        STATE["session_file"] = session_file
    except Exception:
        STATE["session_file"] = None

    STATE["t_start_wall"] = datetime.now()
    STATE["t_start_mono"] = time.monotonic()

    print_banner(console)
    flush_optional_notices(console)

    if args.mission:
        mission_text = args.mission
        if mission_text.startswith("@"):
            mission_path = Path(mission_text[1:])
            try:
                mission_text = mission_path.read_text(encoding="utf-8")
            except OSError as exc:
                console.print(f"[red]Could not read --mission file {mission_path}: {exc}[/]")
                return 1
        if not mission_text.strip():
            console.print("[red]--mission text is empty.[/]")
            return 1
        STATE["last_outcome"] = None
        try:
            run_agent_turn(mission_text, args)
        except SystemExit:
            pass  # CONNECT FAILED path already recorded last_outcome
        outcome = STATE.get("last_outcome")
        if outcome == "max_iters":
            return 3
        if outcome == "chat_error":
            return 1
        if outcome in ("interrupted", "empty", "answered"):
            console.print("[yellow]mission ended without task_done confirmation[/]")
            return 4
        return 0

    interactive = bool(sys.stdin.isatty() and sys.stdout.isatty())
    if interactive:
        try:
            import msvcrt  # noqa: F401 - enables the Up/Down history editor
            os.system("")  # enable ANSI/VT escape sequences in classic conhost
        except ImportError:
            interactive = False
    console.print(
        "[dim]Type your goal and press Enter. Commands: /help /new /sessions /history /stats /research "
        "/compact /model /tools /save /reset /cwd /exit  |  Up/Down = prompt history from ALL sessions[/]"
    )
    save_session()
    history = load_history()

    while True:
        try:
            if interactive:
                user_input = prompt_with_history("\u276f ", history)
            else:
                user_input = console.input("[bold magenta]\u276f [/bold magenta]")
        except EOFError:
            break
        except KeyboardInterrupt:
            console.print()
            continue
        user_input = user_input.strip()
        if not user_input:
            continue

        low = user_input.lower()
        if low in ("/exit", "/quit"):
            break

        if low == "/help":
            console.print(
                Panel(
                    "[bold]/new[/] start a fresh session · [bold]/sessions[/] list saved sessions · "
                    "[bold]/sessions <n>[/] load session n · [bold]/history[/] recent prompts (all sessions)\n"
                    "[bold]/stats[/] model + machine + session stats · [bold]/research <topic>[/] headless web "
                    "research · [bold]/compact[/] compress session memory now\n"
                    "[bold]/model <tag>[/] switch model · [bold]/tools[/] tool list · [bold]/save[/] save session · "
                    "[bold]/reset[/] or [bold]/clear[/] clear conversation · [bold]/cwd <dir>[/] set working "
                    "directory · [bold]/exit[/] or [bold]/quit[/] leave\n"
                    "[dim]Up/Down arrows recall prompts from all past sessions, newest first.[/]",
                    title="COMMANDS",
                    border_style="cyan",
                )
            )
            continue

        if low in ("/reset", "/clear"):
            STATE["messages"] = [STATE["messages"][0]]
            STATE["done_confirmed"] = False
            save_session()
            console.print("[yellow]Conversation reset (system prompt kept).[/]")
            continue

        if low == "/new":
            STATE["messages"] = [STATE["messages"][0]]
            STATE["done_confirmed"] = False
            STATE["session_file"] = UAI_DIR / "sessions" / (datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")
            STATE["session_file"].parent.mkdir(parents=True, exist_ok=True)
            save_session()
            console.print(f"[green]New session started:[/] {STATE['session_file'].name}")
            continue

        if low == "/sessions":
            files = sorted(UAI_DIR.glob("sessions/*.json"))
            if not files:
                console.print("[yellow]No saved sessions yet.[/]")
                continue
            for i, f in enumerate(files, 1):
                try:
                    data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
                    n = len(data.get("messages", []))
                    first = next(
                        (str(m.get("content", ""))[:70] for m in data.get("messages", []) if m.get("role") == "user"),
                        "",
                    )
                except Exception:
                    n, first = 0, "(unreadable)"
                mark = " [green](loaded)[/]" if f == STATE.get("session_file") else ""
                console.print(f"  {i:>2}. {f.name}  ·  {n} msgs  ·  {first}{mark}")
            console.print("[dim]Load one with /sessions <number>[/]")
            continue

        if low.startswith("/sessions"):
            parts = user_input.split(maxsplit=1)
            files = sorted(UAI_DIR.glob("sessions/*.json"))
            try:
                pick = files[int(parts[1]) - 1]
            except (IndexError, ValueError, OSError):
                console.print("[yellow]usage: /sessions <number> (see /sessions)[/]")
                continue
            try:
                data = json.loads(pick.read_text(encoding="utf-8", errors="replace"))
                msgs = data.get("messages", [])
                if msgs and msgs[0].get("role") != "system":
                    msgs = [{"role": "system", "content": build_system_prompt()}, *msgs]
                STATE["messages"] = msgs
                STATE["session_file"] = pick
                console.print(f"[green]Loaded {pick.name} ({len(msgs)} messages).[/]")
            except Exception as exc:
                console.print(f"[red]Could not load session: {exc}[/]")
            continue

        if low == "/history":
            for i, h in enumerate(history[-25:], 1):
                console.print(f"  {i:>3}. {h[:110]}")
            continue

        if low == "/stats":
            hw = HW_GROUND_TRUTH or "(not provided - set by the launcher)"
            console.print(
                Panel(
                    f"[bold]model[/]: {STATE['model']}\n"
                    f"[bold]host [/]: {STATE['host']}  ·  context window: {STATE.get('num_ctx') or 'model default'}\n"
                    f"[bold]ctx tokens used (last request)[/]: {STATE.get('ctx_tokens') or 'n/a'}\n"
                    f"[bold]messages in session[/]: {len(STATE['messages'])}\n"
                    f"[bold]cwd[/]: {STATE['cwd']}\n"
                    f"[bold]machine (ground truth)[/]: {hw}",
                    title="SESSION STATS",
                    border_style="cyan",
                )
            )
            continue

        if low.startswith("/research"):
            topic = user_input.split(maxsplit=1)[1].strip() if len(user_input.split(maxsplit=1)) > 1 else ""
            if not topic:
                console.print("[yellow]usage: /research <topic>[/]")
                continue
            result, dur = execute_tool_with_progress(console, "web_research", {"query": topic})
            console.print(Markdown(result))
            continue

        if low == "/compact":
            if compact_history(console, force=True):
                console.print("[green]Session memory compacted.[/]")
            else:
                console.print("[yellow]Nothing to compact yet (session still small).[/]")
            continue

        if low.startswith("/cwd"):
            parts = user_input.split(maxsplit=1)
            if len(parts) == 2 and Path(parts[1].strip()).is_dir():
                STATE["cwd"] = str(Path(parts[1].strip()).resolve())
                console.print(f"[green]Working directory set to {STATE['cwd']}[/]")
            else:
                console.print(f"[yellow]Current cwd: {STATE['cwd']}  (usage: /cwd <directory>)[/]")
            continue

        if low == "/save":
            save_session()
            target = STATE.get("session_file") or "(no session file)"
            console.print(f"[green]Session saved:[/] {target}")
            continue
        if user_input == "/tools":
            print_tools(console)
            continue
        if user_input.startswith("/model"):
            parts = user_input.split(maxsplit=1)
            if len(parts) == 2 and parts[1].strip():
                STATE["model"] = parts[1].strip()
                console.print(f"[green]Model set to {STATE['model']}.[/]")
            else:
                console.print(f"[yellow]Current model: {STATE['model']} (usage: /model <tag>)[/]")
            continue

        append_history(user_input)
        history.append(user_input)
        try:
            run_agent_turn(user_input, args)
        except SystemExit:
            raise
        except Exception as exc:
            console.print(Panel(f"[red]Harness error: {exc}[/]", border_style="red"))

    save_session()
    console.print("[bold cyan]Goodbye.[/]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
