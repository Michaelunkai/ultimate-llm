#!/usr/bin/env python3
"""T-E checker: lossless memory after REAL compaction. Usage: python check_mem.py <transcript.log>"""
import json, sys, glob, os
from pathlib import Path

log = Path(sys.argv[1])
text = log.read_text(encoding="utf-8", errors="replace")
ev = []
ok = True

# 1) newest archive containing BOTH the codeword user record AND a real compaction record
best = None
for f in sorted(glob.glob(os.path.expanduser("~/.ultimate-ai/sessions/*.jsonl")), key=os.path.getmtime, reverse=True):
    zebra_ts = None
    comps = []
    with open(f, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("kind") == "msg" and r.get("role") == "user" and "HAWK-8842-XZ" in str(r.get("content", "")):
                if zebra_ts is None:
                    zebra_ts = r.get("ts")
            if r.get("kind") == "compaction":
                comps.append(r.get("ts"))
    if zebra_ts and comps:
        best = (os.path.basename(f), zebra_ts, comps)
        break

if not best:
    ev.append("missing: no archive with both a token user record AND a compaction record")
    ok = False
else:
    f, zts, comps = best
    after = [c for c in comps if c > zts]
    ev.append(f"archive {f}: codeword recorded {zts}; {len(comps)} real compaction(s), {len(after)} after the codeword")
    ok = ok and bool(after)

# 2) transcript shows a REAL harness compaction notice (piped mode prints it)
comp_notice = "(context compaction:" in text or "context compacted:" in text
ev.append(f"real harness compaction notice in transcript: {comp_notice}")
ok = ok and comp_notice

# 3) memory_search returned the codeword from the archive (result panel quoting the record)
ms_result = ("memory_search RESULT" in text and "HAWK-8842" in text)
ev.append(f"memory_search result with codeword in transcript: {ms_result}")
ok = ok and ms_result

# 4) final answer contains the codeword
ev.append(f"codeword present in transcript/answer: {'HAWK-8842-XZ' in text}")
ok = ok and "HAWK-8842-XZ" in text

# 5) codeword.txt exact + part files
cw = Path("missions/compliance/MEM/codeword.txt")
cw_ok = cw.exists() and cw.read_text(encoding="utf-8").strip() == "HAWK-8842-XZ"
ev.append(f"codeword.txt exact: {cw_ok}")
ok = ok and cw_ok
parts = len(glob.glob("missions/compliance/MEM/loop/part*.txt"))
ev.append(f"part files written: {parts} (need >= 20)")
ok = ok and parts >= 20

print(("PASS" if ok else "FAIL") + " MEM (lossless memory)")
for e in ev:
    print("  - " + e)
sys.exit(0 if ok else 1)
