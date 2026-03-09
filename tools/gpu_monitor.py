"""
GPU Monitor
-----------
Monitors GPU temperature and memory usage via nvidia-smi.
Implements escalating safeguards:

  Level 0 — NORMAL:   All clear
  Level 1 — WARM:     Slow loop delays (throttle)
  Level 2 — HOT:      Pause all agents (no new LLM calls)
  Level 3 — CRITICAL: Stop heaviest model first, stay paused

Thresholds (configurable in config.yaml):
  temp_warn:     75°C   → Level 1
  temp_hot:      85°C   → Level 2
  temp_critical: 92°C   → Level 3
  mem_warn:      80%    → Level 1
  mem_hot:       90%    → Level 2
  mem_critical:  95%    → Level 3
"""

import asyncio
import logging
import subprocess
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional

from bus.broker import bus

log = logging.getLogger("gpu_monitor")


class SafeLevel(IntEnum):
    NORMAL   = 0
    WARM     = 1  # throttle
    HOT      = 2  # pause all
    CRITICAL = 3  # stop heaviest


@dataclass
class GPUStats:
    gpu_index:    int
    name:         str
    temp_c:       float
    mem_used_mb:  float
    mem_total_mb: float
    mem_pct:      float
    timestamp:    float = field(default_factory=time.time)

    @property
    def ok(self) -> bool:
        return self.temp_c > 0


@dataclass
class MonitorConfig:
    temp_warn:     float = 75.0
    temp_hot:      float = 85.0
    temp_critical: float = 92.0
    mem_warn:      float = 80.0
    mem_hot:       float = 90.0
    mem_critical:  float = 95.0
    poll_seconds:  float = 10.0

    @classmethod
    def from_dict(cls, d: dict) -> "MonitorConfig":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class GPUMonitor:
    def __init__(self, config: MonitorConfig, agents: dict):
        self.config     = config
        self.agents     = agents          # {agent_id: Agent}
        self.stats:  list[GPUStats] = []
        self.level:  SafeLevel = SafeLevel.NORMAL
        self._paused_agents: set[str] = set()
        self._stopped_agents: set[str] = set()
        self._running = False
        self.history: list[dict] = []     # for dashboard

    # ── Public ────────────────────────────────────────────────────────────────

    async def run(self):
        self._running = True
        log.info("GPU monitor started")
        while self._running:
            try:
                self.stats = self._read_gpu_stats()
                new_level  = self._compute_level()
                if new_level != self.level:
                    await self._apply_level(new_level)
                self._record_history()
            except Exception as e:
                log.warning(f"GPU monitor error: {e}")
            await asyncio.sleep(self.config.poll_seconds)

    def stop(self):
        self._running = False

    def status(self) -> dict:
        return {
            "level":       self.level.name,
            "level_int":   int(self.level),
            "gpus":        [self._stat_dict(s) for s in self.stats],
            "paused":      list(self._paused_agents),
            "stopped":     list(self._stopped_agents),
            "history":     self.history[-60:],
        }

    # ── GPU reading ───────────────────────────────────────────────────────────

    def _read_gpu_stats(self) -> list[GPUStats]:
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=index,name,temperature.gpu,memory.used,memory.total,utilization.memory",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return []

            stats = []
            for line in result.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 5:
                    continue

                def safe_float(val, default=0.0) -> float:
                    try:
                        return float(val)
                    except (ValueError, TypeError):
                        return default

                mem_used  = safe_float(parts[3])
                mem_total = safe_float(parts[4])
                mem_util  = safe_float(parts[5]) if len(parts) > 5 else 0.0

                # Grace Hopper / GB10: unified memory — try mem_util % if totals are N/A
                if mem_total == 0:
                    mem_used, mem_total, mem_pct = self._read_gh_memory(int(safe_float(parts[0])), mem_util)
                else:
                    mem_pct = (mem_used / mem_total * 100) if mem_total > 0 else 0

                stats.append(GPUStats(
                    gpu_index    = int(safe_float(parts[0])),
                    name         = parts[1],
                    temp_c       = safe_float(parts[2]),
                    mem_used_mb  = mem_used,
                    mem_total_mb = mem_total,
                    mem_pct      = mem_pct,
                ))
            return stats
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []

    def _read_gh_memory(self, gpu_index: int, util_pct: float):
        """
        Grace Hopper GB10 unified memory fallback.
        Tries nvidia-smi dmon, then free -m for system RAM as proxy.
        Returns (used_mb, total_mb, pct).
        """
        # Try nvidia-smi dmon for a single sample
        try:
            r = subprocess.run(
                ["nvidia-smi", "dmon", "-s", "m", "-c", "1", "-i", str(gpu_index)],
                capture_output=True, text=True, timeout=5,
            )
            for line in r.stdout.splitlines():
                if line.startswith("#") or not line.strip():
                    continue
                cols = line.split()
                if len(cols) >= 3:
                    used  = float(cols[1])
                    total = float(cols[2])
                    if total > 0:
                        return used, total, (used / total * 100)
        except Exception:
            pass

        # Fallback: read system unified memory via /proc/meminfo
        try:
            mem = {}
            with open("/proc/meminfo") as f:
                for l in f:
                    k, v = l.split(":")
                    mem[k.strip()] = int(v.strip().split()[0]) / 1024  # KB→MB
            total = mem.get("MemTotal", 0)
            avail = mem.get("MemAvailable", 0)
            used  = total - avail
            pct   = (used / total * 100) if total > 0 else 0
            return used, total, pct
        except Exception:
            pass

        return 0, 0, util_pct  # last resort: use nvidia-smi util %

    # ── Level computation ─────────────────────────────────────────────────────

    def _compute_level(self) -> SafeLevel:
        if not self.stats:
            return SafeLevel.NORMAL

        max_temp = max(s.temp_c  for s in self.stats)
        max_mem  = max(s.mem_pct for s in self.stats)

        if max_temp >= self.config.temp_critical or max_mem >= self.config.mem_critical:
            return SafeLevel.CRITICAL
        if max_temp >= self.config.temp_hot or max_mem >= self.config.mem_hot:
            return SafeLevel.HOT
        if max_temp >= self.config.temp_warn or max_mem >= self.config.mem_warn:
            return SafeLevel.WARM
        return SafeLevel.NORMAL

    # ── Level application ─────────────────────────────────────────────────────

    async def _apply_level(self, new_level: SafeLevel):
        old_level = self.level
        self.level = new_level

        summary = self._stats_summary()
        msg = f"GPU safeguard: {old_level.name} → {new_level.name} | {summary}"
        log.warning(msg) if new_level > SafeLevel.NORMAL else log.info(msg)

        await bus.publish({
            "agent_id": "gpu_monitor",
            "model":    "system",
            "color":    self._level_color(new_level),
            "phase":    "system",
            "thought":  msg,
            "concepts": ["gpu", "temperature", "memory", "safeguard"],
            "publish":  msg,
        })

        # ── Escalation ────────────────────────────────────────
        if new_level == SafeLevel.CRITICAL:
            await self._pause_all()
            await self._stop_heaviest()

        elif new_level == SafeLevel.HOT:
            await self._pause_all()

        # ── De-escalation ─────────────────────────────────────
        elif new_level == SafeLevel.WARM:
            # Unpause agents that were paused at HOT/CRITICAL
            # Restart stopped agents — throttled delays will keep load manageable
            await self._resume_all()
            await self._apply_throttle()

        elif new_level == SafeLevel.NORMAL:
            await self._resume_all()

    async def _apply_throttle(self):
        """Increase loop delays on all running agents."""
        for agent in self.agents.values():
            agent.loop_config["min_delay_seconds"] = 15
            agent.loop_config["max_delay_seconds"] = 30
        log.info("Throttle applied: loop delay increased to 15–30s")

    async def _pause_all(self):
        """Set a pause flag on all agents — they finish current iteration then wait."""
        for agent_id, agent in self.agents.items():
            if not hasattr(agent, "_paused"):
                agent._paused = False
            if not agent._paused:
                agent._paused = True
                self._paused_agents.add(agent_id)
        log.warning(f"All agents paused: {list(self._paused_agents)}")

    async def _stop_heaviest(self):
        """Stop the agent running the heaviest model (largest param count heuristic)."""
        model_weight = {
            "qwen2.5-coder:32b":      32,
            "glm-4.7-flash:latest":   7,
            "llama3.1:8b":            8,
            "deepseek-coder-v2:16b":  16,
            "llama3.3:70b":           70,
            "nemotron-3-nano:latest": 3,
        }
        running = [
            a for a in self.agents.values()
            if a.id not in self._stopped_agents
        ]
        if not running:
            return
        heaviest = max(running, key=lambda a: model_weight.get(a.model, 10))
        await heaviest.stop()
        self._stopped_agents.add(heaviest.id)
        log.warning(f"Stopped heaviest agent: {heaviest.id} ({heaviest.model})")

    async def _resume_all(self):
        """Resume paused agents, restart stopped agents, and restore normal delays."""
        for agent_id, agent in self.agents.items():
            if getattr(agent, "_paused", False):
                agent._paused = False
                self._paused_agents.discard(agent_id)

        # Restart agents that were hard-stopped at CRITICAL level
        if self._stopped_agents:
            for agent_id in list(self._stopped_agents):
                agent = self.agents.get(agent_id)
                if agent:
                    log.info(f"Restarting stopped agent: {agent_id} ({agent.model})")
                    asyncio.create_task(agent.run(), name=f"agent-{agent_id}")
            restarted = list(self._stopped_agents)
            self._stopped_agents.clear()
            await bus.publish({
                "agent_id": "gpu_monitor",
                "model":    "system",
                "color":    self._level_color(SafeLevel.NORMAL),
                "phase":    "system",
                "thought":  f"Restarted previously stopped agents: {restarted}",
                "concepts": ["gpu", "safeguard", "recovery"],
                "publish":  f"GPU temps normal — restarting {', '.join(restarted)}",
            })

        # Restore normal delays
        for agent in self.agents.values():
            agent.loop_config["min_delay_seconds"] = 3
            agent.loop_config["max_delay_seconds"] = 8

        log.info("Agents resumed, delays restored to normal.")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _stats_summary(self) -> str:
        if not self.stats:
            return "no GPU data"
        parts = []
        for s in self.stats:
            parts.append(f"GPU{s.gpu_index} {s.temp_c:.0f}°C {s.mem_pct:.0f}%mem")
        return " | ".join(parts)

    def _record_history(self):
        entry = {
            "ts":    time.time(),
            "level": int(self.level),
            "gpus":  [self._stat_dict(s) for s in self.stats],
        }
        self.history.append(entry)
        if len(self.history) > 360:  # ~1hr at 10s intervals
            self.history = self.history[-360:]

    def _stat_dict(self, s: GPUStats) -> dict:
        return {
            "index":      s.gpu_index,
            "name":       s.name,
            "temp_c":     s.temp_c,
            "mem_used_mb": s.mem_used_mb,
            "mem_total_mb": s.mem_total_mb,
            "mem_pct":    round(s.mem_pct, 1),
        }

    def _level_color(self, level: SafeLevel) -> str:
        return {
            SafeLevel.NORMAL:   "#10B981",
            SafeLevel.WARM:     "#F59E0B",
            SafeLevel.HOT:      "#EF4444",
            SafeLevel.CRITICAL: "#7C3AED",
        }[level]
