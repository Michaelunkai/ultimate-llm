#!/usr/bin/env python3
"""Real-prompt context-window proof (the decisive test for max num_ctx).

ctx_probe.py sends a tiny prompt, which only proves idle VRAM sizing. The
2026-10-12 proof showed a REAL 60k-token prompt crashes the runner at 65536
with fp16 KV even though idle probes pass. This driver sends REAL large
prompts with needle questions (comprehension check) and decides:

  ADOPT 65536  iff the 65536 real-prompt runs all succeed, answer the needle
               correctly, and /api/ps VRAM stays <= 15.5 GB.
  KEEP 32768   otherwise (any error, wrong needle, VRAM blowout).

On ADOPT it writes .ctx-proof.json at the project root, which the harness
picks up at startup to maximize the context window permanently. Pure HTTP
against the running daemon - no daemon restart, no state change.

Run AFTER the daemon was started with OLLAMA_FLASH_ATTENTION=1 and
OLLAMA_KV_CACHE_TYPE=q8_0 (see lib/ensure-prereqs.ps1): q8_0 KV is what
halves KV memory enough for the 65536 window on a 16GB card.
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

NEEDLE = "47.83"

_PARA = (
    "Distributed inference schedulers balance prefill and decode phases across workers by "
    "estimating per-batch memory pressure, reordering queues when the cache hit ratio drops, "
    "and throttling admission so tail latency stays bounded. Section {i} repeats this contract: "
    "the scheduler must never evict a resident batch to admit a newer one while the running "
    "sum of reserved KV blocks exceeds the soft ceiling, and it must log every such decision "
    "with the batch identifier, the block count, and the measured pressure delta. "
)


def build_prompt(target_tokens: int) -> str:
    # gpt-oss tokenizer compresses this repetitive text to ~5.4 chars/token;
    # 5.6 leaves margin without risking num_ctx overflow at 65536.
    chars = int(target_tokens * 5.6)
    needle_para = (
        "Calibration note: the quantum flux calibration constant for this document is "
        f"{NEEDLE}, recorded during the commissioning run and never revised. "
    )
    needle_at = int(chars * 0.55)
    parts = []
    total = 0
    i = 0
    inserted = False
    while total < chars:
        i += 1
        para = _PARA.format(i=i)
        if not inserted and total >= needle_at:
            para = needle_para + para
            inserted = True
        parts.append(para)
        total += len(para)
    body = "".join(parts)
    if not inserted:  # degenerate target - force it
        body = needle_para + body
    return (
        body
        + "\n\nQuestion: what is the quantum flux calibration constant stated in this document? "
        "Answer with just the number."
    )


def chat(num_ctx: int, prompt: str, num_predict: int = 256) -> dict:
    payload = {
        "model": MODEL,
        "stream": False,
        "messages": [{"role": "user", "content": prompt}],
        "keep_alive": "24h",
        "options": {"num_ctx": num_ctx, "num_predict": num_predict, "temperature": 0.6},
    }
    with httpx.Client(timeout=httpx.Timeout(900.0, connect=10.0)) as client:
        t0 = time.time()
        resp = client.post(HOST + "/api/chat", json=payload)
        wall = time.time() - t0
    resp.raise_for_status()
    data = resp.json()
    message = data.get("message") or {}
    content = str(message.get("content", ""))
    thinking = str(message.get("thinking") or "")
    return {
        "wall": round(wall, 1),
        "prompt_tokens": int(data.get("prompt_eval_count") or 0),
        "eval_count": int(data.get("eval_count") or 0),
        "tps": round(int(data.get("eval_count") or 0) / (float(data.get("eval_duration") or 1) / 1e9), 1),
        "needle_ok": NEEDLE in content or NEEDLE in thinking,
        "answer": (content.strip() or ("[thinking] " + thinking.strip()))[:120],
    }


def ps() -> float:
    with httpx.Client(timeout=10.0) as client:
        r = client.get(HOST + "/api/ps")
        for m in r.json().get("models", []):
            if m.get("name") == MODEL or m.get("model") == MODEL:
                return round(m.get("size_vram", 0) / 1e9, 2)
    return 0.0


def daemon_alive() -> bool:
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.get(HOST + "/api/ps")
            return r.status_code == 200
    except Exception:
        return False


def main() -> int:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = ROOT / "missions" / "logs" / f"ctx-proof-{ts}"
    out.mkdir(parents=True, exist_ok=True)
    report = []

    def log(line: str) -> None:
        print(line, flush=True)
        report.append(line)

    prompt32 = build_prompt(25000)  # baseline must FIT 32768
    prompt64 = build_prompt(60000)  # the real 60k stress
    results = {}
    errors = []

    log(f"ctx-proof start {ts} | baseline prompt ~25k tokens, stress prompt ~60k tokens")
    for label, nc, prompt in (("baseline-32768", 32768, prompt32), ("stress-65536", 65536, prompt64)):
        runs = []
        tries = (1, 2) if nc == 32768 else (1, 2, 3)
        for i in tries:
            try:
                r = chat(nc, prompt)
                r["vram_gb"] = ps()
                runs.append(r)
                log(
                    f"{label} run{i}: {r['prompt_tokens']} prompt tok, {r['tps']} tok/s, "
                    f"wall {r['wall']}s, vram {r['vram_gb']}GB, needle_ok={r['needle_ok']} "
                    f"(answer: {r['answer']!r})"
                )
            except Exception as exc:
                errors.append(f"{label} run{i}: {type(exc).__name__}: {str(exc)[:200]}")
                log(f"{label} run{i}: ERROR {type(exc).__name__}: {str(exc)[:200]}")
                if not daemon_alive():
                    log("DAEMON DOWN after error - this is the proven crash mode; stop probing.")
                    break
                time.sleep(3)
        results[label] = runs
        time.sleep(2)

    big = results.get("stress-65536") or []
    base_ok = bool(results.get("baseline-32768"))
    ok_all = bool(big) and len(big) == 3 and all(r["needle_ok"] for r in big)
    ok_stress_size = bool(big) and all(r["prompt_tokens"] >= 55000 for r in big)
    ok_vram = all(r["vram_gb"] <= 15.5 for r in big)
    if errors and not daemon_alive():
        verdict = "KEEP 32768 (daemon died - restart with ONE 'ollama serve')"
    elif ok_all and ok_stress_size and ok_vram and base_ok:
        verdict = "ADOPT 65536"
    else:
        verdict = (
            f"KEEP 32768 (all_ok={ok_all}, stress_size_ok={ok_stress_size}, "
            f"vram_ok={ok_vram}, baseline_ok={base_ok}, errors={len(errors)})"
        )

    log(f"VERDICT: {verdict}")
    proof_path = ROOT / ".ctx-proof.json"
    if verdict == "ADOPT 65536":
        proof = {
            "model": MODEL,
            "num_ctx": 65536,
            "proven": "real-prompt needle comprehension, VRAM<=15.5GB",
            "proven_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "kv_env": "OLLAMA_FLASH_ATTENTION=1; OLLAMA_KV_CACHE_TYPE=q8_0",
            "proof_log": str(out),
        }
        proof_path.write_text(json.dumps(proof, indent=2), encoding="utf-8")
        log(f"WROTE {proof_path} - harness will adopt 65536 at startup.")
    else:
        log(f"no proof written ({proof_path.name} absent/unchanged) - harness keeps 32768.")

    with open(out / "report.txt", "w", encoding="utf-8") as fh:
        fh.write(f"VERDICT: {verdict}\n")
        for line in report:
            fh.write(line + "\n")
        for e in errors:
            fh.write("ERROR " + e + "\n")
    print(f"report: {out / 'report.txt'}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
