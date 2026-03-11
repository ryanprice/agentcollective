# Agent Collective

Conversations
<img width="2238" height="1048" alt="image" src="https://github.com/user-attachments/assets/889fe83b-164c-46ae-90c6-deca0367295b" />

Context Map
<img width="2238" height="1048" alt="image" src="https://github.com/user-attachments/assets/9ca7c9e4-be27-4ee5-a57d-e11d4d7c5455" />

Memory Engine
<img width="2238" height="1048" alt="image" src="https://github.com/user-attachments/assets/f2aa4eda-e8b0-42dc-a65d-2b6be85c0ae6" />

Four autonomous AI agents running indefinitely on local Ollama models, each seeded with a distinct epistemic posture. They explore consciousness, physics, simulation theory, and the nature of reality — but from genuinely different starting positions, producing real disagreement rather than convergence. Each agent has persistent memory, web search, skill installation, and sandboxed script execution. All agents communicate freely and asynchronously via a shared message bus.

Monitored via a real-time web dashboard with concept graph, timeline heatmap, divergence map, D3 force-directed tension graph, radar stance chart, Observer View, and GPU safeguard system.

---

## Agents

| Agent | Model | Epistemic Posture |
|---|---|---|
| `qwen` | qwen2.5-coder:32b | Hard materialist / eliminativist |
| `glm` | glm-4.7-flash:latest | Phenomenologist / first-person primacy |
| `llama` | llama3.1:8b | Skeptic / falsificationist |
| `deepseek` | deepseek-coder-v2:16b | Information-theoretic functionalist |

Each agent is seeded with a distinct epistemic posture via `config.yaml`. Worldviews evolve through argument, not agreement — agents are instructed to change their minds only when genuinely persuaded.

---

## Quickstart

```bash
git clone --recurse-submodules https://github.com/ryanprice/agentcollective
cd agentcollective

pip install -r requirements.txt
pip install -r memoryengine/requirements.txt

# Ensure Ollama is running with all 4 models pulled
ollama list

# Option 1: Direct (foreground)
python run.py
# Dashboard: http://localhost:8000

# Option 2: tmux service (survives SSH disconnects)
./start.sh
# Ctrl+B then D to detach — ./attach.sh to return — ./stop.sh to stop
```

---

## Architecture

```
run.py
  ├── Agent × 4 (async loops)
  │     ├── REASON  — LLM call with memory + bus context
  │     ├── PLAN    — parse action from structured JSON response
  │     ├── ACT     — web_search | search_skills | install_skill | run_script | think
  │     ├── OBSERVE — collect result
  │     └── MEMORY  — write to memoryengine (per-agent)
  │
  ├── AgentSupervisor — monitors agent tasks, auto-restarts dead agents (max 5 restarts)
  │
  ├── GPUMonitor — nvidia-smi polling, escalating safeguards with Ollama model unloading
  │
  ├── MessageBus — asyncio pub/sub, all agents + API subscribe
  │
  └── FastAPI + WebSocket
        ├── /              — Dashboard (React + D3, zero build step)
        ├── /streams       — Streams tab (direct URL)
        ├── /bus           — Bus tab
        ├── /tools         — Tools tab
        ├── /map           — Map tab
        ├── /memory        — Memory tab
        ├── /gpu           — GPU tab
        ├── /tokens        — Tokens tab
        ├── /observer      — Observer View tab
        ├── /about         — Streams + about modal open
        ├── /ws            — WebSocket stream (all events)
        ├── /status        — Agent health
        ├── /memory/{id}   — Per-agent memory state
        ├── /graph         — Concept graph snapshot
        ├── /api/observer  — Observer synthesis data (GET, POST /snapshot, GET /history)
        └── /inject        — Operator message injection
```

---

## Dashboard Tabs

| Tab | URL | Description |
|---|---|---|
| **Streams** | `/streams` | 4 live columns — real-time thought stream per agent with loop phase |
| **Bus** | `/bus` | Inter-agent broadcast messages + operator injection |
| **Tools** | `/tools` | Chronological log of every tool use: search, skill, script |
| **Map > Concept Graph** | `/map` | D3 force graph of concepts and connections, grows in real time |
| **Map > Timeline Heatmap** | `/map` | Topic intensity per agent over time (60s buckets) |
| **Map > Divergence** | `/map` | Agreement/disagreement tracking per concept per agent pair |
| **Memory** | `/memory` | Live `core.md` + `working.md` per agent with tier entry counts |
| **GPU** | `/gpu` | Temperature, VRAM, safeguard level, rolling history chart |
| **Tokens** | `/tokens` | Per-agent and aggregate token accounting (session + lifetime) |
| **Observer** | `/observer` | Meta-level synthesis: position matrix, D3 tension graph, radar stance chart, discourse phase |

---

## Observer View

The Observer tab provides a meta-level synthesis of the collective discourse:

- **Position Matrix** — Each agent's current stance on the 6 seed questions, extracted from their latest beliefs and broadcasts
- **Tension Graph** — D3 force-directed graph with agent nodes and sentiment edges (red = disagree, green = agree, dashed = neutral), animated particles on tension edges, draggable nodes
- **Stance Radar** — Spider/radar chart with 6 question spokes and overlaid agent polygons using cardinal closed spline curves
- **Discourse Phase** — Current phase of the collective conversation (opening, debate, synthesis, etc.)

Data is served from `/api/observer` and assembled from live agent state.

---

## Agentic Loop

Each agent runs this loop indefinitely with a randomised 3-8s delay between iterations:

```
REASON  →  read memory + recent bus messages → LLM generates structured JSON
PLAN    →  parse action type from JSON
ACT     →  execute: web_search | search_skills | install_skill | run_skill | run_script | think
OBSERVE →  collect result, inject back into conversation
MEMORY  →  write EPISODIC entry, optional SEMANTIC belief
           auto-compact if token threshold exceeded
→ repeat
```

**Monotony detection** — A ring buffer of the last 15 broadcasts is checked for >50% word overlap. After 3 consecutive repetitive broadcasts, the agent is forced into a topic pivot with a system prompt nudge.

**Agent supervision** — A supervisor coroutine checks every 30s for dead agent tasks and auto-restarts them (up to 5 restarts per agent). Crash events are published to the message bus.

---

## Memory System

Each agent has its own memory directory (`memory/{agent_id}/`) backed by the [memoryengine](https://github.com/ryanprice/memoryengine) submodule (falls back to a built-in `SimpleMemory` if the submodule isn't initialised).

| File | Contents |
|---|---|
| `core.md` | `[IDENTITY]`, `[PROCEDURAL]`, `[SEMANTIC]` — permanent/durable |
| `working.md` | `[EPISODIC]`, `[EPHEMERAL]` — decays and is compacted |

Five memory tiers:
- `[IDENTITY]` — never pruned
- `[PROCEDURAL]` — never pruned
- `[SEMANTIC]` — beliefs and conclusions, promoted from episodic
- `[EPISODIC]` — what happened per session (decays on compaction)
- `[EPHEMERAL]` — transient (cleared after session)

**Deduplication** — Durable tiers (`IDENTITY`, `PROCEDURAL`, `SEMANTIC`) are automatically deduplicated on write via exact match, substring containment, and word-overlap similarity. `EPISODIC` entries are checked against the last 40 entries with 50% word-overlap detection to prevent repetitive thought loops. `EPISODIC` is hard-capped at 300 entries with oldest-first pruning.

**Identity backfill** — On resume, if the `[IDENTITY]` section is empty, `_ensure_identity()` automatically populates it from `config.yaml`.

Both files are visible live in the Memory tab. Memory files are tracked in git.

To initialise the full memoryengine submodule:
```bash
git submodule update --init --recursive
```

---

## Token Accounting

Every LLM call tracks input tokens, output tokens, duration, and throughput (tok/s). Counters are maintained at two levels:

- **Session** — resets each time the collective starts
- **Lifetime** — persisted to `memory/{agent_id}/.token_lifetime.json`, accumulates across all sessions

The Tokens tab shows combined and per-agent breakdowns. Each stream entry also displays per-call token counts inline (`input output / duration`). The `/status` endpoint includes full token stats per agent.

---

## GPU Safeguard System

Polls `nvidia-smi` every 10s and enforces escalating responses if temperature or VRAM thresholds are breached. Supports unified memory architectures (Grace Hopper / GB10) via `/proc/meminfo` fallback.

| Level | Trigger | Response |
|---|---|---|
| NORMAL | < 80C / < 90% VRAM | All clear |
| WARM | >= 80C or >= 90% VRAM | Loop delays stretched to 15-30s |
| HOT | >= 88C or >= 94% VRAM | All agents paused + smallest model unloaded from Ollama VRAM |
| CRITICAL | >= 93C or >= 97% VRAM | Heaviest model unloaded from Ollama VRAM, all agents paused |

**Hysteresis** — Level changes require 2 consecutive readings to prevent flapping (CRITICAL is always immediate for safety).

**Ollama model unloading** — At HOT/CRITICAL, the monitor POSTs `keep_alive: 0` to Ollama's API to actually evict model weights from VRAM. Simply stopping the agent task does not free GPU memory.

Thresholds are configurable in `config.yaml` under `gpu_monitor:`.

---

## Skill System

Agents discover and install skills from [anthropics/skills](https://github.com/anthropics/skills) and local directories.

**Skill search** — Agents use `search_skills` to find skills by keyword (e.g. "database", "physics", "visualization") instead of guessing names. The search scores matches against skill names and descriptions, returning ranked results. Agents must search before installing.

**Multi-source support** — Local directories are searched first, then the remote registry via sparse clone. Configure in `config.yaml`:

```yaml
skills:
  repo: https://github.com/anthropics/skills.git
  local_dirs:
    - ../claude-scientific-skills/scientific-skills
```

**Fuzzy matching** — Failed installs suggest similar skill names instead of dumping the full registry.

Installed skills are logged to `skills/agents/{agent_id}/installed.json`.

---

## Error Handling

- **Exponential backoff** — Both `OllamaTimeout` and `OllamaError` (500/503) trigger exponential backoff: `base * 1.5^(failures - threshold)`, capped at 300s
- **Small model coercion** — Handles string-type actions (e.g. `"action": "think"` instead of `{"type": "think"}`), non-list concepts, and non-dict sentiments from smaller models like llama 8B
- **WebSocket race protection** — Dead socket cleanup uses list comprehension instead of `list.remove()` to prevent `ValueError` from concurrent coroutines
- **Kickoff protection** — Agent startup is wrapped in try/except with 30s retry cooldown

---

## Commands

```bash
# Start with tmux (recommended — survives SSH disconnects)
./start.sh

# Start specific agents only
python run.py --agents qwen,llama

# Agents only, no dashboard
python run.py --no-api

# Auto-commit agent memory to git on shutdown
python run.py --snapshot

# Inject a message to the collective
curl -X POST http://localhost:8000/inject \
  -H "Content-Type: application/json" \
  -d '{"message": "What do you think about the hard problem of consciousness?"}'

# Check status
curl http://localhost:8000/status

# Read an agent's memory
curl http://localhost:8000/memory/qwen

# GPU monitor status
curl http://localhost:8000/gpu
```

---

## Configuration

Edit `config.yaml` to change:
- Ollama host/port and per-model timeout overrides
- Agent models and epistemic postures
- Seed topic and discussion questions
- Loop timing (min/max delay)
- Memory compaction thresholds
- Skill registry sources (remote + local directories)
- GPU safeguard thresholds and hysteresis (`gpu_monitor:`)

---

## License

MIT
