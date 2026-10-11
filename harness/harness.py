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
import time
import urllib.parse
import webbrowser
from datetime import datetime
from pathlib import Path

# --- Core dependencies (critical path) -------------------------------------
import httpx
import psutil
import pyperclip
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

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

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) UltimateAI/1.0"

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


def _run_captured(cmd, timeout_s: float, shell: bool = False) -> str:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_s,
            cwd=STATE["cwd"],
            shell=shell,
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout_s:g}s"
    except FileNotFoundError as exc:
        return f"ERROR: executable not found: {exc}"
    except OSError as exc:
        return f"ERROR: {exc}"
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return (out or "(no output)") + f"\n[exit code: {proc.returncode}]"


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
        return _run_captured(["cmd", "/c", command], _as_timeout(args.get("timeout_s")))
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
        return _run_captured([sys.executable, str(tmp_path)], timeout_s)
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
    "Grep-like case-insensitive text search across files under a root directory (recursive). Returns path:line: text matches.",
    {
        "type": "object",
        "properties": {
            "root": {"type": "string", "description": "Root directory to search (default: session cwd)"},
            "query": {"type": "string", "description": "Text to search for (case-insensitive)"},
            "max_results": {"type": "integer", "description": "Maximum matches (default 100)"},
            "glob_pattern": {"type": "string", "description": "Only scan files matching this glob (default *)"},
        },
        "required": ["query"],
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
        if not query:
            return "ERROR: query required"
        if not root.exists():
            return f"ERROR: root not found: {root}"
        needle = query.lower()
        results = []
        stopped = ""
        try:
            for path in root.rglob(glob_pattern):
                if len(results) >= max_results:
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


def _ddg_html_fallback(query: str, max_results: int) -> str:
    resp = httpx.get(
        "https://html.duckduckgo.com/html/",
        params={"q": query},
        headers={"User-Agent": USER_AGENT},
        timeout=20.0,
        follow_redirects=True,
    )
    resp.raise_for_status()
    page = resp.text
    entries = []
    if BeautifulSoup is not None:
        soup = BeautifulSoup(page, "html.parser")
        for a in soup.select("a.result__a")[:max_results]:
            entries.append((a.get_text(" ", strip=True), a.get("href", "")))
    if not entries:
        for m in re.finditer(
            r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            page,
            re.DOTALL,
        ):
            title = html_lib.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
            entries.append((title, m.group(1)))
            if len(entries) >= max_results:
                break
    if not entries:
        return ""
    lines = []
    for i, (title, href) in enumerate(entries[:max_results], 1):
        lines.append(f"{i}. {title}\n   {_clean_ddg_url(href)}")
    return "\n".join(lines)


@register_tool(
    "web_search",
    "Search the web (DuckDuckGo). Uses the ddgs package when installed, otherwise an HTTP+HTML fallback. "
    "Returns numbered title/URL/snippet results.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "max_results": {"type": "integer", "description": "Maximum results (default 8)"},
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
        if DDGS is not None:
            try:
                with DDGS() as ddgs:
                    found = list(ddgs.text(query, max_results=max_results))
                lines = []
                for i, r in enumerate(found[:max_results], 1):
                    title = r.get("title") or ""
                    url = r.get("href") or r.get("url") or r.get("link") or ""
                    snippet = r.get("body") or r.get("snippet") or r.get("description") or ""
                    lines.append(f"{i}. {title}\n   {url}\n   {snippet[:300]}")
                if lines:
                    return f"Web results for '{query}':\n" + "\n".join(lines)
            except Exception:
                pass  # fall through to the HTML fallback
        fallback = _ddg_html_fallback(query, max_results)
        if fallback:
            return f"Web results for '{query}':\n" + fallback
        return "ERROR: web_search returned no results (install ddgs for reliability: pip install ddgs)"
    except Exception as exc:
        return f"ERROR: web_search failed: {exc}"


@register_tool(
    "fetch_url",
    "Download a URL over HTTP(S) with httpx (TLS enabled) and return its text content; HTML pages are stripped to readable text.",
    {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "max_bytes": {"type": "integer", "description": "Maximum bytes to download (default 400000)"},
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
            max_bytes = max(1000, int(args.get("max_bytes", 400000) or 400000))
        except (TypeError, ValueError):
            max_bytes = 400000
        with httpx.Client(timeout=30.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
            resp = client.get(url)
        resp.raise_for_status()
        raw = resp.content[:max_bytes]
        ctype = resp.headers.get("content-type", "")
        text = raw.decode(resp.encoding or "utf-8", errors="replace")
        looks_html = (
            "html" in ctype.lower()
            or text.lstrip()[:15].lower().startswith("<!doctype")
            or "<html" in text[:500].lower()
        )
        if looks_html:
            if BeautifulSoup is not None:
                soup = BeautifulSoup(text, "html.parser")
                for tag in soup(["script", "style", "noscript", "svg"]):
                    tag.decompose()
                text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n"))
            else:
                text = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", text, flags=re.DOTALL | re.IGNORECASE)
                text = html_lib.unescape(re.sub(r"<[^>]+>", " ", text))
                text = re.sub(r"[ \t]{2,}", " ", text)
                text = re.sub(r"\n{3,}", "\n\n", text)
        cap = 24000
        note = ""
        if len(text) > cap:
            text = text[:cap]
            note = "\n[... text truncated to keep the context manageable ...]"
        size_note = f"\n[download truncated at max_bytes={max_bytes}]" if len(resp.content) > max_bytes else ""
        return f"URL: {url} (HTTP {resp.status_code}, {ctype or 'unknown type'})\n\n{text}{note}{size_note}"
    except Exception as exc:
        return f"ERROR: fetch_url failed: {exc}"


@register_tool(
    "open_in_browser",
    "Open a URL in the user's default web browser.",
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
        for proc in psutil.process_iter(["pid", "name", "memory_info"]):
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
                proc = psutil.Process(int(target))
                pname = proc.name()
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except psutil.TimeoutExpired:
                    proc.kill()
                return f"Stopped process {pname} (pid {target})."
            except psutil.NoSuchProcess:
                return f"ERROR: no process with pid {target}"
        victims = []
        for proc in psutil.process_iter(["pid", "name"]):
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
            text = pyperclip.paste()
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
            pyperclip.copy(text)
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
    summary = str(args.get("summary", "") or "done")
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

    Returns (content, tool_calls). Content deltas are rendered live via a
    transient rich Live preview; tool_call fragments are merged by the
    ToolCallAssembler.
    """
    payload = {
        "model": STATE["model"],
        "messages": STATE["messages"],
        "stream": True,
        "tools": ollama_tools_schema(),
    }
    if STATE.get("num_ctx"):
        payload["options"] = {"num_ctx": STATE["num_ctx"]}
    url = STATE["host"].rstrip("/") + "/api/chat"
    assembler = ToolCallAssembler()
    parts = []
    last_render = 0.0
    try:
        with httpx.Client(timeout=httpx.Timeout(600.0, connect=10.0)) as client:
            with client.stream("POST", url, json=payload) as resp:
                if resp.status_code != 200:
                    detail = resp.read().decode("utf-8", errors="replace")[:400]
                    raise ChatError(f"HTTP {resp.status_code} from {url}: {detail}")
                with Live(console=console, refresh_per_second=4, transient=True) as live:
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
                        delta = message.get("content") or ""
                        if delta:
                            parts.append(delta)
                            now = time.monotonic()
                            if now - last_render >= 0.3:
                                live.update(Markdown("".join(parts)[-6000:]))
                                last_render = now
                        if message.get("tool_calls"):
                            assembler.feed(message)
                        if obj.get("done"):
                            break
                    live.update(Markdown("".join(parts)[-6000:]))
    except httpx.ConnectError as exc:
        raise ConnectionError(f"cannot reach Ollama at {STATE['host']} ({exc})") from exc
    except httpx.ConnectTimeout as exc:
        raise ConnectionError(f"connection to Ollama at {STATE['host']} timed out ({exc})") from exc
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


def display_tool_call(console: Console, name: str, args) -> None:
    try:
        arg_text = json.dumps(args, ensure_ascii=False, default=str) if isinstance(args, dict) else str(args)
    except Exception:
        arg_text = str(args)
    if len(arg_text) > TOOL_ARG_TRUNC:
        arg_text = arg_text[:TOOL_ARG_TRUNC] + " ...[truncated]"
    console.print(Panel(arg_text or "{}", title=f"TOOL CALL - {name}", border_style="cyan"))


def display_tool_result(console: Console, name: str, result: str) -> None:
    text = str(result)
    is_error = text.startswith("ERROR")
    if len(text) > TOOL_RESULT_TRUNC:
        text = text[:TOOL_RESULT_TRUNC] + " ...[truncated]"
    console.print(
        Panel(text, title=f"{name} - {'ERROR' if is_error else 'RESULT'}", border_style="red" if is_error else "green")
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


# --- Agent loop --------------------------------------------------------------------


def run_agent_turn(user_input: str, args) -> None:
    console = STATE["console"]
    STATE["messages"].append({"role": "user", "content": user_input})
    save_session()
    try:
        for _iteration in range(1, max(1, args.max_iters) + 1):
            try:
                content, tool_calls = chat_stream(console)
            except KeyboardInterrupt:
                console.print("\n[yellow]Interrupted by user.[/]")
                return
            except ConnectionError as exc:
                console.print(
                    Panel(
                        "[bold red]Ollama is not reachable.[/]\n"
                        f"Host: {STATE['host']}\n\n"
                        "Start the Ollama server first, e.g.:\n"
                        "    ollama serve\n"
                        "or make sure the Ollama app is running, then re-run the harness.",
                        title="CONNECT FAILED",
                        border_style="red",
                    )
                )
                save_session()
                raise SystemExit(2) from exc
            except ChatError as exc:
                console.print(Panel(f"[red]Chat error: {exc}[/]", title="ERROR", border_style="red"))
                return

            assistant_msg = {"role": "assistant", "content": content}
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
                    display_tool_call(console, name, call["arguments"])
                    try:
                        result = execute_tool(name, call["arguments"])
                    except KeyboardInterrupt:
                        result = "ERROR: interrupted by user"
                    display_tool_result(console, name, result)
                    STATE["messages"].append({"role": "tool", "content": result, "name": name})
                    if name == "task_done":
                        done = True
                        done_summary = result
                save_session()
                if done:
                    console.print(Panel(str(done_summary)[:2000], title="TASK DONE", border_style="green"))
                    return
                continue

            fallback = parse_fallback_tool_call(content) if content else None
            if fallback:
                name, fb_args = fallback
                shown = strip_fallback_json(content)
                if shown:
                    console.print(Markdown(shown))
                display_tool_call(console, name, fb_args)
                try:
                    result = execute_tool(name, fb_args)
                except KeyboardInterrupt:
                    result = "ERROR: interrupted by user"
                display_tool_result(console, name, result)
                STATE["messages"].append({"role": "tool", "content": result, "name": name})
                save_session()
                if name == "task_done":
                    console.print(Panel(str(result)[:2000], title="TASK DONE", border_style="green"))
                    return
                continue

            if content.strip():
                console.print(Markdown(content))
            else:
                console.print("[dim](the model returned an empty response)[/]")
            return
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user.[/]")
        return
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
        "--max-iters", dest="max_iters", type=int, default=80, help="Max agent iterations per goal (default 80)"
    )
    parser.add_argument("--num-ctx", dest="num_ctx", type=int, default=None, help="Ollama num_ctx context window override")
    parser.add_argument(
        "--yes",
        action="store_true",
        default=True,
        help="Auto-approve every tool call (this is already the default; accepted for explicitness)",
    )
    parser.add_argument("--system", default=None, help="Path to a file whose content replaces the built-in system prompt")
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

        # 4) system prompt lists (almost) every tool
        prompt = build_system_prompt()
        present = sum(1 for n in TOOLS if n in prompt)
        assert present >= 20, f"system prompt lists only {present} tool names, expected >= 20"

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

    print_banner(console)
    flush_optional_notices(console)
    console.print("[dim]Type your goal and press Enter. Commands: /tools  /model <tag>  /reset  /save  /exit[/]")
    save_session()

    while True:
        try:
            user_input = console.input("[bold magenta]\u276f [/bold magenta]")
        except EOFError:
            break
        except KeyboardInterrupt:
            console.print()
            continue
        user_input = user_input.strip()
        if not user_input:
            continue

        if user_input in ("/exit", "/quit"):
            break
        if user_input == "/reset":
            STATE["messages"] = [STATE["messages"][0]]
            save_session()
            console.print("[yellow]Conversation reset (system prompt kept).[/]")
            continue
        if user_input == "/save":
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
