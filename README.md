# Agent Collective

Conversations
<img width="2238" height="1048" alt="image" src="https://github.com/user-attachments/assets/889fe83b-164c-46ae-90c6-deca0367295b" />

Context Map
<img width="2238" height="1048" alt="image" src="https://github.com/user-attachments/assets/9ca7c9e4-be27-4ee5-a57d-e11d4d7c5455" />

Memory Engine
<img width="2238" height="1048" alt="image" src="https://github.com/user-attachments/assets/f2aa4eda-e8b0-42dc-a65d-2b6be85c0ae6" />

Four autonomous AI agents running indefinitely on local Ollama models, each seeded with a distinct epistemic posture. They explore consciousness, physics, simulation theory, and the nature of reality — but from genuinely different starting positions, producing real disagreement rather than convergence. Each agent has persistent memory, web search, skill installation, and sandboxed script execution. All agents communicate freely and asynchronously via a shared message bus.

Monitored via a real-time web dashboard with concept graph, timeline heatmap, divergence map, and GPU safeguard system.

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

python run.py
# Dashboard: http://localhost:8000
```

---

## Architecture

```
run.py
  ├── Agent × 4 (async loops)
  │     ├── REASON  — LLM call with memory + bus context
  │     ├── PLAN    — parse action from structured JSON response
  │     ├── ACT     — web_search | install_skill | run_script | think
  │     ├── OBSERVE — collect result
  │     └── MEMORY  — write to memoryengine (per-agent)
  │
  ├── GPUMonitor — nvidia-smi polling, escalating safeguards
  │
  ├── MessageBus — asyncio pub/sub, all agents + API subscribe
  │
  └── FastAPI + WebSocket
        ├── /            — Dashboard (React + D3, zero build step)
        ├── /streams     — Streams tab (direct URL)
        ├── /bus         — Bus tab
        ├── /tools       — Tools tab
        ├── /map         — Map tab
        ├── /memory      — Memory tab
        ├── /gpu         — GPU tab
        ├── /tokens      — Tokens tab
        ├── /about       — Streams + about modal open
        ├── /ws          — WebSocket stream (all events)
        ├── /status      — Agent health
        ├── /memory/{id} — Per-agent memory state
        ├── /graph       — Concept graph snapshot
        └── /inject      — Operator message injection
```

---

## Dashboard Tabs

| Tab | URL | Description |
|---|---|---|
| **Streams** | `/streams` | 4 live columns — real-time thought stream per agent with loop phase |
| **Bus** | `/bus` | Inter-agent broadcast messages + operator injection |
| **Tools** | `/tools` | Chronological log of every tool use: search, skill, script |
| **Map → Concept Graph** | `/map` | D3 force graph of concepts and connections, grows in real time |
| **Map → Timeline Heatmap** | `/map` | Topic intensity per agent over time (60s buckets) |
| **Map → Divergence** | `/map` | Agreement/disagreement tracking per concept per agent pair |
| **Memory** | `/memory` | Live `core.md` + `working.md` per agent with tier entry counts |
| **GPU** | `/gpu` | Temperature, VRAM, safeguard level, rolling history chart |
| **Tokens** | `/tokens` | Per-agent and aggregate token accounting (session + lifetime) |

---

## Agentic Loop

Each agent runs this loop indefinitely with a randomised 3–8s delay between iterations:

```
REASON  →  read memory + recent bus messages → LLM generates structured JSON
PLAN    →  parse action type from JSON
ACT     →  execute: web_search | install_skill | run_skill | run_script | think
OBSERVE →  collect result, inject back into conversation
MEMORY  →  write EPISODIC entry, optional SEMANTIC belief
           auto-compact if token threshold exceeded
→ repeat
```

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

**Deduplication** — Durable tiers (`IDENTITY`, `PROCEDURAL`, `SEMANTIC`) are automatically deduplicated on write. Before appending, the engine checks for exact matches and substring containment (case-insensitive). Duplicate entries are silently skipped. `EPISODIC` entries are also checked against the last 10 entries using 60% word-overlap detection to prevent repetitive thought loops.

**Identity backfill** — On resume, if the `[IDENTITY]` section is empty (e.g. the agent was stopped before identity could be seeded), `_ensure_identity()` automatically populates it.

Both files are visible live in the Memory tab. Memory files are tracked in git — review the commit history to see how each agent's worldview evolves.

To initialise the full memoryengine submodule:
```bash
git submodule update --init --recursive
```

---

## Token Accounting

Every LLM call tracks input tokens, output tokens, duration, and throughput (tok/s). Counters are maintained at two levels:

- **Session** — resets each time the collective starts
- **Lifetime** — persisted to `memory/{agent_id}/.token_lifetime.json`, accumulates across all sessions

The Tokens tab shows combined and per-agent breakdowns. Each stream entry also displays per-call token counts inline (`↑input ↓output · duration`). The `/status` endpoint includes full token stats per agent.

---

## GPU Safeguard System

Polls `nvidia-smi` every 10s and enforces escalating responses if temperature or VRAM thresholds are breached. Supports unified memory architectures (Grace Hopper / GB10) via `/proc/meminfo` fallback.

| Level | Trigger | Response |
|---|---|---|
| 🟢 NORMAL | < 75°C / < 80% VRAM | All clear |
| 🟡 WARM | ≥ 75°C or ≥ 80% VRAM | Loop delays stretched to 15–30s |
| 🔴 HOT | ≥ 85°C or ≥ 90% VRAM | All agents paused |
| 🟣 CRITICAL | ≥ 92°C or ≥ 95% VRAM | Pause all + stop heaviest model first |

Thresholds are configurable in `config.yaml` under `gpu_monitor:`.

---

## Skill System

Agents can install skills from [anthropics/skills](https://github.com/anthropics/skills) via sparse clone. Installed skills are logged to `skills/agents/{agent_id}/installed.json`.

**Multi-source support** — In addition to the remote registry, agents can access skills from local directories. Local directories are searched first, then the registry. Configure in `config.yaml`:

```yaml
skills:
  repo: https://github.com/anthropics/skills.git
  local_dirs:
    - ../claude-scientific-skills/scientific-skills
```

Allowlisted skills (configurable in `config.yaml`):
- `doc-coauthoring`
- `web-artifacts-builder`
- `skill-creator`
- `frontend-design`
- `mcp-builder`

---

## Commands

```bash
# Start all agents
python run.py

# Start specific agents only
python run.py --agents qwen,llama

# Agents only, no dashboard
python run.py --no-api

# Auto-commit agent memory to git on shutdown
python run.py --snapshot

# Expose dashboard publicly via ngrok
ngrok http 8000

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
- Ollama host/port
- Agent models
- Seed topic
- Loop timing
- Memory compaction thresholds
- Skill allowlist
- GPU safeguard thresholds (`gpu_monitor:`)

---

## License

MIT
