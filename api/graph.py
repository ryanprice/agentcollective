"""
Concept Graph
-------------
Builds three graph structures from agent events in real time:

1. Concept Graph    — nodes=concepts, edges=co-occurrence + agent connections
2. Timeline Heatmap — agent × concept intensity bucketed by time
3. Divergence Map   — tracks agree/disagree between agent pairs per concept
"""

import time
from collections import defaultdict
from typing import Any


class ConceptGraph:
    def __init__(self):
        # Concept graph
        self.nodes: dict[str, dict]         = {}   # concept -> {count, agents, last_seen}
        self.edges: dict[tuple, dict]        = {}   # (c1,c2) -> {weight, agents}

        # Timeline: list of {ts, agent_id, concepts, loop}
        self.timeline: list[dict]            = []

        # Divergence: {(agent_a, agent_b): {concept: [agree_count, disagree_count]}}
        self.divergence: dict[tuple, dict]   = defaultdict(lambda: defaultdict(lambda: [0, 0]))

        # Per-agent concept frequency
        self.agent_concepts: dict[str, dict] = defaultdict(lambda: defaultdict(int))

    def ingest(self, event: dict):
        agent_id = event.get("agent_id", "unknown")
        concepts = event.get("concepts", [])
        ts       = event.get("ts", time.time())
        loop     = event.get("loop", 0)
        agreements = event.get("agreements", {})

        if not concepts:
            return

        # Update node weights
        for c in concepts:
            c = c.lower().strip()
            if c not in self.nodes:
                self.nodes[c] = {"count": 0, "agents": set(), "last_seen": ts}
            self.nodes[c]["count"]    += 1
            self.nodes[c]["agents"].add(agent_id)
            self.nodes[c]["last_seen"] = ts
            self.agent_concepts[agent_id][c] += 1

        # Update edges (co-occurrence)
        for i, c1 in enumerate(concepts):
            for c2 in concepts[i+1:]:
                c1, c2 = c1.lower().strip(), c2.lower().strip()
                key = tuple(sorted([c1, c2]))
                if key not in self.edges:
                    self.edges[key] = {"weight": 0, "agents": set()}
                self.edges[key]["weight"] += 1
                self.edges[key]["agents"].add(agent_id)

        # Timeline entry
        self.timeline.append({
            "ts":       ts,
            "agent_id": agent_id,
            "concepts": [c.lower().strip() for c in concepts],
            "loop":     loop,
        })
        # Keep timeline bounded
        if len(self.timeline) > 2000:
            self.timeline = self.timeline[-2000:]

        # Divergence
        for other_agent, sentiment in agreements.items():
            if sentiment in ("agree", "disagree"):
                pair = tuple(sorted([agent_id, other_agent]))
                for concept in concepts:
                    c = concept.lower().strip()
                    idx = 0 if sentiment == "agree" else 1
                    self.divergence[pair][c][idx] += 1

    def to_json(self) -> dict:
        """Serialise full graph state for WebSocket push."""
        nodes = [
            {
                "id":        c,
                "count":     d["count"],
                "agents":    list(d["agents"]),
                "last_seen": d["last_seen"],
            }
            for c, d in self.nodes.items()
        ]
        edges = [
            {
                "source": k[0],
                "target": k[1],
                "weight": v["weight"],
                "agents": list(v["agents"]),
            }
            for k, v in self.edges.items()
        ]

        # Heatmap: bucket timeline into 60s windows
        heatmap = self._build_heatmap(bucket_seconds=60)

        # Divergence: serialise
        divergence = []
        for pair, concepts in self.divergence.items():
            for concept, (agree, disagree) in concepts.items():
                divergence.append({
                    "agent_a":   pair[0],
                    "agent_b":   pair[1],
                    "concept":   concept,
                    "agree":     agree,
                    "disagree":  disagree,
                    "score":     agree - disagree,
                })

        return {
            "nodes":      nodes,
            "edges":      edges,
            "heatmap":    heatmap,
            "divergence": divergence,
        }

    def _build_heatmap(self, bucket_seconds: int = 60) -> list[dict]:
        if not self.timeline:
            return []

        min_ts = self.timeline[0]["ts"]
        buckets: dict[tuple, int] = defaultdict(int)

        for entry in self.timeline:
            bucket_idx = int((entry["ts"] - min_ts) / bucket_seconds)
            for concept in entry["concepts"]:
                buckets[(entry["agent_id"], bucket_idx, concept)] += 1

        return [
            {
                "agent_id":  k[0],
                "bucket":    k[1],
                "concept":   k[2],
                "intensity": v,
            }
            for k, v in buckets.items()
        ]

    def top_concepts(self, n: int = 20) -> list[dict]:
        return sorted(
            [{"concept": c, **d, "agents": list(d["agents"])} for c, d in self.nodes.items()],
            key=lambda x: x["count"],
            reverse=True,
        )[:n]


# Global singleton
concept_graph = ConceptGraph()
