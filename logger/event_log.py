"""
Event Logger
------------
Persists every bus event to JSONL for post-session AI analysis.

Layout:
  logs/
    sessions.json          — manifest of all sessions
    {session_id}/
      events.jsonl         — one JSON object per line, every event
      summary.json         — written on shutdown: stats, top concepts, agent activity

Each JSONL line:
  {"ts": 1234567890.1, "session": "...", "agent_id": "...", "model": "...",
   "phase": "...", "thought": "...", "concepts": [...], "publish": "...",
   "action": {...}, "result": {...}, "loop": 3}
"""

import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("event_log")

LOGS_DIR = Path("logs")


class EventLogger:
    def __init__(self, session_id: str = None):
        self.session_id  = session_id or datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.session_dir = LOGS_DIR / self.session_id
        self.session_dir.mkdir(parents=True, exist_ok=True)

        self.events_file  = self.session_dir / "events.jsonl"
        self.summary_file = self.session_dir / "summary.json"
        self._start_time  = time.time()
        self._count       = 0

        self._register_session()
        log.info(f"Event log: logs/{self.session_id}/events.jsonl")

    # ── Public ────────────────────────────────────────────────────────────────

    def write(self, event: dict):
        """Append one event to the JSONL file."""
        record = {
            "ts":       event.get("ts", time.time()),
            "session":  self.session_id,
            "agent_id": event.get("agent_id"),
            "model":    event.get("model"),
            "phase":    event.get("phase"),
            "loop":     event.get("loop"),
            "thought":  event.get("thought"),
            "concepts": event.get("concepts", []),
            "publish":  event.get("publish"),
            "belief":   event.get("belief"),
            "action":   event.get("action"),
            "result":   self._safe_result(event.get("result")),
        }
        with self.events_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._count += 1

    def close(self, agents: dict = None):
        """Write summary.json on shutdown."""
        duration = time.time() - self._start_time
        summary  = self._build_summary(agents, duration)
        self.summary_file.write_text(json.dumps(summary, indent=2, ensure_ascii=False))

        # Update sessions manifest
        self._update_manifest(summary)
        log.info(f"Session closed — {self._count} events logged to logs/{self.session_id}/")
        return summary

    # ── Summary building ──────────────────────────────────────────────────────

    def _build_summary(self, agents: dict, duration: float) -> dict:
        concept_counts: dict[str, int] = defaultdict(int)
        agent_stats: dict[str, dict]   = defaultdict(lambda: {
            "events": 0, "searches": 0, "broadcasts": 0,
            "beliefs": [], "loops": 0,
        })
        phase_counts: dict[str, int]   = defaultdict(int)
        all_thoughts: list[str]        = []

        if self.events_file.exists():
            for line in self.events_file.read_text(encoding="utf-8").splitlines():
                try:
                    e = json.loads(line)
                except Exception:
                    continue

                aid = e.get("agent_id", "unknown")
                agent_stats[aid]["events"] += 1
                phase_counts[e.get("phase", "unknown")] += 1

                for c in (e.get("concepts") or []):
                    concept_counts[c] += 1

                if e.get("action", {}) and e["action"].get("type") == "search":
                    agent_stats[aid]["searches"] += 1

                if e.get("publish"):
                    agent_stats[aid]["broadcasts"] += 1

                if e.get("belief"):
                    agent_stats[aid]["beliefs"].append(e["belief"])

                if e.get("loop"):
                    agent_stats[aid]["loops"] = max(agent_stats[aid]["loops"], e["loop"])

                if e.get("thought") and e.get("phase") == "reason":
                    all_thoughts.append(e["thought"][:300])

        top_concepts = sorted(concept_counts.items(), key=lambda x: -x[1])[:30]

        return {
            "session_id":    self.session_id,
            "started_at":    datetime.fromtimestamp(self._start_time, tz=timezone.utc).isoformat(),
            "duration_secs": round(duration),
            "total_events":  self._count,
            "phase_counts":  dict(phase_counts),
            "top_concepts":  [{"concept": c, "count": n} for c, n in top_concepts],
            "agents":        {
                aid: {
                    **stats,
                    "beliefs": stats["beliefs"][-20:],  # last 20 beliefs
                }
                for aid, stats in agent_stats.items()
            },
            "thought_sample": all_thoughts[-50:],  # last 50 reason thoughts
        }

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _register_session(self):
        manifest = self._load_manifest()
        manifest["sessions"].append({
            "session_id": self.session_id,
            "started_at": datetime.fromtimestamp(self._start_time, tz=timezone.utc).isoformat(),
            "closed":     False,
        })
        self._save_manifest(manifest)

    def _update_manifest(self, summary: dict):
        manifest = self._load_manifest()
        for s in manifest["sessions"]:
            if s["session_id"] == self.session_id:
                s["closed"]        = True
                s["duration_secs"] = summary["duration_secs"]
                s["total_events"]  = summary["total_events"]
                s["top_concepts"]  = [c["concept"] for c in summary["top_concepts"][:10]]
                break
        self._save_manifest(manifest)

    def _load_manifest(self) -> dict:
        manifest_file = LOGS_DIR / "sessions.json"
        if manifest_file.exists():
            try:
                return json.loads(manifest_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"sessions": []}

    def _save_manifest(self, manifest: dict):
        manifest_file = LOGS_DIR / "sessions.json"
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        manifest_file.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))

    def _safe_result(self, result) -> dict | None:
        """Strip large raw blobs — keep summaries and search result titles/urls."""
        if not result or not isinstance(result, dict):
            return None
        out = {"type": result.get("type"), "summary": result.get("summary", "")[:500]}
        raw = result.get("raw")
        if isinstance(raw, dict) and "results" in raw:
            out["results"] = [
                {"title": r.get("title", ""), "url": r.get("url", r.get("href", ""))}
                for r in raw["results"][:5]
            ]
        return out
