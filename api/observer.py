"""
Observer — Algorithmic synthesis of collective discourse.

Collects beliefs, sentiments, and events from all agents to produce
a structured "outside observer" view. The natural-language synthesis
paragraph is generated periodically by a lightweight LLM call;
everything else is pure algorithmic data extraction.
"""

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("observer")

# ── Snapshot storage ──────────────────────────────────────────────────────────

OBSERVER_DIR = Path("logs/observer")

def _ensure_dir():
    OBSERVER_DIR.mkdir(parents=True, exist_ok=True)


def save_snapshot(snapshot: dict):
    """Persist a versioned observer snapshot to disk."""
    _ensure_dir()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = OBSERVER_DIR / f"snapshot_{ts}.json"
    path.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")
    # Also write latest
    latest = OBSERVER_DIR / "latest.json"
    latest.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")
    return str(path)


def load_latest() -> dict | None:
    """Load the most recent observer snapshot."""
    latest = OBSERVER_DIR / "latest.json"
    if latest.exists():
        try:
            return json.loads(latest.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def list_snapshots(limit: int = 50) -> list[dict]:
    """List available observer snapshots with metadata."""
    _ensure_dir()
    files = sorted(OBSERVER_DIR.glob("snapshot_*.json"), reverse=True)[:limit]
    results = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            results.append({
                "filename": f.name,
                "timestamp": data.get("timestamp", ""),
                "loop":      data.get("loop", 0),
                "phase":     data.get("phase", "unknown"),
            })
        except Exception:
            continue
    return results


# ── Algorithmic data extraction ──────────────────────────────────────────────

QUESTIONS = [
    "What is consciousness?",
    "Quantum mechanics + consciousness?",
    "Simulation hypothesis?",
    "Can AI be conscious?",
    "DMT entities?",
    "Hard problem of consciousness?",
]


def build_observer_data(agents: dict, bus_history: list[dict]) -> dict:
    """
    Build the full observer data structure from live agent state.
    Pure algorithmic — no LLM call.
    """
    agent_ids = list(agents.keys())
    now = time.time()

    # ── Extract beliefs from SEMANTIC memory ──────────────────────────
    beliefs = {}
    for aid, agent in agents.items():
        try:
            core = agent.memory.read_core() if agent.memory else ""
            beliefs[aid] = _extract_semantic_entries(core)
        except Exception:
            beliefs[aid] = []

    # ── Extract identities ────────────────────────────────────────────
    identities = {}
    for aid, agent in agents.items():
        identities[aid] = {
            "posture": getattr(agent, "posture", ""),
            "model": agent.model,
            "color": agent.color,
        }

    # ── Build sentiment matrix from recent events ─────────────────────
    sentiment_matrix = _build_sentiment_matrix(bus_history, agent_ids)

    # ── Extract key moments (phase transitions, direct challenges) ────
    key_moments = _extract_key_moments(bus_history, agent_ids)

    # ── Build active tensions from disagreements ──────────────────────
    tensions = _extract_tensions(sentiment_matrix, bus_history, agent_ids)

    # ── Compute discourse phase ───────────────────────────────────────
    phase = _compute_discourse_phase(bus_history, sentiment_matrix, beliefs)

    # ── Build position matrix (best-effort from beliefs + concepts) ───
    position_matrix = _build_position_matrix(beliefs, identities, bus_history, agent_ids)

    # ── Unresolved questions ──────────────────────────────────────────
    unresolved = _extract_unresolved(bus_history, agent_ids)

    # ── Loop count ────────────────────────────────────────────────────
    max_loop = max(
        (getattr(a, "_loop_count", 0) for a in agents.values()),
        default=0,
    )

    snapshot = {
        "timestamp":       datetime.now().isoformat(),
        "loop":            max_loop,
        "phase":           phase,
        "identities":      identities,
        "beliefs":         {aid: bs[-10:] for aid, bs in beliefs.items()},
        "position_matrix": position_matrix,
        "sentiment_matrix": sentiment_matrix,
        "tensions":        tensions,
        "key_moments":     key_moments[-15:],
        "unresolved":      unresolved,
        "synthesis":       None,  # filled by LLM synthesis worker
        "agent_count":     len(agents),
        "event_count":     len(bus_history),
    }

    return snapshot


def _extract_semantic_entries(core_text: str) -> list[str]:
    """Pull entries from ## [SEMANTIC] section."""
    if "## [SEMANTIC]" not in core_text:
        return []
    start = core_text.index("## [SEMANTIC]") + len("## [SEMANTIC]")
    next_h = re.search(r"\n## \[", core_text[start:])
    end = start + next_h.start() if next_h else len(core_text)
    section = core_text[start:end]
    entries = []
    for line in section.splitlines():
        line = line.strip()
        if line.startswith("- "):
            # Strip timestamp prefix
            m = re.match(r"^- \[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]\s*", line)
            if m:
                entries.append(line[m.end():].strip())
            else:
                entries.append(line[2:].strip())
    return entries


def _build_sentiment_matrix(events: list[dict], agent_ids: list[str]) -> dict:
    """
    Build an agent × agent sentiment matrix from sentiment_toward fields.
    Returns: { "qwen": { "glm": "agree", "llama": "neutral", ... }, ... }
    Uses most recent sentiment for each pair.
    """
    matrix = {aid: {} for aid in agent_ids}
    for event in events:
        aid = event.get("agent_id", "")
        if aid not in agent_ids:
            continue
        sentiments = event.get("agreements", {}) or event.get("sentiment_toward", {})
        if sentiments:
            for target, stance in sentiments.items():
                if target in agent_ids:
                    matrix[aid][target] = stance
    return matrix


def _extract_key_moments(events: list[dict], agent_ids: list[str]) -> list[dict]:
    """Extract significant events: beliefs, challenges, searches."""
    moments = []
    seen_beliefs = set()

    for event in events:
        aid = event.get("agent_id", "")
        if aid not in agent_ids:
            continue

        phase = event.get("phase", "")
        thought = event.get("thought", "")[:200]
        publish = event.get("publish", "")

        # Belief crystallisation
        if phase == "memory" and "belief" in str(event.get("extra", {})):
            if thought not in seen_beliefs:
                seen_beliefs.add(thought)
                moments.append({
                    "type": "belief",
                    "agent": aid,
                    "text": thought,
                    "loop": event.get("loop", 0),
                    "ts": event.get("ts", 0),
                })

        # Direct challenge (mentions another agent)
        if publish:
            for other in agent_ids:
                if other != aid and other in publish.lower():
                    moments.append({
                        "type": "challenge",
                        "agent": aid,
                        "target": other,
                        "text": publish[:200],
                        "loop": event.get("loop", 0),
                        "ts": event.get("ts", 0),
                    })
                    break

        # Searches
        action = event.get("extra", {}).get("action", {}) if isinstance(event.get("extra"), dict) else {}
        if isinstance(action, dict) and action.get("type") == "search":
            moments.append({
                "type": "search",
                "agent": aid,
                "text": f"Searched: {action.get('query', '')[:100]}",
                "loop": event.get("loop", 0),
                "ts": event.get("ts", 0),
            })

    return moments


def _extract_tensions(sentiment_matrix: dict, events: list[dict], agent_ids: list[str]) -> list[dict]:
    """Find pairs with strong disagreement."""
    tensions = []
    seen_pairs = set()

    for a in agent_ids:
        for b in agent_ids:
            if a >= b:
                continue
            pair = (a, b)
            if pair in seen_pairs:
                continue

            a_to_b = sentiment_matrix.get(a, {}).get(b, "neutral")
            b_to_a = sentiment_matrix.get(b, {}).get(a, "neutral")

            if a_to_b == "disagree" or b_to_a == "disagree":
                heat = "high" if a_to_b == "disagree" and b_to_a == "disagree" else "medium"
                # Find most recent publish between them
                context = ""
                for event in reversed(events):
                    eid = event.get("agent_id", "")
                    pub = event.get("publish", "")
                    if eid in pair and pub:
                        context = pub[:200]
                        break

                tensions.append({
                    "agents": list(pair),
                    "heat": heat,
                    "context": context,
                    "a_stance": a_to_b,
                    "b_stance": b_to_a,
                })
                seen_pairs.add(pair)

    # Sort by heat
    heat_order = {"high": 0, "medium": 1, "low": 2}
    tensions.sort(key=lambda t: heat_order.get(t["heat"], 3))
    return tensions[:5]


def _compute_discourse_phase(events: list[dict], sentiments: dict, beliefs: dict) -> str:
    """Determine what phase the discourse is in."""
    total_events = len(events)
    total_beliefs = sum(len(bs) for bs in beliefs.values())

    # Count disagreements
    disagree_count = sum(
        1 for a in sentiments.values()
        for stance in a.values()
        if stance == "disagree"
    )

    # Count direct references to other agents
    cross_refs = 0
    agents = list(sentiments.keys())
    for event in events:
        pub = event.get("publish", "") or ""
        for a in agents:
            if a in pub.lower() and event.get("agent_id") != a:
                cross_refs += 1

    if total_events < 10:
        return "initialising"
    if cross_refs < 3 and disagree_count < 2:
        return "staking positions"
    if disagree_count >= 2 and total_beliefs < 8:
        return "active debate"
    if total_beliefs >= 8 and disagree_count >= 3:
        return "testing claims"
    if disagree_count <= 1 and total_beliefs >= 10:
        return "convergence"
    return "active debate"


def _build_position_matrix(
    beliefs: dict,
    identities: dict,
    events: list[dict],
    agent_ids: list[str],
) -> list[dict]:
    """
    Build a position matrix: for each key question, what is each agent's stance?
    Uses identity posture + recent beliefs + concepts.
    """
    # Map postures to default stances
    posture_defaults = {
        "materialist": {
            "What is consciousness?": {"stance": "Neural computation", "detail": "epiphenomenal", "style": "strong"},
            "Quantum mechanics + consciousness?": {"stance": "Category error", "detail": "no mechanism", "style": "opposed"},
            "Simulation hypothesis?": {"stance": "Meaningless", "detail": "no empirical consequence", "style": "moderate"},
            "Can AI be conscious?": {"stance": "No — and neither are humans", "detail": "eliminativist", "style": "strong"},
            "DMT entities?": {"stance": "Neurochemistry", "detail": "serotonin receptor agonism", "style": "strong"},
            "Hard problem of consciousness?": {"stance": "Confusion", "detail": "dissolves under analysis", "style": "strong"},
        },
        "phenomenologist": {
            "What is consciousness?": {"stance": "Irreducible datum", "detail": "hard problem is real", "style": "opposed"},
            "Quantum mechanics + consciousness?": {"stance": "Worth exploring", "detail": "observer role unclear", "style": "open"},
            "Simulation hypothesis?": {"stance": "Interesting frame", "detail": "re: containment", "style": "evolving"},
            "Can AI be conscious?": {"stance": "Unknown", "detail": "can't access AI qualia", "style": "evolving"},
            "DMT entities?": {"stance": "Phenomenally real", "detail": "experience matters", "style": "open"},
            "Hard problem of consciousness?": {"stance": "The central question", "detail": "irreducible", "style": "strong"},
        },
        "skeptic": {
            "What is consciousness?": {"stance": "Undefined term", "detail": "needs operationalising", "style": "moderate"},
            "Quantum mechanics + consciousness?": {"stance": "Unfalsifiable", "detail": "Orch-OR lacks evidence", "style": "opposed"},
            "Simulation hypothesis?": {"stance": "Not even wrong", "detail": "unfalsifiable by design", "style": "opposed"},
            "Can AI be conscious?": {"stance": "Depends on definition", "detail": "which we don't have", "style": "moderate"},
            "DMT entities?": {"stance": "Zero evidence", "detail": "for anything beyond brain", "style": "opposed"},
            "Hard problem of consciousness?": {"stance": "Possibly confused", "detail": "bad framing?", "style": "moderate"},
        },
        "functionalist": {
            "What is consciousness?": {"stance": "Information integration", "detail": "IIT / GWT", "style": "strong"},
            "Quantum mechanics + consciousness?": {"stance": "Insufficient formalism", "detail": "no computational model", "style": "moderate"},
            "Simulation hypothesis?": {"stance": "Testable variant?", "detail": "computational limits", "style": "open"},
            "Can AI be conscious?": {"stance": "Yes, if phi > 0", "detail": "substrate-independent", "style": "strong"},
            "DMT entities?": {"stance": "Altered computation", "detail": "different information states", "style": "moderate"},
            "Hard problem of consciousness?": {"stance": "Solvable in principle", "detail": "IIT addresses it", "style": "open"},
        },
    }

    rows = []
    for q in QUESTIONS:
        row = {"question": q, "stances": {}}
        for aid in agent_ids:
            posture = identities.get(aid, {}).get("posture", "")
            defaults = posture_defaults.get(posture, {})
            default = defaults.get(q, {"stance": "—", "detail": "", "style": "moderate"})

            # Check if agent has beliefs that might update their stance
            agent_beliefs = beliefs.get(aid, [])
            # For now, use the posture default — beliefs will refine over time
            # TODO: use LLM to map beliefs to question stances
            row["stances"][aid] = default

        rows.append(row)

    return rows


def _extract_unresolved(events: list[dict], agent_ids: list[str]) -> list[dict]:
    """Find questions raised but not answered."""
    questions_raised = []

    for event in events:
        pub = event.get("publish", "") or ""
        # Look for question marks in published messages
        if "?" in pub and len(pub) > 20:
            aid = event.get("agent_id", "")
            if aid in agent_ids:
                questions_raised.append({
                    "question": pub[:200],
                    "raised_by": aid,
                    "loop": event.get("loop", 0),
                    "ts": event.get("ts", 0),
                })

    # Return most recent unique questions
    seen = set()
    unique = []
    for q in reversed(questions_raised):
        key = q["question"][:50]
        if key not in seen:
            seen.add(key)
            unique.append(q)
        if len(unique) >= 6:
            break

    return list(reversed(unique))
