# Improvements — October 2026 round (scout triage)

Findings from the always-on research scouts (S-A models, S-B ollama capabilities,
S-C harness/UX), each triaged APPLIED / REJECTED with reason and source.

| # | Finding | Verdict | Where / why |
|---|---|---|---|
| 1 | Scroll-stealing progress: conhost snaps to bottom on **new-line** writes; WT never yanks. In-place `\r`+`ESC[K` rewrites + OSC title are scroll-safe in both. | **APPLIED** | harness.py `ProgressEngine` (title + in-place status + audit); verified by T-B/T-C proof (viewport frozen 30s while 23 ticks continued). Sources: microsoft/terminal#10191, #8879; MS VT sequences doc. |
| 2 | OSC 9;4 progress sequence → tab + Windows taskbar progress ring (WT 1.6+, ignored elsewhere). | **APPLIED** | `ProgressEngine.start/stop` emit `ESC]9;4;3` / `ESC]9;4;0`. Source: MS Terminal progress-bar-sequences tutorial. |
| 3 | `OLLAMA_KV_CACHE_TYPE=q8_0` (halves KV memory, "very small loss"; requires flash attention). | **REJECTED (this machine, measured)** | A/B on second daemon (:11435, ollama 0.40.3, gpt-oss:20b, 32k ctx): 101–106 tok/s vs 156–162 baseline (−34%), 0/3 correct on the counting probe, VRAM 15834 vs 14934 MiB. Fails the ≥15% VRAM-or-≥10%-faster AND 3/3-correct gate. Revisit if a future ollama build enables FA+KV-quant properly on this GPU. Source: docs.ollama.com/faq. |
| 4 | Model landscape (Oct 2026): gpt-oss:20b still the only frontier-agentic model fully in 16GB VRAM (~150 tok/s); qwen3.6:35b-A3B beats it on SWE-V only via CPU expert-offload (10–17 tok/s, llama.cpp-only); devstral-small-2 SWE-V 65.8% but 15GB dense. | **APPLIED (catalog)** | Embedded catalog + 5 scout entries (qwen3.6:35b, qwen3.8:27b, glm-4.7-flash, laguna-xs.2, devstral-small-2:24b) sized so the in-VRAM winner is unchanged on 16GB. Sources: ollama.com/library, qwen.ai, artificialanalysis.ai, arXiv 2508.10925. |
| 5 | Streaming tool calls (blog May 2025; docs say "partial tool calls"): current ToolCallAssembler (index-keyed merge) handles both complete-object and fragment streams. | **APPLIED (no change needed)** | Verified assembler handles fragments (self-test case) and complete objects (M01–M12 battery, T-D session). Sources: ollama.com/blog/streaming-tool, docs.ollama.com/api/chat. |
| 6 | `/api/ps` endpoint: loaded models + `size_vram` + expiry — ideal for /doctor. | **APPLIED** | `/doctor` health check (W5). Source: github.com/ollama/ollama docs/api.md. |
| 7 | `think` request field levels ("low"/"medium"/"high") + /api/show thinking controls (v0.34.3+). | **NOTED — deferred** | gpt-oss already returns `message.thinking` and the harness round-trips it; per-request think-level control deferred (no current demand; avoid mid-certification churn). |
| 8 | Preserve assistant `thinking` + `tool_calls` verbatim in history (community consensus for gpt-oss). | **APPLIED (already present)** | harness.py rounds `STATE["last_thinking"]` back into the assistant message (pre-existing scout finding from the 10-11 round). |
| 9 | WT scroll marks via OSC 133;A/D per turn (stable WT 1.21+; ignored elsewhere). | **DEFERRED** | Zero-cost but only benefits WT users with marks enabled; revisit next round. Source: MS shell-integration tutorial, microsoft/terminal#11000. |
| 10 | Synchronized output DECSET 2026 (WT 1.23+). | **REJECTED** | Our status update is a single short line write; no tearing to prevent. |
| 11 | Claude-code style rewind/checkpoint UX; aider git-undo; codex /status //doctor //compact surface. | **APPLIED (adapted)** | /undo (conversation snapshots), /doctor, /verify, /usage, /compact + auto-compaction — see W5/W6. |
| 12 | Ollama daemon auto-updates itself; harness should not assume version. | **APPLIED** | /doctor reads /api/version live. |
| 13 | `ESC[3J` scrollback wipe is WT-only and destructive; never auto-emit. `/swipe` uses `2J+3J+H` only on explicit user command. | **APPLIED** | /swipe handler (W5). Source: microsoft/terminal#19086. |

Raw scout reports with full source lists: session archive (2026-10-12), evidence
bundles under missions/logs/. VRAM A/B raw data: this file row 3 + baseline
summary in missions/logs/baseline-20261012-003125/.

## Never-stuck hardening (live-session feedback, 11:05 session)

| # | Finding | Verdict | Where |
|---|---|---|---|
| 14 | Pasting a long prompt echoed one stacked wrapped copy PER CHARACTER (33+ lines of garbage that looks like a freeze). Root cause: per-char full redraw where `\r`+`ESC[K` only clears ONE physical row once the line wraps. | **FIXED** | prompt_with_history: redraw erases the whole wrapped block (cursor-up + erase-to-end), 20 ms coalescing throttle + 50 ms flusher thread. Proof: tools/paste_proof.py — 275-char instant paste renders as exactly ONE line (1 prompt glyph), model answers, exit 0. |
| 15 | First message after idle sat silent: cold model load (~30-90 s) with a static status line and a stream that would never time out. | **FIXED (3 layers)** | (a) honest status via /api/ps ("cold-loading weights", "partial residency"); (b) stream stall watchdog — reader thread + watcher, kills + retries a stream silent beyond CONFIG stream_stall_s (45 s), with cold-load patience to 240 s so a legitimate load is never killed (zero-kill proven); (c) keep_alive 24h on chat + launcher warm-up so weights stay resident all day. Proof: dead-server test (11 kills, beats every ≤3.03 s, clean exit), cold-start M03 (12 notes, 0 kills, verified 1/1, max gap 3.01 s). |
