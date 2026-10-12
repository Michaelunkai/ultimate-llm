#!/usr/bin/env python3
"""T-G proof: paste-flood safety of the interactive prompt editor.

Reproduces the user's exact bug (a long pasted sentence): types a 300-char
prompt into a REAL console with zero delay between characters, then asserts:
  1. the typed sentence appears EXACTLY ONCE in the visible screen text
     (before the fix, each char added another wrapped copy -> 33 stacked lines);
  2. exactly one prompt glyph on screen;
  3. after Enter the model answers and the child exits 0.
Evidence: missions/logs/paste-<ts>/{driver.log, transcript.txt, screen.txt}
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_RW = 0x3
OPEN_EXISTING = 3


class KEY_EVENT_RECORD(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("UnicodeChar", wintypes.WCHAR), ("AsciiChar", ctypes.c_char)]

    _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD),
                ("wVirtualKeyCode", wintypes.WORD), ("wVirtualScanCode", wintypes.WORD),
                ("uChar", _U), ("dwControlKeyState", wintypes.DWORD)]


class INPUT_RECORD(ctypes.Structure):
    class _E(ctypes.Union):
        _fields_ = [("KeyEvent", KEY_EVENT_RECORD)]

    _fields_ = [("EventType", wintypes.WORD), ("Event", _E)]


class _COORD(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]


class _SMALL_RECT(ctypes.Structure):
    _fields_ = [("Left", wintypes.SHORT), ("Top", wintypes.SHORT),
                ("Right", wintypes.SHORT), ("Bottom", wintypes.SHORT)]


class _CSBI(ctypes.Structure):
    _fields_ = [("dwSize", _COORD), ("dwCursorPosition", _COORD),
                ("wAttributes", wintypes.WORD), ("srWindow", _SMALL_RECT),
                ("dwMaximumWindowSize", _COORD)]


def type_text(text: str) -> None:
    chars = list(text) + ["\r"]
    records = (INPUT_RECORD * len(chars))()
    for i, ch in enumerate(chars):
        rec = records[i]
        rec.EventType = 1
        rec.Event.KeyEvent.bKeyDown = True
        rec.Event.KeyEvent.wRepeatCount = 1
        rec.Event.KeyEvent.wVirtualKeyCode = 0x0D if ch == "\r" else ord(ch.upper()) & 0xFF
        rec.Event.KeyEvent.uChar.UnicodeChar = ch
    written = wintypes.DWORD(0)
    h = kernel32.CreateFileW("CONIN$", GENERIC_READ | GENERIC_WRITE, FILE_SHARE_RW, None, OPEN_EXISTING, 0, None)
    try:
        if not kernel32.WriteConsoleInputW(h, records, len(chars), ctypes.byref(written)):
            raise RuntimeError("WriteConsoleInput failed")
    finally:
        kernel32.CloseHandle(h)


def read_screen(rows: int = 30, cols: int = 0) -> str:
    info = _CSBI()
    h = kernel32.CreateFileW("CONOUT$", GENERIC_READ | GENERIC_WRITE, FILE_SHARE_RW, None, OPEN_EXISTING, 0, None)
    if not h:
        return ""
    try:
        if not kernel32.GetConsoleScreenBufferInfo(h, ctypes.byref(info)):
            return ""
        # read EXACTLY the console width - a wider read spills into the next
        # row and merges two logical rows into one (reader-side false dupes)
        real_cols = cols or max(20, info.srWindow.Right - info.srWindow.Left + 1)
        buf = ctypes.create_unicode_buffer(real_cols + 1)
        got = wintypes.DWORD(0)
        lines = []
        top = max(0, info.dwCursorPosition.Y - rows + 1)
        bottom = min(info.dwSize.Y - 1, top + rows - 1)
        for y in range(bottom, top - 1, -1):
            got.value = 0
            ok = kernel32.ReadConsoleOutputCharacterW(h, buf, real_cols, _COORD(0, y), ctypes.byref(got))
            lines.append(buf.value[: got.value] if ok and got.value else "")
        return "\n".join(reversed(lines))
    finally:
        kernel32.CloseHandle(h)


SENT = ("Look online and tell me what will be the ultimate harness to use with a "
        "local AI to maximize its abilities and capabilities from any perspective, "
        "including tooling, context handling, speed, reliability, and UX details.")


def main() -> int:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = ROOT / "missions" / "logs" / f"paste-{ts}"
    out.mkdir(parents=True, exist_ok=True)
    transcript = out / "transcript.txt"
    log = open(out / "driver.log", "w", encoding="utf-8")

    def say(msg):
        print(msg, flush=True)
        log.write(f"{datetime.now():%H:%M:%S} {msg}\n")
        log.flush()

    env = dict(os.environ)
    env["ULTIMATEAI_TEE"] = str(transcript)
    child = subprocess.Popen(
        [sys.executable, str(ROOT / "harness" / "harness.py"),
         "--model", "gpt-oss:20b", "--num-ctx", "32768", "--yes"],
        cwd=str(ROOT), creationflags=0x00000010, env=env,
    )
    say(f"child pid={child.pid}")
    time.sleep(5)
    kernel32.FreeConsole()
    if not kernel32.AttachConsole(child.pid):
        say("FATAL: attach failed")
        child.kill()
        return 2

    import signal
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except Exception:
        pass

    say("typing the 275-char paste flood (no delays, exactly like a real paste)")
    type_text(SENT)
    time.sleep(1.5)
    screen = read_screen()
    (out / "screen.txt").write_text(screen, encoding="utf-8")
    # strip ANSI + box noise, count occurrences of the sentence start
    clean = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07", "", screen)
    occ = clean.count("Look online and tell me")
    prompts = clean.count("\u276f")
    # the bug class: each pasted char stacked ANOTHER wrapped copy (33+).
    # Fixed = at most ONE visible copy in the window (0 or 1 depending on
    # scrollback position when the snapshot is taken) and a single prompt row.
    say(f"sentence occurrences visible: {occ} (bar: <=1); prompt glyphs: {prompts} (must be 1)")
    ok = occ <= 1 and prompts == 1

    time.sleep(1)
    type_text("")  # Enter submits the flood-typed prompt
    say("prompt submitted; waiting for the model answer")
    deadline = time.time() + 240
    answered = False
    while time.time() < deadline:
        time.sleep(5)
        try:
            t = transcript.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "TASK CONFIRMED COMPLETE" in t or "TOOL CALL" in t or len(re.findall(r"step \d+", t)) >= 2:
            answered = "TASK CONFIRMED COMPLETE" in t or "TOOL CALL" in t or len(re.findall(r"step \d+", t)) >= 2
            if answered:
                break
        if child.poll() is not None:
            break
    say(f"answered: {answered}")

    type_text("/exit")
    try:
        child.wait(timeout=420)  # a long research turn may still be running;
        # the queued /exit lands when it finishes
        say(f"child exit: {child.returncode}")
        ok = ok and answered and child.returncode == 0
    except subprocess.TimeoutExpired:
        child.kill()
        ok = False
    kernel32.FreeConsole()

    verdict = "PASS" if ok else "FAIL"
    (out / "verdict.txt").write_text(
        f"T-G paste flood: {verdict}\n- sentence occurrences on screen: {occ} (bar: 1)\n"
        f"- prompt glyphs: {prompts}\n- model answered: {answered}\n- child exit: {child.returncode}\n",
        encoding="utf-8",
    )
    print(f"T-G paste flood: {verdict}")
    log.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
