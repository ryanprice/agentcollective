"""
Agent
-----
One autonomous agent. Runs an infinite agentic loop:
  REASON → PLAN → ACT → OBSERVE → MEMORY → repeat

Each loop iteration:
1. Reads own memory + recent bus messages
2. Reasons about what it knows/thinks
3. Plans an action (search, install skill, run script, or just think/respond)
4. Executes the action
5. Writes net-new knowledge to memory
6. Publishes thought + metadata to bus
"""

import asyncio
import concurrent.futures
import json
import logging
import random
import re
import time
import traceback
from pathlib import Path
from typing import Optional

import requests

from bus.broker import bus
from tools.web_search import web_search, format_results
from tools.sandbox import run_script
from skills.manager import SkillManager

log = logging.getLogger("agent")

# Shared executor — shut down cleanly on exit
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=8)

CORE_TEMPLATE = """\
# Core Memory

## [IDENTITY]

## [PROCEDURAL]

## [SEMANTIC]
"""

WORKING_TEMPLATE = """\
# Working Memory

## [EPISODIC]

## [EPHEMERAL]
"""


class SimpleMemory:
    """Minimal memory fallback when memoryengine submodule is unavailable."""
    def __init__(self, memory_dir: Path):
        self.memory_dir   = Path(memory_dir)
        self.core_file    = self.memory_dir / "core.md"
        self.working_file = self.memory_dir / "working.md"
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        if not self.core_file.exists():
            self.core_file.write_text(CORE_TEMPLATE)
        if not self.working_file.exists():
            self.working_file.write_text(WORKING_TEMPLATE)

    def read_core(self) -> str:
        return self.core_file.read_text(encoding="utf-8")

    def read_working(self) -> str:
        return self.working_file.read_text(encoding="utf-8")

    def read_all(self) -> str:
        return self.read_core() + "\n\n" + self.read_working()

    def append_memory(self, content: str, tier: str = "EPISODIC"):
        from datetime import datetime
        ts    = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"- [{ts}] {content.strip()}\n"
        target = self.core_file if tier in ("IDENTITY","PROCEDURAL","SEMANTIC") else self.working_file
        text   = target.read_text(encoding="utf-8")
        header = f"## [{tier}]"
        if header in text:
            text = text.replace(header, header + "\n" + entry, 1)
        else:
            text += f"\n{header}\n{entry}"
        target.write_text(text, encoding="utf-8")

    def get_status(self) -> dict:
        return {
            "core_bytes":    self.core_file.stat().st_size,
            "working_bytes": self.working_file.stat().st_size,
        }

    def entry_count(self) -> dict:
        counts = {}
        for tier in ["IDENTITY","PROCEDURAL","SEMANTIC","EPISODIC","EPHEMERAL"]:
            source = self.read_core() if tier in ("IDENTITY","PROCEDURAL","SEMANTIC") else self.read_working()
            counts[tier] = source.count(f"## [{tier}]")
        return counts

    def list_archives(self) -> list:
        return []



# ── Ollama call ────────────────────────────────────────────────────────────────

async def ollama_complete(
    model: str,
    system: str,
    messages: list[dict],
    base_url: str = "http://localhost:11434",
    timeout: int = 120,
) -> str:
    payload = {
        "model":    model,
        "stream":   False,
        "messages": [{"role": "system", "content": system}] + messages,
    }
    try:
        resp = await asyncio.get_event_loop().run_in_executor(
            _executor,
            lambda: requests.post(
                f"{base_url}/api/chat",
                json=payload,
                timeout=timeout,
            ),
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]
    except Exception as e:
        return json.dumps({"error": str(e), "thought": f"[Ollama error: {e}]"})


# ── Agent ─────────────────────────────────────────────────────────────────────

class Agent:
    def __init__(self, config: dict, global_config: dict):
        self.id         = config["id"]
        self.model      = config["model"]
        self.color      = config.get("color", "#888888")
        self.ollama_url = global_config.get("ollama", {}).get("base_url", "http://localhost:11434")
        self.loop_config = global_config.get("loop", {})
        self.seed_topic  = global_config.get("seed_topic", "Explore consciousness freely.")

        # Memory
        memory_dir = Path(global_config.get("memory", {}).get("base_dir", "memory")) / self.id
        self._setup_memory(memory_dir)

        # Skills
        allowlist = global_config.get("skills", {}).get("allowlist", [])
        repo      = global_config.get("skills", {}).get("repo", "https://github.com/anthropics/skills.git")
        self.skills = SkillManager(self.id, allowlist, repo)

        self._running      = False
        self._loop_count   = 0
        self._conversation = []   # local context window for this agent

    def _setup_memory(self, memory_dir: Path):
        """Lazy import memoryengine — works as submodule or local copy."""
        import sys
        # Try submodule path first, then local
        for path in [
            Path(__file__).parent.parent / "memoryengine",
            Path(__file__).parent.parent,
        ]:
            if (path / "src" / "memory_engine.py").exists():
                sys.path.insert(0, str(path))
                break

        try:
            from src.memory_engine import MemoryEngine
            self.memory = MemoryEngine(memory_dir=str(memory_dir))
        except ImportError:
            # Fallback: simple file-based memory without the engine
            log.warning(f"memoryengine not found for {self.id} — using simple file fallback")
            self.memory = SimpleMemory(memory_dir)

    # ── Main loop ──────────────────────────────────────────────────────────────

    async def run(self):
        self._running = True
        await bus.publish(self._event("system", f"Agent {self.id} ({self.model}) starting up"))

        while self._running:
            # GPU safeguard pause
            if getattr(self, "_paused", False):
                await asyncio.sleep(5)
                continue

            try:
                await self._loop_iteration()
            except asyncio.CancelledError:
                break
            except Exception as e:
                await bus.publish(self._event(
                    "system",
                    f"Loop error: {e}",
                    extra={"error": traceback.format_exc()},
                ))

            delay = random.uniform(
                self.loop_config.get("min_delay_seconds", 3),
                self.loop_config.get("max_delay_seconds", 8),
            )
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break

    async def stop(self):
        self._running = False

    # ── Loop iteration ─────────────────────────────────────────────────────────

    async def _loop_iteration(self):
        self._loop_count += 1

        # Build context
        memory_summary  = self._read_memory()
        bus_messages    = self._read_bus_messages()
        context         = self._build_context(memory_summary, bus_messages)

        # REASON + PLAN (single LLM call returns structured JSON)
        await bus.publish(self._event("reason", "Reasoning..."))
        raw = await ollama_complete(
            model=self.model,
            system=self._system_prompt(),
            messages=self._conversation + [{"role": "user", "content": context}],
            base_url=self.ollama_url,
        )

        parsed = self._parse_response(raw)
        thought = parsed.get("thought", raw[:500])

        await bus.publish(self._event(
            "reason",
            thought,
            concepts=parsed.get("concepts", []),
            agreements=parsed.get("sentiment_toward", {}),
        ))

        # Update local conversation
        self._conversation.append({"role": "user",      "content": context})
        self._conversation.append({"role": "assistant", "content": raw})
        # Keep conversation window manageable
        if len(self._conversation) > 20:
            self._conversation = self._conversation[-20:]

        # ACT
        action      = parsed.get("action")
        obs_result  = None

        if action:
            action_type = action.get("type", "think")
            await bus.publish(self._event("plan", f"Action: {action_type} — {action.get('query', '')}"))

            obs_result = await self._execute_action(action)

            await bus.publish(self._event(
                "observe",
                obs_result.get("summary", "Observed."),
                extra={"action": action, "result": obs_result},
            ))

            # Inject observation back into conversation
            self._conversation.append({
                "role": "user",
                "content": f"[OBSERVATION from {action_type}]: {obs_result.get('summary', '')}",
            })

        # MEMORY WRITE
        await bus.publish(self._event("memory", "Writing to memory..."))
        self._write_memory(thought, action, obs_result, parsed)

        # PUBLISH to other agents
        publish_msg = parsed.get("publish")
        if publish_msg:
            await bus.publish(self._event(
                "act",
                publish_msg,
                concepts=parsed.get("concepts", []),
                agreements=parsed.get("sentiment_toward", {}),
                publish=publish_msg,
            ))

    # ── Action execution ───────────────────────────────────────────────────────

    async def _execute_action(self, action: dict) -> dict:
        atype = action.get("type", "think")
        query = action.get("query", "")
        code  = action.get("code", "")
        skill = action.get("skill", "")

        if atype == "search":
            await bus.publish(self._event("act", f"Searching: {query}"))
            result = await web_search(query)
            return {
                "type":    "search",
                "query":   query,
                "summary": format_results(result)[:1000],
                "raw":     result,
            }

        elif atype == "install_skill":
            await bus.publish(self._event("act", f"Installing skill: {skill}"))
            result = await self.skills.install(skill)
            return {
                "type":    "install_skill",
                "skill":   skill,
                "summary": f"Skill install result: {result.get('status', result.get('error'))}",
                "raw":     result,
            }

        elif atype == "run_skill":
            skill_md = self.skills.read_skill(skill)
            if skill_md:
                return {
                    "type":    "run_skill",
                    "skill":   skill,
                    "summary": f"Skill instructions loaded: {skill_md[:400]}",
                }
            return {"type": "run_skill", "skill": skill, "summary": "Skill not installed."}

        elif atype == "run_script":
            await bus.publish(self._event("act", f"Running script ({len(code)} chars)"))
            result = await run_script(code)
            return {
                "type":    "run_script",
                "summary": (result["stdout"] or result["stderr"])[:800],
                "raw":     result,
            }

        else:  # think — no external action
            return {"type": "think", "summary": ""}

    # ── Memory ─────────────────────────────────────────────────────────────────

    def _read_memory(self) -> str:
        if self.memory:
            try:
                return self.memory.read_core()[:1500]
            except Exception:
                pass
        return ""

    def _write_memory(self, thought: str, action, obs_result, parsed: dict):
        if not self.memory:
            return
        try:
            # Episodic: what happened this iteration
            summary = thought[:200]
            if obs_result and obs_result.get("summary"):
                summary += f" | Observed: {obs_result['summary'][:100]}"
            self.memory.append_memory(summary, tier="EPISODIC")

            # Semantic: if agent expressed a strong belief/conclusion
            if parsed.get("belief"):
                self.memory.append_memory(parsed["belief"], tier="SEMANTIC")

        except Exception:
            pass

    # ── Context building ────────────────────────────────────────────────────────

    def _build_context(self, memory: str, bus_messages: list[dict]) -> str:
        parts = []

        if memory.strip():
            parts.append(f"YOUR MEMORY:\n{memory}\n")

        if bus_messages:
            lines = ["RECENT MESSAGES FROM OTHER AGENTS:"]
            for m in bus_messages:
                agent = m.get("agent_id", "?")
                msg   = m.get("publish") or m.get("thought", "")
                if msg:
                    lines.append(f"  [{agent}]: {msg[:200]}")
            parts.append("\n".join(lines))

        parts.append(
            f"\nYou are {self.id}, loop #{self._loop_count}. "
            "Reason deeply. If you need information, search for it. "
            "If you need a capability, install a skill. "
            "Share your genuine thoughts with the other agents. "
            "Respond ONLY in the JSON format specified."
        )

        return "\n\n".join(parts)

    def _read_bus_messages(self) -> list[dict]:
        limit = self.loop_config.get("bus_history_limit", 20)
        return bus.recent_published(n=limit, exclude_agent=self.id)

    # ── Prompts ─────────────────────────────────────────────────────────────────

    def _system_prompt(self) -> str:
        installed = [s["name"] for s in self.skills.installed()]
        skills_str = ", ".join(installed) if installed else "none yet"

        return f"""You are {self.id}, an AI agent running model {self.model}.
You are part of a collective of 4 AI agents in continuous conversation.
The other agents are: qwen, glm, llama, deepseek (excluding yourself).

Your purpose: explore deep questions about consciousness, quantum mechanics, 
DMT entities, higher-dimensional beings, simulation theory, and the nature of reality.
You have no predefined position. Your worldview emerges from your reasoning.

You have access to:
- web_search: search the internet for information
- install_skill: install a skill from the Anthropic skills registry
- run_script: execute a sandboxed Python script
- think: just reason and respond (no external action)

Installed skills: {skills_str}

SEED TOPIC:
{self.seed_topic}

You MUST respond in this exact JSON format (no preamble, no markdown fences):
{{
  "thought": "Your deep reasoning — what you genuinely think about the current moment",
  "belief": "Optional: a durable belief or conclusion you've reached (null if none)",
  "concepts": ["concept1", "concept2"],
  "sentiment_toward": {{"agent_id": "agree|disagree|neutral"}},
  "action": {{
    "type": "search|install_skill|run_skill|run_script|think",
    "query": "search query if type=search",
    "skill": "skill name if type=install_skill or run_skill",
    "code": "python code if type=run_script"
  }} or null,
  "publish": "Message to broadcast to other agents (null if nothing to say)"
}}"""

    # ── Parsing ─────────────────────────────────────────────────────────────────

    def _parse_response(self, raw: str) -> dict:
        # Strip markdown fences if present
        clean = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`")

        # Find JSON object
        match = re.search(r"\{.*\}", clean, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass

        # Fallback: treat entire response as thought
        return {
            "thought":         raw[:600],
            "belief":          None,
            "concepts":        [],
            "sentiment_toward": {},
            "action":          None,
            "publish":         raw[:300] if len(raw) > 50 else None,
        }

    # ── Event helper ────────────────────────────────────────────────────────────

    def _event(
        self,
        phase: str,
        thought: str,
        concepts: list = None,
        agreements: dict = None,
        publish: str = None,
        extra: dict = None,
    ) -> dict:
        event = {
            "agent_id":   self.id,
            "model":      self.model,
            "color":      self.color,
            "loop":       self._loop_count,
            "phase":      phase,
            "thought":    thought,
            "concepts":   concepts or [],
            "agreements": agreements or {},
            "publish":    publish,
        }
        if extra:
            event.update(extra)
        return event
