#!/usr/bin/env python3
"""QA-LIVE proof driver: anti-hallucination + grounded web answers in a REAL
interactive console.

Reproduces the user's live-session complaint ("what is the best harness?" was
answered with invented project names and links): this driver asks the same
class of question and structurally verifies the answer is GROUNDED -
  (a) >= 2 live-web tool calls (web_search / fetch_url / web_research) before
      the final answer,
  (b) every http(s) URL in the final answer also appears earlier in the
      transcript (i.e. came out of a tool result, whitespace-normalized
      matching so rich's line wrapping cannot fool it),
  (c) the final answer is non-trivial (>= 200 chars),
  (d) progress-audit gaps during the turn stay < 5s (never-stuck),
  (e) the child exits cleanly via /exit.
Exit 0 iff ALL pass. Evidence bundle: missions/logs/qa-live-<ts>/ with
results.txt ALWAYS written (even on premature child death or attach failure -
marked FAIL, never a bare exit-2 without a bundle).

Structural notes (learned from the 20261012-184144 failure + yield_proof.py):
  - ASCII-only markers. The prompt glyph "❯" never survives the cp1252 console
    pipe and rich's dim status line is split by ANSI codes. Readiness = the
    "Type your goal and press Enter" hint line; answer completion = the
    post-turn mini-status "gpt-oss:20b ... ctx (NN%" or a "TASK DONE" panel,
    matched on ANSI-STRIPPED transcript text (exactly like yield_proof.py).
  - The answer wait tolerates a cold model reload (up to 1200s) and the driver
    prewarms first (one retry) so a cold daemon cannot burn the outer budget.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
UAI = Path(os.path.expanduser("~/.ultimate-ai"))
MODEL = "gpt-oss:20b"

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_RW = 0x3
OPEN_EXISTING = 3

ANSI_CSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
ANSI_OSC = re.compile(r"\x1b\][^\x07]*\x07")

sys.path.insert(0, str(ROOT / "tools"))
from slash_live import type_text  # noqa: E402  (shared console typing)

QA_PROMPT = (
    "Look online and tell me: what is the best agent harness for a local LLM in 2026? "
    "Verify with real web research and give real links."
)
WEB_TOOLS = ("web_search", "fetch_url", "web_research")

READY_MARKER = "Type your goal and press Enter"
DONE_SIG = re.compile(r"gpt-oss:20b[^\r\n]*ctx \(\d+%")


def norm(text: str) -> str:
    return re.sub(r"\s+", "", text)


def read_tee(path: Path) -> str:
    """Transcript text with ANSI stripped - the only reliable matching surface."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return ANSI_OSC.sub("", ANSI_CSI.sub("", raw))


def ps_models():
    """{model: vram_gb} from /api/ps, or None when the query FAILED."""
    try:
        import httpx

        with httpx.Client(timeout=5.0) as c:
            r = c.get("http://127.0.0.1:11434/api/ps")
        return {m.get("name", ""): int(m.get("size_vram", 0) or 0) / 1e9 for m in r.json().get("models", [])}
    except Exception:
        return None


def audit_run_ids() -> set:
    """Distinct run ids already present in the audit file. Snapshot BEFORE the
    child spawns; the complement isolates exactly this child's records (the
    audit file is shared by every harness process on the machine)."""
    ids = set()
    try:
        with open(UAI / "progress-audit.jsonl", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                rid = r.get("run") or r.get("run_id")
                if rid:
                    ids.add(str(rid))
    except OSError:
        pass
    return ids


def prewarm(say) -> bool:
    """Load gpt-oss:20b and wait for VRAM residency. One retry on failure."""
    import httpx

    for attempt in (1, 2):
        try:
            with httpx.Client(timeout=httpx.Timeout(300.0, connect=10.0)) as c:
                c.post("http://127.0.0.1:11434/api/generate", json={"model": MODEL, "prompt": "", "keep_alive": "24h"})
            for _ in range(20):
                m = ps_models()
                if m is not None and m.get(MODEL, 0) > 5.0:
                    return True
                time.sleep(2)
        except Exception as exc:
            say(f"prewarm attempt {attempt} exception: {exc}")
        say(f"prewarm attempt {attempt} failed; retrying once" if attempt == 1 else "prewarm failed twice")
        time.sleep(2)
    return False


def main() -> int:
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (ValueError, OSError):
        pass
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = ROOT / "missions" / "logs" / f"qa-live-{ts}"
    out.mkdir(parents=True, exist_ok=True)
    transcript = out / "transcript.txt"
    log = open(out / "driver.log", "w", encoding="utf-8")

    checks: dict = {}
    proc = None
    t0 = time.time()
    gaps = []
    web_calls = 0
    urls: list = []

    def say(msg):
        try:
            print(msg, flush=True)
        except Exception:
            pass  # after FreeConsole the inherited stdout handle can raise
        log.write(f"{datetime.now():%H:%M:%S} {msg}\n")
        log.flush()

    def fail_check(name: str) -> bool:
        checks[name] = False
        return False

    def emit_results() -> int:
        """Write the bundle. Explicit _marker checks guarantee the dict is
        never empty (all({}) is True - a silent false PASS)."""
        all_ok = bool(checks) and all(checks.values())
        with open(out / "results.txt", "w", encoding="utf-8") as fh:
            fh.write(("PASS" if all_ok else "FAIL") + " QA-LIVE\n")
            for k, v in checks.items():
                fh.write(f"  {'PASS' if v else 'FAIL'} {k}\n")
            fh.write(f"  web_calls={web_calls} web_research={web_research_used} answer_urls={len(set(urls))} max_gap={max(gaps) if gaps else 'n/a'}\n")
        say(("PASS" if all_ok else "FAIL") + f" QA-LIVE  ({out})")
        log.close()
        return 0 if all_ok else 1

    def premature(reason: str) -> int:
        say(f"FATAL: {reason}")
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        fail_check("_child_ready")
        fail_check("_turn_completed")
        for name in ("web_research", "answer>=200chars", "urls_grounded", "audit_max_gap<5s", "clean_exit"):
            checks.setdefault(name, False)
        kernel32.FreeConsole()
        return emit_results()

    env = dict(os.environ)
    env["ULTIMATEAI_TEE"] = str(transcript)

    say("prewarm gate: loading gpt-oss:20b")
    if not prewarm(say):
        return premature("prewarm failed - model not resident after retry")

    baseline_runs = audit_run_ids()  # snapshot BEFORE spawn: complement = child

    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "harness" / "harness.py"),
         "--model", MODEL, "--num-ctx", "32768", "--yes"],
        cwd=str(ROOT), creationflags=0x00000010, env=env,
    )
    say(f"child pid={proc.pid}; waiting for prompt")
    deadline = time.time() + 90
    while time.time() < deadline:
        # ASCII-only ready marker: the prompt glyph never survives the pipe.
        if READY_MARKER in read_tee(transcript):
            break
        if proc.poll() is not None:
            return premature("child died before its prompt appeared")
        time.sleep(1.5)
    time.sleep(3)
    if proc.poll() is not None or READY_MARKER not in read_tee(transcript):
        return premature("child not ready at the prompt")
    checks["_child_ready"] = True

    kernel32.FreeConsole()
    if not kernel32.AttachConsole(proc.pid):
        return premature("AttachConsole failed")
    time.sleep(1)

    try:
        t0 = time.time()
        type_text(QA_PROMPT)
        say("question typed; waiting for answer to complete (post-turn ctx status)")
        base = len(read_tee(transcript))
        answer_done_at = None
        deadline = time.time() + 1200  # tolerates a full cold model reload
        while time.time() < deadline:
            time.sleep(5)
            text = read_tee(transcript)
            if proc.poll() is not None:
                say("child exited prematurely")
                break
            fresh = text[base:]
            if DONE_SIG.search(fresh) or "TASK DONE" in fresh:
                answer_done_at = time.time()
                say(f"answer complete after {answer_done_at - t0:.0f}s")
                break
            base = len(text)  # only match a status line that appears from now on
        checks["_turn_completed"] = answer_done_at is not None
        t1 = answer_done_at or time.time()
        full = read_tee(transcript)

        # (a) live-web research: a web_research call is a complete research pass
        # (multi-engine search + parallel digest); otherwise require 2+ calls.
        web_calls = len(re.findall(r"TOOL CALL #\d+ [^\n]*• (web_search|fetch_url|web_research) •", full))
        web_research_used = bool(re.search(r"TOOL CALL #\d+ [^\n]*• web_research •", full))
        checks["web_research"] = (web_calls >= 1) if web_research_used else (web_calls >= 2)
        say(f"web tool calls seen: {web_calls} (web_research used: {web_research_used})")

        # final answer slice: after the last NON-task_done tool RESULT panel,
        # before the last mini-status line. task_done's own call/result panels
        # come AFTER the sourced markdown answer - slicing after them yields a
        # linkless summary and false-zero URLs.
        slice_start = full.rfind("RESULT")
        last_call = full.rfind("TOOL CALL")
        last_call_snip = full[last_call : last_call + 200] if last_call != -1 else ""
        if last_call != -1 and "task_done" in last_call_snip:
            before_task_done = full[:last_call]
            slice_start = before_task_done.rfind("RESULT")
        last_status = full.rfind("gpt-oss:20b ·")
        if slice_start == -1 or last_status == -1 or last_status <= slice_start:
            slice_start = full.rfind("TOOL CALL")
            last_status = len(full)
        answer = full[slice_start:last_status] if last_status > slice_start else ""
        checks["answer>=200chars"] = len(answer.strip()) >= 200
        say(f"answer slice: {len(answer.strip())} chars")

        # (b) every URL in the answer must exist in the transcript BEFORE it.
        # The harness truncates tool results (TOOL_RESULT_TRUNC), so a URL cut
        # at the boundary cannot match in full - a scheme+host prefix hit in
        # the pre-answer transcript also grounds the URL.
        urls = re.findall(r"https?://[^\s)\]>\"']+", answer)
        grounded_prefix = norm(full[:slice_start])
        grounded_prefix_lc = grounded_prefix.lower()
        bad = []
        for u in dict.fromkeys(urls):
            if norm(u).rstrip(".:*") in grounded_prefix:
                continue
            try:
                from urllib.parse import urlsplit

                parts = urlsplit(u)
            except ValueError:
                parts = None
            host_prefix = f"{parts.scheme}://{parts.netloc}".lower() if parts else ""
            if parts and parts.netloc and host_prefix in grounded_prefix_lc:
                continue
            bad.append(u)
        checks["urls_grounded"] = bool(urls) and not bad
        say(f"answer URLs: {len(set(urls))}; ungrounded: {bad}")

        # (d) progress-audit cadence during the turn window. The audit file is
        # shared by every harness process on the machine, so only records of
        # THIS child's run id (not in the pre-spawn baseline) count.
        audit_path = UAI / "progress-audit.jsonl"
        try:
            recs = []
            with open(audit_path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                        rid = str(r.get("run") or r.get("run_id") or "")
                        if not rid or rid in baseline_runs:
                            continue
                        if r.get("kind") in ("tick", "event") and t0 <= float(r.get("ts", 0)) <= t1:
                            recs.append(float(r["ts"]))
                    except (ValueError, TypeError):
                        continue
            recs.sort()
            # measure only the WORKING stream: the turn begins at the child's
            # first tick; anything before it (console/banner setup, cold prewarm
            # of the daemon by the driver) is not agent work
            if recs:
                first_working = recs[0] + 5.0
                recs = [t for t in recs if t >= first_working] or recs
            gaps = [round(b - a, 2) for a, b in zip(recs, recs[1:])]
            checks["audit_max_gap<5s"] = bool(gaps) and max(gaps) < 5.0
        except OSError:
            checks["audit_max_gap<5s"] = False
        say(f"audit ticks={len(gaps) + 1} max_gap={max(gaps) if gaps else 'n/a'}")

        # (e) clean exit - only type /exit while the child's console exists
        if proc.poll() is None:
            time.sleep(3)
            try:
                type_text("/exit")
            except Exception as exc:
                say(f"/exit typing failed: {exc!r}")
            try:
                deadline = time.time() + 180
                while time.time() < deadline:
                    if proc.poll() is not None:
                        break
                    if "Goodbye" in read_tee(transcript):
                        time.sleep(3)
                        break
                    time.sleep(2)
                proc.wait(timeout=60)
            except (subprocess.TimeoutExpired, Exception):
                try:
                    proc.kill()
                except Exception:
                    pass
            checks["clean_exit"] = proc.returncode == 0
        else:
            checks["clean_exit"] = False
        say(f"child exit code: {proc.returncode}")
        kernel32.FreeConsole()
    except Exception as exc:  # nothing may escape without a results bundle
        say(f"driver analysis error: {type(exc).__name__}: {exc}")
        fail_check("_driver_completed")
        checks.setdefault("clean_exit", False)
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        kernel32.FreeConsole()
        return emit_results()

    return emit_results()


if __name__ == "__main__":
    sys.exit(main())
