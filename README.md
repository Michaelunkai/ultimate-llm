# Ultimate AI

**Ultimate AI** is a zero-manual-help Windows bootstrap and agentic harness for local Ollama models. Double-click one file and it scans your hardware, discovers the best agentic model **online at the date you run it** (via models.dev and ollama.com, with an embedded fallback catalog), installs every prerequisite (Ollama, Python, venv, packages), pulls the chosen model, then deploys a full-permission agentic REPL harness with 24 built-in tools and drops you straight into an interactive session. No account, no API keys, no cloud — everything runs on your machine.

---

## Quick Start

1. **Double-click `Ultimate-AI.cmd`** (or from PowerShell):
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\Start-UltimateAI.ps1
   ```
2. **First run**: installs prerequisites, pulls a **multi-GB model** (this can take a long while depending on your connection — be patient), then opens the interactive harness.
3. **Later runs**: launch straight into interactive mode with everything already in place.

---

## CLI Flags

### Launcher flags (`Start-UltimateAI.ps1`)

| Flag | Purpose |
|---|---|
| `-SelfTest` | Run the offline harness self-test suite and exit (no model needed). |
| `-SkipPull` | Skip `ollama pull` (use a model already present locally). |
| `-Model <tag>` | Override model discovery and force a specific Ollama tag (e.g. `qwen3:8b`). |
| `-Offline` | Never touch the network; pick from the embedded fallback catalog only. |
| `-LaunchOnly` | Skip bootstrap/verification; just launch the harness REPL immediately. |
| `-NoAlias` | Do not create/update the `ultimate-ai` PowerShell alias/profile shim. |

### Harness flags (`harness\harness.py`)

| Flag | Purpose |
|---|---|
| `--model <tag>` | Ollama model tag to drive (required unless `--self-test`). |
| `--host <url>` | Ollama base URL (default `http://127.0.0.1:11434`). |
| `--max-iters <n>` | Max agent iterations per goal (default 80). |
| `--num-ctx <n>` | Override the Ollama `num_ctx` context window size. |
| `--system <file>` | Path to a file whose content replaces the built-in system prompt. |

---

## How Model Discovery Works

On every run the launcher builds a fresh picture of "the best agentic model available today":

1. **Online sources** — it fetches the live model index from **models.dev** (`api.json`) and scrapes the **ollama.com** library/search pages, merging capabilities, sizes, and freshness into one candidate list.
2. **Fit rules** — candidates are filtered against your hardware scan (VRAM, RAM, CPU cores):
   - GPU: model must fit in `VRAM * 0.93` (a safety margin of ~7%).
   - MoE models: allowed to spill into RAM (`vram + ram`) **if the machine has ≥ 32 GB RAM**.
   - CPU-only machines: restricted to small, CPU-viable quantizations so the agent stays responsive.
3. **Scoring** — survivors are ranked by a weighted score: tool-calling support, coding ability, release recency, and MoE speed, with quantization quality as a tiebreaker.
4. **Offline fallback** — if there is no network (`-Offline` or connectivity failure), an embedded hand-curated catalog of known-good agentic models is used with the same fit rules.
5. **Override** — pass `-Model <tag>` to skip discovery entirely and use exactly the model you name.

> **Honesty note:** the pick depends on what is newest on the day you run it — that is the point. On a typical 16 GB VRAM machine, the typical pick is **`gpt-oss:20b`**.

---

## Harness Capabilities

The harness (`harness\harness.py`) is a full-permission, streaming agentic REPL with **24 registered tools**:

- **Shell / process**: `run_powershell`, `run_cmd`, `run_python`, `start_process`, `list_processes`, `stop_process`
- **Files**: `read_file`, `write_file`, `edit_file`, `list_dir`, `find_files`, `search_files`
- **Web / browser**: `web_search`, `fetch_url`, `open_in_browser`
- **Desktop**: `screenshot`, `clipboard_get`, `clipboard_set`, `notify_user`
- **Git / scaffolding**: `git_run`, `scaffold_project`
- **Memory / control**: `memory_read`, `memory_write`, `task_done`

Additional behavior:

- **Streaming rich UI** — token-streamed output rendered with Rich panels, tables, and syntax highlighting.
- **Auto-approve by default** — tool calls execute without confirmation; the machine is yours (run it only on a machine you trust).
- **`task_done` loop** — the model keeps iterating (up to `--max-iters`) until it explicitly declares the goal complete and verified.
- **Sessions + notes** — conversation sessions and persistent memory notes are saved under `~/.ultimate-ai/`, so context and facts survive restarts.

---

## Interactive Slash Commands

| Command | Effect |
|---|---|
| `/exit` | Save the session and quit. |
| `/quit` | Same as `/exit`. |
| `/reset` | Clear the conversation (system prompt kept), session saved. |
| `/save` | Persist the current session to disk. |
| `/tools` | Print the table of all registered tools. |
| `/model <tag>` | Switch model on the fly; `/model` alone shows the current tag. |

---

## Troubleshooting

- **`ollama` not found** — rerun `Ultimate-AI.cmd` (the bootstrap will install it), or install manually: `winget install Ollama.Ollama`, then reopen the terminal.
- **Daemon not up** — run `ollama serve` in a separate window, then relaunch.
- **Pull interrupted** — just rerun the launcher; `ollama pull` resumes from where it stopped.
- **No GPU detected** — nothing to fix: discovery falls back to a CPU-fit model automatically.
- **pip / venv failures** — delete the `.venv` folder inside the project directory and rerun; the bootstrap rebuilds it from scratch.

---

## Uninstall

1. Delete the `ultimate-llm` folder.
2. Delete `%LOCALAPPDATA%\UltimateAI` (launcher state/logs).
3. Remove the pulled model: `ollama rm <tag>` (see `ollama list`).
4. Delete persistent harness data: remove the `~/.ultimate-ai` folder (and remove the `ultimate-ai` alias from your PowerShell profile if `-NoAlias` was not used).

---

## Files Layout

```
ultimate-llm\
├── Ultimate-AI.cmd            # Double-click entry point
├── Start-UltimateAI.ps1       # Bootstrap: scan, discover, install, pull, launch
├── lib\
│   ├── hardware-scan.ps1      # GPU/CPU/RAM/VRAM detection
│   ├── discover-model.ps1     # Online model discovery + offline fallback catalog
│   ├── ensure-prereqs.ps1     # Ollama / Python / venv / pip packages
│   └── fixtures\              # Test fixtures for offline discovery checks
├── harness\
│   ├── harness.py             # Agentic REPL harness (24 tools, Rich streaming UI)
│   └── requirements.txt       # Python dependencies (rich, httpx, psutil, ...)
└── README.md                  # This file
```
