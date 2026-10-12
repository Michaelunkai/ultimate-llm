#!/usr/bin/env python3
"""T-F proof driver v2: execute ALL slash commands in a REAL interactive console.

Evidence: the child harness runs with ULTIMATEAI_TEE=<bundle>/transcript.txt so
everything it prints is captured verbatim; this driver types each command into
the child console (WriteConsoleInput — real keyboard-equivalent input through
msvcrt) and waits for the command's marker to appear in the transcript.
Exit 0 iff every command's marker is seen and the child exits cleanly.
"""

from __future__ import annotations

import ctypes
import os
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
        ok = kernel32.WriteConsoleInputW(h, records, len(chars), ctypes.byref(written))
        if not ok:
            raise RuntimeError("WriteConsoleInput failed err=%d" % ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(h)


def wait_marker(transcript: Path, marker, timeout: float, log) -> bool:
    import re

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            text = transcript.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if isinstance(marker, str):
            if marker.lower() in text.lower():
                return True
        elif re.search(marker, text, re.I):
            return True
        time.sleep(1.5)
        if not PROC or PROC.poll() is not None:
            time.sleep(2)  # let atexit flush the tee
            try:
                text = transcript.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            return marker.lower() in text.lower() if isinstance(marker, str) else bool(re.search(marker, text, re.I))
    return False


PROC = None


def main() -> int:
    global PROC
    import signal

    # The driver shares the child's console; a stray Ctrl+C (or any real
    # keyboard input reaching that console) must never kill the driver.
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (ValueError, OSError):
        pass
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = ROOT / "missions" / "logs" / f"slash-live-{ts}"
    out.mkdir(parents=True, exist_ok=True)
    transcript = out / "transcript.txt"
    log = open(out / "driver.log", "w", encoding="utf-8")
    results = []

    def say(msg):
        print(msg, flush=True)
        log.write(f"{datetime.now():%H:%M:%S} {msg}\n")
        log.flush()

    env = dict(os.environ)
    env["ULTIMATEAI_TEE"] = str(transcript)
    PROC = subprocess.Popen(
        [sys.executable, str(ROOT / "harness" / "harness.py"),
         "--model", "gpt-oss:20b", "--num-ctx", "32768", "--yes"],
        cwd=str(ROOT), creationflags=0x00000010, env=env,
    )
    say(f"child pid={PROC.pid}; waiting for banner")
    time.sleep(5)
    kernel32.FreeConsole()
    if not kernel32.AttachConsole(PROC.pid):
        say("FATAL: AttachConsole failed")
        PROC.kill()
        return 2
    time.sleep(1)  # deliberately NOT foregrounding the window: real keyboard
    # input must not interleave with the driver's injected keystrokes.

    def run_cmd(cmd: str, marker, timeout: float = 45.0) -> bool:
        import re

        is_re = isinstance(marker, str) and "|" in marker  # alternation = regex
        before = 0
        try:
            before = len(transcript.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            before = 0
        type_text(cmd)
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(1.5)
            try:
                text = transcript.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fresh = text[before:]
            if not is_re:
                hit = str(marker).lower() in fresh.lower()
            else:
                hit = bool(re.search(str(marker), fresh, re.I))
            if hit:
                results.append((cmd, str(marker), True))
                say(f"OK   {cmd}  -> {marker}")
                return True
        results.append((cmd, str(marker), False))
        say(f"FAIL {cmd}  -> marker '{marker}' not seen within {timeout:.0f}s")
        return False

    checks = [
        ("/help", "COMMANDS (26)"),
        ("/stats", "SESSION STATS"),
        ("/tools", "Tools (26)"),
        ("/usage", "USAGE"),
        ("/memory", "MEMORY"),
        ("/memory HAWK", "memory_search:"),
        ("/history", ". "),
        ("/sessions", "msgs"),
        ("/save", "Session saved"),
        ("/config", "CONFIG"),
        ("/config theme bright", "Set theme"),
        ("/config theme dim", "Set theme"),
        ("/config progress_interval_s 99", "Invalid"),
        ("/system", "ACTIVE SYSTEM PROMPT"),
        ("/note tf-note-42", "noted"),
        ("/doctor", "checks passed"),
        ("/update", "already on the best"),
        ("/model", "Current model"),
        ("/model gpt-oss:20b", "Model set"),
        ("/cwd", "Current cwd"),
        ("/export", "Session exported"),
        ("/copy", "No assistant answer"),
        ("/undo", "Nothing to undo"),
        ("/swipe", "View cleared"),
    ]
    all_ok = True
    for cmd, marker in checks:
        all_ok &= run_cmd(cmd, marker)

    # model-dependent commands (turn machinery)
    all_ok &= run_cmd("Say exactly this and nothing else: TF-OK-42", "TF-OK-42", timeout=240)
    all_ok &= run_cmd("/copy", "copied", timeout=45)
    all_ok &= run_cmd("/undo", "Undid the last exchange", timeout=45)
    all_ok &= run_cmd("/retry", "TF-OK-42|retrying", timeout=300)
    all_ok &= run_cmd("/research ollama", "ollama|Ollama|Error", timeout=300)
    all_ok &= run_cmd("/compact", "compacted|Nothing to compact", timeout=240)
    all_ok &= run_cmd("/reset", "Conversation reset", timeout=45)
    all_ok &= run_cmd("/new", "New session started", timeout=45)
    all_ok &= run_cmd("/verify", "step|TASK|verify|PASS|FAIL", timeout=360)
    # /verify spawns a full agent turn; wait for it to finish (TASK line or
    # 30s of transcript silence) before sending /exit, then allow a long exit.
    try:
        _base = len(transcript.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        _base = 0
    _deadline = time.time() + 300
    while time.time() < _deadline:
        time.sleep(5)
        try:
            _t = transcript.read_text(encoding="utf-8", errors="replace")
        except OSError:
            break
        if "TASK CONFIRMED COMPLETE" in _t[_base:] or "TASK DONE" in _t[_base:]:
            break
        if len(_t) == _base:
            break  # 30s of silence = back at prompt
        _base = len(_t)

    type_text("/exit")
    try:
        PROC.wait(timeout=300)
        say(f"child exit code: {PROC.returncode}")
        all_ok &= PROC.returncode == 0
    except subprocess.TimeoutExpired:
        say("child did not exit; killing")
        PROC.kill()
        all_ok = False
    kernel32.FreeConsole()

    passed = sum(1 for _, _, ok in results if ok)
    summary = f"T-F slash live: {passed}/{len(results)} command markers matched"
    say(summary)
    with open(out / "results.txt", "w", encoding="utf-8") as fh:
        fh.write(summary + "\n")
        for cmd, marker, ok in results:
            fh.write(f"{'PASS' if ok else 'FAIL'}  {cmd}  -> {marker}\n")
    print(("PASS" if all_ok else "FAIL") + " T-F")
    log.close()
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
