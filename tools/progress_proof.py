#!/usr/bin/env python3
"""T-B + T-C proof driver: real-console interactive evidence for the
scroll-stable progress engine.

What it does (fully automated, evidence to missions/logs/proof-<ts>/):
  1. Spawns harness.py in its OWN console window (CREATE_NEW_CONSOLE) so the
     child is a genuine interactive terminal session.
  2. Types a long-task prompt into that console with WriteConsoleInput on
     CONIN$ (identical to real keyboard input for msvcrt.getwch).
  3. T-B: watches the child's run id in ~/.ultimate-ai/progress-audit.jsonl
     for 30s and proves max gap < 5s (progress continued the whole time).
  4. T-C: attaches to the child console, records the viewport rect, scrolls
     UP 40 lines (WM_VSCROLL SB_LINEUP), waits 30s while the model keeps
     working, re-reads the rect -> must be IDENTICAL (view stayed put) while
     >= 12 new audit ticks arrived (progress continued). Screenshots taken.
  5. Restores the viewport, sends /exit, waits for clean exit.

Exit 0 iff every assertion holds. Evidence bundle: rect0.txt/rect1.txt,
audit-analysis.txt, screenshots, console-title samples, driver log.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT = Path.home() / ".ultimate-ai" / "progress-audit.jsonl"
PROMPT = (
    "Write a 3500-word essay about the history of Rome, from the founding "
    "to the fall of Constantinople, with section headings. Take your time "
    "and be thorough."
)

# ---- Win32 plumbing -----------------------------------------------------------

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

WM_VSCROLL = 0x0115
SB_LINEUP = 0
SB_BOTTOM = 7


class KEY_EVENT_RECORD(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("UnicodeChar", wintypes.WCHAR), ("AsciiChar", ctypes.c_char)]

    _fields_ = [
        ("bKeyDown", wintypes.BOOL),
        ("wRepeatCount", wintypes.WORD),
        ("wVirtualKeyCode", wintypes.WORD),
        ("wVirtualScanCode", wintypes.WORD),
        ("uChar", _U),
        ("dwControlKeyState", wintypes.DWORD),
    ]


class INPUT_RECORD(ctypes.Structure):
    class _E(ctypes.Union):
        _fields_ = [("KeyEvent", KEY_EVENT_RECORD)]

    _fields_ = [("EventType", wintypes.WORD), ("Event", _E)]


GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_RW = 0x3  # FILE_SHARE_READ | FILE_SHARE_WRITE
OPEN_EXISTING = 3


def open_conin():
    return kernel32.CreateFileW(
        "CONIN$", GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_RW, None, OPEN_EXISTING, 0, None,
    )


def type_text(text: str) -> None:
    """Type text + Enter into the attached console's input buffer."""
    chars = list(text) + ["\r"]
    records = (INPUT_RECORD * len(chars))()
    for i, ch in enumerate(chars):
        rec = records[i]
        rec.EventType = 1  # KEY_EVENT
        rec.Event.KeyEvent.bKeyDown = True
        rec.Event.KeyEvent.wRepeatCount = 1
        rec.Event.KeyEvent.wVirtualKeyCode = 0x0D if ch == "\r" else ord(ch.upper()) & 0xFF
        rec.Event.KeyEvent.uChar.UnicodeChar = ch
    written = wintypes.DWORD(0)
    ok = kernel32.WriteConsoleInputW(open_conin(), records, len(chars), ctypes.byref(written))
    if not ok or written.value != len(chars):
        raise RuntimeError(f"WriteConsoleInput failed ok={ok} written={written.value}/{len(chars)}")


class _COORD(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]


class _SMALL_RECT(ctypes.Structure):
    _fields_ = [("Left", wintypes.SHORT), ("Top", wintypes.SHORT),
                ("Right", wintypes.SHORT), ("Bottom", wintypes.SHORT)]


class _CSBI(ctypes.Structure):
    _fields_ = [("dwSize", _COORD), ("dwCursorPosition", _COORD),
                ("wAttributes", wintypes.WORD), ("srWindow", _SMALL_RECT),
                ("dwMaximumWindowSize", _COORD)]


def open_conout():
    return kernel32.CreateFileW(
        "CONOUT$", GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_RW, None, OPEN_EXISTING, 0, None,
    )


def read_viewport():
    info = _CSBI()
    ok = kernel32.GetConsoleScreenBufferInfo(open_conout(), ctypes.byref(info))
    if not ok:
        raise RuntimeError(f"GetConsoleScreenBufferInfo failed err={ctypes.get_last_error()}")
    return dict(Top=info.srWindow.Top, Bottom=info.srWindow.Bottom,
                Left=info.srWindow.Left, Right=info.srWindow.Right,
                CursorY=info.dwCursorPosition.Y, BufH=info.dwSize.Y)


def read_title(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def screenshot(path: Path) -> None:
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
        "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen;"
        "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
        "$g=[System.Drawing.Graphics]::FromImage($bmp);"
        "$g.CopyFromScreen($b.Left,$b.Top,0,0,$bmp.Size);"
        f"$bmp.Save('{path}');$g.Dispose();$bmp.Dispose()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=60)


def audit_runs() -> dict:
    runs = {}
    try:
        for line in AUDIT.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            runs.setdefault(str(rec.get("run", "?")), []).append(rec)
    except OSError:
        pass
    return runs


def gap_stats(recs, t_from=None, t_to=None) -> dict:
    ts = sorted(float(r["ts"]) for r in recs
                if isinstance(r.get("ts"), (int, float))
                and (t_from is None or r["ts"] >= t_from)
                and (t_to is None or r["ts"] <= t_to))
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    return dict(count=len(ts), max_gap=max(gaps) if gaps else 0.0,
                first=ts[0] if ts else None, last=ts[-1] if ts else None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=30, help="per-phase observation window")
    args = ap.parse_args()

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = ROOT / "missions" / "logs" / f"proof-{ts}"
    out.mkdir(parents=True, exist_ok=True)
    log = open(out / "driver.log", "w", encoding="utf-8")

    def say(msg):
        print(msg)
        log.write(f"{datetime.now():%H:%M:%S} {msg}\n")
        log.flush()

    known_runs = set(audit_runs().keys())
    say(f"baseline audit runs: {len(known_runs)}")

    child = subprocess.Popen(
        [sys.executable, str(ROOT / "harness" / "harness.py"),
         "--model", "gpt-oss:20b", "--num-ctx", "32768", "--yes"],
        cwd=str(ROOT), creationflags=0x00000010,  # CREATE_NEW_CONSOLE
    )
    say(f"spawned child pid={child.pid}")
    time.sleep(6)  # banner + prompt

    # Attach to the child's console: type input + read its screen buffer.
    kernel32.FreeConsole()
    if not kernel32.AttachConsole(child.pid):
        say(f"FATAL: AttachConsole failed err={ctypes.get_last_error()}")
        child.kill()
        return 2
    hwnd = kernel32.GetConsoleWindow()
    say(f"attached; console hwnd={hwnd}, title='{read_title(hwnd)}'")
    user32.SetForegroundWindow(hwnd)

    type_text(PROMPT)
    say("prompt typed; waiting for the child's audit run id")
    t_prompt = time.time()

    child_run = None
    for _ in range(40):
        time.sleep(1.0)
        new = set(audit_runs().keys()) - known_runs
        if new:
            child_run = sorted(new)[0]
            break
    if child_run is None:
        say("FATAL: no new audit run appeared (child never started working)")
        type_text("/exit")
        child.wait(timeout=30)
        return 2
    say(f"child audit run: {child_run}")

    # ---- T-B: 30s progress window -----------------------------------------
    tb_start, tb_end = time.time(), time.time() + args.seconds
    title_samples = []
    while time.time() < tb_end:
        title_samples.append(read_title(hwnd))
        time.sleep(2.0)
    recs = audit_runs().get(child_run, [])
    stats = gap_stats(recs, tb_start - 1, tb_end)
    distinct_titles = len({t for t in title_samples if t.strip()})
    say(f"T-B: {stats['count']} ticks in {args.seconds}s, max gap {stats['max_gap']:.2f}s, {distinct_titles} distinct titles")
    screenshot(out / "tb-midwork.png")

    # ---- T-C: scroll proof ---------------------------------------------------
    r_before = read_viewport()
    say(f"viewport before scroll: {r_before}")
    for _ in range(40):
        user32.PostMessageW(hwnd, WM_VSCROLL, SB_LINEUP, 0)
        time.sleep(0.02)
    time.sleep(1.0)
    r0 = read_viewport()
    (out / "rect0.txt").write_text(json.dumps(r0, indent=1), encoding="utf-8")
    say(f"scrolled up: {r0} (delta Top={r0['Top'] - r_before['Top']})")
    if r0["Top"] >= r_before["Top"]:
        say("WARN: WM_VSCROLL did not move the viewport; trying WM_MOUSEWHEEL")
        for _ in range(30):
            user32.PostMessageW(hwnd, 0x020A, 120 << 16, 0)
            time.sleep(0.03)
        time.sleep(1.0)
        r0 = read_viewport()
        (out / "rect0.txt").write_text(json.dumps(r0, indent=1), encoding="utf-8")
        say(f"after mousewheel: {r0}")

    tc_start = time.time()
    titles_during = [read_title(hwnd)]
    while time.time() - tc_start < args.seconds:
        time.sleep(2.0)
        titles_during.append(read_title(hwnd))
    ticks_before = gap_stats(recs, 0, tc_start)["count"]
    recs2 = audit_runs().get(child_run, [])
    tc_stats = gap_stats(recs2, tc_start - 1, None)
    r1 = read_viewport()
    (out / "rect1.txt").write_text(json.dumps(r1, indent=1), encoding="utf-8")
    say(f"viewport after {args.seconds}s of work: {r1}")
    screenshot(out / "tc-scrolled-still.png")
    distinct_during = len({t for t in titles_during if t.strip()})
    say(f"T-C: {tc_stats['count']} new ticks while scrolled, {distinct_during} distinct titles")

    # restore viewport to bottom and end the session
    user32.PostMessageW(hwnd, WM_VSCROLL, SB_BOTTOM, 0)
    time.sleep(1.0)
    screenshot(out / "tc-returned-bottom.png")
    type_text("/exit")
    kernel32.FreeConsole()
    try:
        child.wait(timeout=120)
    except subprocess.TimeoutExpired:
        say("WARN: child did not exit in 120s; killing")
        child.kill()
        child.wait()
    say(f"child exit code: {child.returncode}")

    # ---- analysis + verdict ---------------------------------------------------
    ok_tb = stats["max_gap"] < 5.0 and stats["count"] >= args.seconds / 5
    ok_scroll = (r1["Top"] == r0["Top"] and r1["Bottom"] == r0["Bottom"]) and r0["Top"] < r_before["Top"]
    ok_tc = ok_scroll and tc_stats["count"] >= 12 and distinct_during >= 5
    analysis = (
        f"T-B (progress <=5s in REAL console): {'PASS' if ok_tb else 'FAIL'} — "
        f"{stats['count']} ticks / {args.seconds}s, max gap {stats['max_gap']:.2f}s, "
        f"{distinct_titles} distinct titles\n"
        f"T-C (scroll stays put while working): {'PASS' if ok_scroll else 'FAIL'} — "
        f"viewport Top/Bottom {r0['Top']}/{r0['Bottom']} -> {r1['Top']}/{r1['Bottom']} "
        f"(scrolled up by {r_before['Top'] - r0['Top']} lines first), "
        f"{tc_stats['count']} ticks + {distinct_during} title changes DURING the frozen window\n"
        f"verdict: {'PASS' if (ok_tb and ok_tc) else 'FAIL'}\n"
    )
    (out / "audit-analysis.txt").write_text(analysis, encoding="utf-8")
    print(analysis)
    return 0 if (ok_tb and ok_tc) else 1


if __name__ == "__main__":
    sys.exit(main())
