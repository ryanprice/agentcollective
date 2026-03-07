"""
Event Logger
------------
Persists every bus event to rotated JSONL chunks for post-session AI analysis.

Layout:
  logs/
    sessions.json              — manifest of all sessions
    {session_id}/
      chunk_000.jsonl          — up to CHUNK_SIZE events per file
      chunk_001.jsonl
      ...
      index.json               — maps chunk files to event ranges + top concepts
      summary.json             — written on shutdown: stats, beliefs, thought sample

Each JSONL line:
  {"ts": ..., "session": "...", "agent_id": "...", "model": "...",
   "phase": "...", "thought": "...", "concepts": [...], "publish": "...",
   "action": {...}, "result": {...}, "loop": 3}

Chunk size is tuned so each file fits comfortably in an AI context window (~2MB / ~500 events).
"""

import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("event_log")

LOGS_DIR   = Path("logs")
CHUNK_SIZE = 500   # events per file — fits easily in any LLM context window


class EventLogger:
    def __init__(self, session_id: str = None):
        self.session_id   = session_id or datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.session_dir  = LOGS_DIR / self.session_id
        self.session_dir.mkdir(parents=True, exist_ok=True)

        self.summary_file = self.session_dir / "summary.json"
        self.index_file   = self.session_dir / "index.json"

        self._start_time   = time.time()
        self._total        = 0        # total events this session
        self._chunk_idx    = 0        # current chunk number
        self._chunk_count  = 0        # events in current chunk
        self._chunk_file   = None     # open file handle
        self._chunks_meta  = []       # [{file, start_event, end_event, start_ts, top_concepts}]
        self._chunk_concepts: dict[str, int] = defaultdict(int)

        self._open_chunk()
        self._register_session()
        log.info(f"Event log: logs/{self.session_id}/ (rotating every {CHUNK_SIZE} events)")

    # ── Public ────────────────────────────────────────────────────────────────

    def write(self, event: dict):
        if self._chunk_count >= CHUNK_SIZE:
            self._rotate()

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
        self._chunk_file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._chunk_file.flush()

        for c in (event.get("concepts") or []):
            self._chunk_concepts[c] += 1

        self._chunk_count += 1
        self._total       += 1

    def close(self, agents: dict = None) -> dict:
        self._close_chunk()
        duration = time.time() - self._start_time
        summary  = self._build_summary(agents, duration)
        self.summary_file.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
        self._write_index()
        self._update_manifest(summary)
        log.info(f"Session closed — {self._total} events in {len(self._chunks_meta)} chunks → logs/{self.session_id}/")
        return summary

    # ── Chunk management ──────────────────────────────────────────────────────

    def _open_chunk(self):
        chunk_name = f"chunk_{self._chunk_idx:03d}.jsonl"
        path = self.session_dir / chunk_name
        self._chunk_file = open(path, "a", encoding="utf-8")
        self._chunk_start_event = self._total
        self._chunk_start_ts    = time.time()
        self._chunk_concepts    = defaultdict(int)
        self._chunk_count       = 0

    def _close_chunk(self):
        if self._chunk_file:
            self._chunk_file.close()
            self._chunk_file = None
        chunk_name = f"chunk_{self._chunk_idx:03d}.jsonl"
        if self._chunk_count > 0:
            top = sorted(self._chunk_concepts.items(), key=lambda x: -x[1])[:10]
            self._chunks_meta.append({
                "file":        chunk_name,
                "start_event": self._chunk_start_event,
                "end_event":   self._total - 1,
                "event_count": self._chunk_count,
                "start_ts":    self._chunk_start_ts,
                "end_ts":      time.time(),
                "top_concepts": [c for c, _ in top],
            })

    def _rotate(self):
        self._close_chunk()
        self._chunk_idx += 1
        self._open_chunk()
        self._write_index()  # update index after each rotation
        log.info(f"Log rotated → chunk_{self._chunk_idx:03d}.jsonl (total {self._total} events)")

    def _write_index(self):
        index = {
            "session_id":  self.session_id,
            "chunk_size":  CHUNK_SIZE,
            "total_events": self._total,
            "chunks":      self._chunks_meta,
            "usage_hint":  (
                "Load one chunk at a time. Each chunk contains up to "
                f"{CHUNK_SIZE} events. Filter by 'phase' (reason/act/observe/memory) "
                "or 'agent_id' (qwen/glm/llama/deepseek). "
                "Start with summary.json for an overview, then crawl chunks by topic using top_concepts."
            ),
        }
        self.index_file.write_text(json.dumps(index, indent=2, ensure_ascii=False))

    # ── Summary ───────────────────────────────────────────────────────────────

    def _build_summary(self, agents: dict, duration: float) -> dict:
        concept_counts: dict[str, int] = defaultdict(int)
        agent_stats: dict[str, dict]   = defaultdict(lambda: {
            "events": 0, "searches": 0, "broadcasts": 0,
            "beliefs": [], "loops": 0,
        })
        phase_counts: dict[str, int]   = defaultdict(int)
        thought_sample: list[str]      = []

        for chunk_meta in self._chunks_meta:
            path = self.session_dir / chunk_meta["file"]
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                aid = e.get("agent_id", "unknown")
                agent_stats[aid]["events"] += 1
                phase_counts[e.get("phase", "unknown")] += 1
                for c in (e.get("concepts") or []):
                    concept_counts[c] += 1
                if e.get("action", {}) and isinstance(e.get("action"), dict) and e["action"].get("type") == "search":
                    agent_stats[aid]["searches"] += 1
                if e.get("publish"):
                    agent_stats[aid]["broadcasts"] += 1
                if e.get("belief"):
                    agent_stats[aid]["beliefs"].append(e["belief"])
                if e.get("loop"):
                    agent_stats[aid]["loops"] = max(agent_stats[aid]["loops"], e["loop"])
                if e.get("phase") == "reason" and e.get("thought"):
                    thought_sample.append(e["thought"][:300])

        top_concepts = sorted(concept_counts.items(), key=lambda x: -x[1])[:30]

        return {
            "session_id":    self.session_id,
            "started_at":    datetime.fromtimestamp(self._start_time, tz=timezone.utc).isoformat(),
            "duration_secs": round(duration),
            "total_events":  self._total,
            "total_chunks":  len(self._chunks_meta),
            "chunk_size":    CHUNK_SIZE,
            "phase_counts":  dict(phase_counts),
            "top_concepts":  [{"concept": c, "count": n} for c, n in top_concepts],
            "agents":        {
                aid: {**stats, "beliefs": stats["beliefs"][-20:]}
                for aid, stats in agent_stats.items()
            },
            "thought_sample": thought_sample[-50:],
            "crawl_guide": (
                f"This session has {len(self._chunks_meta)} chunk files. "
                "Load index.json first to see which chunks cover which topics. "
                "Each chunk is self-contained JSONL — one event per line. "
                "Filter phase='reason' for deep thoughts, phase='act' for tool use, "
                "phase='observe' for search results. belief field = durable conclusions."
            ),
        }

    # ── Manifest ──────────────────────────────────────────────────────────────

    def _register_session(self):
        m = self._load_manifest()
        m["sessions"].append({
            "session_id": self.session_id,
            "started_at": datetime.fromtimestamp(self._start_time, tz=timezone.utc).isoformat(),
            "closed": False,
        })
        self._save_manifest(m)

    def _update_manifest(self, summary: dict):
        m = self._load_manifest()
        for s in m["sessions"]:
            if s["session_id"] == self.session_id:
                s.update({
                    "closed":        True,
                    "duration_secs": summary["duration_secs"],
                    "total_events":  summary["total_events"],
                    "total_chunks":  summary["total_chunks"],
                    "top_concepts":  [c["concept"] for c in summary["top_concepts"][:10]],
                })
                break
        self._save_manifest(m)

    def _load_manifest(self) -> dict:
        f = LOGS_DIR / "sessions.json"
        if f.exists():
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"sessions": []}

    def _save_manifest(self, m: dict):
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        (LOGS_DIR / "sessions.json").write_text(json.dumps(m, indent=2, ensure_ascii=False))

    def _safe_result(self, result) -> dict | None:
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
