#!/usr/bin/env python3
"""MEM x3 certification runner (detached): three consecutive lossless-memory
missions with the independent checker after each. Writes missions/logs/
mem-cert-summary.txt; exit code 0 iff all three PASS."""

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
SUMMARY = ROOT / "missions" / "logs" / "mem-cert-summary.txt"


def main() -> int:
    lines = []
    ok_all = True
    with open(SUMMARY, "a", encoding="utf-8") as fh:
        fh.write(f"=== MEM cert round @ {datetime.now():%Y-%m-%d %H:%M:%S} ===\n")
    for r in (1, 2, 3):
        t0 = time.time()
        sandbox = ROOT / "missions" / "compliance" / "MEM"
        if sandbox.exists():
            for _attempt in (1, 2):
                rc_rm = subprocess.run(["rmdir", "/s", "/q", str(sandbox)], shell=True).returncode
                if rc_rm == 0 or not sandbox.exists():
                    break
                time.sleep(3)
            if sandbox.exists():
                lines.append(f"run{r}: ABORT - sandbox could not be wiped (leftover handles)")
                ok_all = False
                continue
        log = ROOT / "missions" / "logs" / f"mem-r{r}.log"
        with open(log, "w", encoding="utf-8") as fh:
            rc = subprocess.call(
                [PY, str(ROOT / "harness" / "harness.py"),
                 "--model", "gpt-oss:20b", "--num-ctx", "32768", "--yes",
                 "--mission", "@missions/compliance/MEM.txt"],
                cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT,
            )  # timeout guarded below via caller-side watch
        chk = subprocess.run(
            [PY, str(ROOT / "missions" / "compliance" / "check_mem.py"), str(log)],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        verdict = (chk.stdout or "").strip().splitlines()
        ok = rc == 0 and chk.returncode == 0
        ok_all &= ok
        lines.append(f"run{r}: mission-exit={rc} check-exit={chk.returncode} {'PASS' if ok else 'FAIL'} ({time.time()-t0:.0f}s)")
        lines.extend("  " + v for v in verdict)
        with open(SUMMARY, "a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now():%H:%M:%S}] {lines[-1-len(verdict)]}\n")
            for v in verdict:
                fh.write("  " + v + "\n")
    with open(SUMMARY, "a", encoding="utf-8") as fh:
        fh.write(f"OVERALL: {'PASS 3/3' if ok_all else 'FAIL'}\n")
    print("OVERALL:", "PASS 3/3" if ok_all else "FAIL")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
