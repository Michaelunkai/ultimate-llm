#!/usr/bin/env python3
"""Context-window probe (contracts-v4 C8): is num_ctx 65536 viable for
gpt-oss:20b on this 16GB card, or does 32768 remain the right default?

Runs 3 chats at 32768 (baseline) then 3 at 65536, measuring tokens/s from the
API's own eval timings and VRAM residency from /api/ps. Decision rule:
ADOPT 65536 only if no errors, t/s >= 50% of the baseline median on all runs,
and /api/ps VRAM size <= 15.5 GB. Otherwise KEEP 32768.
Pure HTTP against the running daemon - no daemon restart, no state change.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

HOST = "http://127.0.0.1:11434"
MODEL = "gpt-oss:20b"
ROOT = Path(__file__).resolve().parents[1]


def chat(num_ctx: int, prompt: str, num_predict: int = 128) -> dict:
    payload = {
        "model": MODEL,
        "stream": False,
        "messages": [{"role": "user", "content": prompt}],
        "keep_alive": "24h",
        "options": {"num_ctx": num_ctx, "num_predict": num_predict, "temperature": 0.6},
    }
    with httpx.Client(timeout=httpx.Timeout(600.0, connect=10.0)) as client:
        t0 = time.time()
        resp = client.post(HOST + "/api/chat", json=payload)
        wall = time.time() - t0
    resp.raise_for_status()
    data = resp.json()
    eval_count = int(data.get("eval_count") or 0)
    eval_dur = float(data.get("eval_duration") or 0) / 1e9
    tps = (eval_count / eval_dur) if eval_dur > 0 else 0.0
    return {"wall": round(wall, 1), "eval_count": eval_count, "tps": round(tps, 1)}


def ps() -> dict:
    with httpx.Client(timeout=10.0) as client:
        r = client.get(HOST + "/api/ps")
        for m in r.json().get("models", []):
            if m.get("name") == MODEL or m.get("model") == MODEL:
                return {"size_vram_gb": round(m.get("size_vram", 0) / 1e9, 2), "size_gb": round(m.get("size", 0) / 1e9, 2)}
    return {"size_vram_gb": 0.0, "size_gb": 0.0}


def main() -> int:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = ROOT / "missions" / "logs" / f"ctx-probe-{ts}"
    out.mkdir(parents=True, exist_ok=True)
    report = []
    prompt = (
        "Write a 120-word technical summary of what a context window is for a "
        "local LLM agent, then list three risks of exceeding it."
    )
    results = {32768: [], 65536: []}
    errors = []
    for nc in (32768, 65536):
        for i in (1, 2, 3):
            try:
                r = chat(nc, prompt)
                r["ps"] = ps()
                results[nc].append(r)
                line = f"num_ctx={nc} run{i}: {r['eval_count']} tok, {r['tps']} tok/s, wall {r['wall']}s, vram {r['ps']['size_vram_gb']}GB"
            except Exception as exc:
                errors.append(f"num_ctx={nc} run{i}: {type(exc).__name__}: {str(exc)[:160]}")
                line = f"num_ctx={nc} run{i}: ERROR {str(exc)[:160]}"
            print(line, flush=True)
            report.append(line)
            time.sleep(2)

    base_median = sorted(r["tps"] for r in results[32768])
    big = results[65536]
    verdict = "KEEP 32768"
    if not errors and len(big) == 3:
        med = base_median[len(base_median) // 2] if base_median else 0
        ok_tps = all(r["tps"] >= 0.5 * med for r in big)
        ok_vram = all(r["ps"]["size_vram_gb"] <= 15.5 for r in big)
        if ok_tps and ok_vram:
            verdict = "ADOPT 65536"
        else:
            verdict = f"KEEP 32768 (tps_ok={ok_tps}, vram_ok={ok_vram}, baseline_med={med})"
    elif errors:
        verdict = f"KEEP 32768 ({len(errors)} error(s) at 65536 or baseline)"

    kv_note = (
        "KV math (fp16): gpt-oss-20b ~48KB/token -> 32k ~= 1.5GB, 64k ~= 3.0GB; "
        "weights ~12.3GB on a 16GB card leaves ~2.5-3GB headroom."
    )
    print(verdict, flush=True)
    print(kv_note, flush=True)
    with open(out / "report.txt", "w", encoding="utf-8") as fh:
        fh.write(f"VERDICT: {verdict}\n{kv_note}\n")
        for line in report:
            fh.write(line + "\n")
        for e in errors:
            fh.write("ERROR " + e + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
