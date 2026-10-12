#!/usr/bin/env python3
"""T-Y2 proof driver: the resource-yield sentinel under REAL pressure signals.

Hermetic choreography (desktop apps on this machine spike 2-3GB organically,
so ambient thresholds are made inert first, then pressure is applied exactly
once through config):
  0. hermetic config (yield_other_gb 8.0 / headroom 0.2 -> sentinel inert);
     prewarm model; spawn the interactive child (CREATE_NEW_CONSOLE + TEE);
  1. /yield panel shows the sentinel ACTIVE; 12s with ZERO yield events
     (no false positives at inert thresholds);
  2. /config yield_other_gb 0.5 -> the desktop's ~1GB baseline crosses the
     threshold -> 2 ticks -> sentinel unloads. Assert /api/ps shows the model
     GONE (real absence, positive-presence gated), audit kind:"yield", and the
     yielded notice - within 30s;
  3. mid-yield command: type a prompt while unloaded -> model reloads on
     demand (Ollama layer-splits; same model, same context) and answers.
     Completion = ASCII-safe post-turn status line + prompt glyph; audit
     cadence max gap < 5s across the turn;
  4. /config yield_other_gb 8.0 -> pressure gone >= yield_rewarm_s -> audit
     kind:"rewarm" + model resident again;
  5. /yield off -> NO new yield events for 12s; /yield on;
  6. clean /exit (child exit 0, Goodbye on the transcript).
Config is restored/deleted in finally. Exit 0 iff ALL asserts pass.
Bundle: missions/logs/yield-<ts>/
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
AUDIT = UAI / "progress-audit.jsonl"
CONFIG_FILE = UAI / "config.json"
MODEL = "gpt-oss:20b"

sys.path.insert(0, str(ROOT / "tools"))
from slash_live import type_text  # noqa: E402

ANSI_CSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
ANSI_OSC = re.compile(r"\x1b\][^\x07]*\x07")
_ps_err = None


def ps_models():
    """{model: vram_gb} from /api/ps, or None when the query FAILED (distinct
    from an empty list = genuinely no models loaded)."""
    global _ps_err
    try:
        import httpx

        with httpx.Client(timeout=5.0) as c:
            r = c.get("http://127.0.0.1:11434/api/ps")
        return {m.get("name", ""): int(m.get("size_vram", 0) or 0) / 1e9 for m in r.json().get("models", [])}
    except Exception as exc:
        _ps_err = f"{type(exc).__name__}: {str(exc)[:120]}"
        return None


def audit_events():
    out = []
    try:
        with open(AUDIT, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except OSError:
        pass
    return out


def read_tee(path: Path) -> str:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return ANSI_OSC.sub("", ANSI_CSI.sub("", raw))


def prewarm(say) -> bool:
    try:
        import httpx

        with httpx.Client(timeout=httpx.Timeout(300.0, connect=10.0)) as c:
            c.post("http://127.0.0.1:11434/api/generate", json={"model": MODEL, "prompt": "", "keep_alive": "24h"})
        for _ in range(20):
            m = ps_models()
            if m is not None and m.get(MODEL, 0) > 5.0:
                return True
            time.sleep(2)
    except Exception as exc:
        say(f"prewarm exception: {exc}")
    return False


def main() -> int:
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (ValueError, OSError):
        pass
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = ROOT / "missions" / "logs" / f"yield-{ts}"
    out.mkdir(parents=True, exist_ok=True)
    transcript = out / "transcript.txt"
    ps_samples = []
    log = open(out / "driver.log", "w", encoding="utf-8")
    checks = {}
    proc = None
    config_backup = CONFIG_FILE.read_text(encoding="utf-8") if CONFIG_FILE.exists() else None

    def say(msg):
        print(msg, flush=True)
        log.write(f"{datetime.now():%H:%M:%S} {msg}\n")
        log.flush()

    def sample(tag):
        m = ps_models()
        ps_samples.append({"t": time.time(), "tag": tag, "ps": m, "err": _ps_err})
        return m

    def wait_resident(timeout=40.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            m = sample("wait-resident")
            if m is not None and m.get(MODEL, 0) > 5.0:
                return True
            time.sleep(2)
        return False

    try:
        # 0) hermetic ambient: sentinel inert until the test applies pressure
        UAI.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps({"yield_other_gb": 8.0, "yield_headroom_gb": 0.2}), encoding="utf-8")
        if not prewarm(say):
            return premature("prewarm failed")
        env = dict(os.environ)
        env["ULTIMATEAI_TEE"] = str(transcript)
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "harness" / "harness.py"),
             "--model", MODEL, "--num-ctx", "32768", "--yes"],
            cwd=str(ROOT), creationflags=0x00000010, env=env,
        )
        say(f"child pid={proc.pid}; waiting for prompt")
        deadline = time.time() + 90
        while time.time() < deadline:
            # ASCII-only marker: the prompt glyph never survives the cp1252
            # console pipe, but this hint line is printed right before it.
            if "Type your goal and press Enter" in read_tee(transcript):
                break
            time.sleep(1.5)
        time.sleep(3)
        kernel32.FreeConsole()
        if not kernel32.AttachConsole(proc.pid):
            say("FATAL: AttachConsole failed")
            proc.kill()
            return 2
        time.sleep(1)

        ev_start = len(audit_events())
        # daemon may have cycled - guarantee residency again from the driver
        if not wait_resident(30):
            say("model not resident after spawn - re-prewarming once")
            if not prewarm(say) or not wait_resident(30):
                return premature("model not resident after re-prewarm")
        checks["ps_visible"] = True
        say("0) hermetic config + prewarm + resident: True")

        # 1) sentinel lifecycle + stability (no false positives)
        type_text("/yield")
        time.sleep(6)
        t1 = read_tee(transcript)
        checks["sentinel_active"] = "RESOURCE YIELD" in t1 and "active" in t1
        ev_mark1 = len(audit_events())
        time.sleep(12)
        checks["no_false_yields"] = not any(
            e.get("kind") in ("yield", "rewarm") for e in audit_events()[ev_mark1:]
        )
        say(f"1) sentinel active: {checks['sentinel_active']} · 12s stability (no events): {checks['no_false_yields']}")

        # 2) apply pressure deterministically
        notice_base = t1.count("yielded")
        type_text("/config yield_other_gb 0.5")
        cfg_applied = False
        tee_mark = len(read_tee(transcript))
        deadline = time.time() + 20
        while time.time() < deadline:
            if "Set yield_other_gb" in read_tee(transcript)[tee_mark:]:
                cfg_applied = True
                break
            time.sleep(1)
        checks["config_applied"] = cfg_applied
        yielded = False
        last = {}
        deadline = time.time() + 30
        while time.time() < deadline:
            time.sleep(1.5)
            m = sample("pre-yield")
            evs = audit_events()[ev_start:]
            last = {
                "has_ev": any(e.get("kind") == "yield" for e in evs),
                "unloaded": m is not None and m.get(MODEL, 0) == 0,
                "notice": read_tee(transcript).count("yielded") > notice_base,
            }
            if all(last.values()):
                yielded = True
                break
        checks["auto_yield"] = yielded
        say(f"2) config applied: {cfg_applied} · auto-yield (unload+audit+notice): {yielded} legs={last}")

        # 3) mid-yield command -> on-demand reload + answer
        tee_base = len(read_tee(transcript))
        t_turn = time.time()
        type_text("Reply with exactly: BACK-ONLINE")

        def turn_done(offset: int) -> bool:
            # ASCII-only: back at the prompt is proven by the post-turn status
            # line (gpt-oss:20b · N.Nk/N k ctx (..%)) or the TASK DONE panel -
            # the prompt glyph itself never survives the cp1252 console pipe.
            t = read_tee(transcript)[offset:]
            done_sig = bool(re.search(r"gpt-oss:20b[^\r\n]*ctx \(\d+%", t)) or "TASK DONE" in t
            return t.count("BACK-ONLINE") >= 2 and done_sig

        answered = False
        deadline = time.time() + 300
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            if turn_done(tee_base):
                answered = True
                time.sleep(3)
                break
            time.sleep(2)
        t_turn_end = time.time()
        checks["midyield_answer"] = answered
        recs = sorted(float(e["ts"]) for e in audit_events()[ev_start:]
                      if e.get("kind") in ("tick", "event") and t_turn <= float(e["ts"]) <= t_turn_end)
        gaps = [round(b - a, 2) for a, b in zip(recs, recs[1:])]
        checks["turn_audit_gap<5s"] = bool(gaps) and max(gaps) < 5.0
        say(f"3) mid-yield command answered: {answered} · audit ticks={len(gaps) + 1} max_gap={max(gaps) if gaps else 'n/a'}")

        # 4) remove the applied pressure -> auto-rewarm
        type_text("/config yield_other_gb 8.0")
        time.sleep(2)
        evs_mark = len(audit_events())
        rewarm = False
        deadline = time.time() + 120
        while time.time() < deadline:
            time.sleep(2)
            evs = audit_events()[evs_mark:]
            m = sample("rewarm-watch")
            if any(e.get("kind") == "rewarm" for e in evs) and m is not None and m.get(MODEL, 0) > 1.0:
                rewarm = True
                break
        checks["auto_rewarm"] = rewarm
        if not rewarm:
            # dump the sentinel's own view for diagnosis
            type_text("/yield")
            time.sleep(6)
            tail = read_tee(transcript)[-2000:]
            (out / "sentinel-panel.txt").write_text(tail, encoding="utf-8")
            say("4) sentinel panel dumped -> sentinel-panel.txt")
        say(f"4) auto-rewarm: {rewarm} ps_err={_ps_err}")

        # 5) /yield off -> no further auto unload
        evs_mark2 = len(audit_events())
        type_text("/yield off")
        time.sleep(12)
        checks["off_no_yield"] = not any(
            e.get("kind") in ("yield", "rewarm") for e in audit_events()[evs_mark2:]
        )
        say(f"5) /yield off -> quiet: {checks['off_no_yield']}")
        type_text("/yield on")
        time.sleep(2)

        # 6) clean exit
        exit_base = len(read_tee(transcript))
        if not wait_resident(5):
            pass  # residency not required to exit
        time.sleep(2)
        type_text("/exit")
        try:
            deadline = time.time() + 180
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                if "Goodbye" in read_tee(transcript)[exit_base:]:
                    time.sleep(3)
                    break
                time.sleep(2)
            proc.wait(timeout=60)
            checks["clean_exit"] = proc.returncode == 0
        except subprocess.TimeoutExpired:
            proc.kill()
            checks["clean_exit"] = False
        say(f"6) child exit code: {proc.returncode}")
        kernel32.FreeConsole()
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            if config_backup is not None:
                CONFIG_FILE.write_text(config_backup, encoding="utf-8")
            elif CONFIG_FILE.exists():
                CONFIG_FILE.unlink()
        except Exception:
            pass

    all_ok = all(checks.values())
    with open(out / "results.txt", "w", encoding="utf-8") as fh:
        fh.write(("PASS" if all_ok else "FAIL") + " T-Y2 yield proof\n")
        for k, v in checks.items():
            fh.write(f"  {'PASS' if v else 'FAIL'} {k}\n")
    (out / "ps-samples.json").write_text(json.dumps(ps_samples, indent=1), encoding="utf-8")
    say(("PASS" if all_ok else "FAIL") + f" T-Y2 ({out})")
    log.close()
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
