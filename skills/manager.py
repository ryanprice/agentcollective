"""
Skill Manager
-------------
Pulls skills from https://github.com/anthropics/skills
Maintains per-agent skill registry.

Agents discover skills via search_skills(query) — keyword matching against
skill names and descriptions. This prevents hallucination of non-existent
skill names.
"""

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Optional


SKILLS_REPO = "https://github.com/anthropics/skills.git"
REGISTRY_DIR = Path("skills/registry")
AGENT_REGISTRIES = Path("skills/agents")


class SkillManager:
    def __init__(self, agent_id: str, allowlist: list[str], repo_url: str = SKILLS_REPO,
                 local_dirs: list[str] | None = None):
        self.agent_id     = agent_id
        self.allowlist    = allowlist  # kept for config compat, no longer enforced
        self.repo_url     = repo_url
        self.registry_dir = REGISTRY_DIR
        self.local_dirs   = [Path(d) for d in (local_dirs or []) if Path(d).is_dir()]
        self.agent_dir    = AGENT_REGISTRIES / agent_id
        self.log_file     = self.agent_dir / "installed.json"
        self._ensure_dirs()
        self._available_cache: list[str] = []
        self._desc_cache: dict[str, str] = {}  # skill_name → description

    def _ensure_dirs(self):
        self.agent_dir.mkdir(parents=True, exist_ok=True)
        if not self.log_file.exists():
            self.log_file.write_text(json.dumps({"skills": []}))

    def installed(self) -> list[dict]:
        return json.loads(self.log_file.read_text())["skills"]

    def is_installed(self, skill_name: str) -> bool:
        return any(s["name"] == skill_name for s in self.installed())

    def available(self) -> list[str]:
        """Return list of skill names available across all sources."""
        names = set()
        # Registry clone
        skills_dir = self.registry_dir / "skills"
        if skills_dir.exists():
            names.update(
                p.name for p in skills_dir.iterdir()
                if p.is_dir() and not p.name.startswith(".")
            )
        # Local directories
        for local_dir in self.local_dirs:
            if local_dir.exists():
                names.update(
                    p.name for p in local_dir.iterdir()
                    if p.is_dir() and not p.name.startswith(".")
                )
        self._available_cache = sorted(names)
        return self._available_cache

    def _get_description(self, skill_name: str) -> str:
        """Get description for a skill (cached). Reads from SKILL.md frontmatter."""
        if skill_name in self._desc_cache:
            return self._desc_cache[skill_name]
        skill_path = self._find_skill_path(skill_name)
        if not skill_path:
            return ""
        md_path = skill_path / "SKILL.md"
        if not md_path.exists():
            return ""
        try:
            text = md_path.read_text(encoding="utf-8")[:2000]  # only need frontmatter
            desc = self._extract_description(text)
            self._desc_cache[skill_name] = desc
            return desc
        except Exception:
            return ""

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """
        Search available skills by keyword.
        Matches against skill name and description.
        Returns list of {name, description, score} sorted by relevance.
        """
        query = query.strip().lower()
        if not query:
            return []

        keywords = set(query.replace("-", " ").replace("_", " ").split())
        avail = self.available()
        results = []

        for name in avail:
            # Tokenize skill name
            name_tokens = set(name.replace("-", " ").replace("_", " ").lower().split())
            desc = self._get_description(name).lower()
            desc_tokens = set(desc.replace("-", " ").replace("_", " ").split())

            score = 0

            # Exact name match
            if query == name:
                score += 100

            # Query appears as substring of name
            if query in name:
                score += 50

            # Keyword overlap with name (strong signal)
            name_overlap = keywords & name_tokens
            score += len(name_overlap) * 20

            # Keyword overlap with description (weaker signal)
            desc_overlap = keywords & desc_tokens
            score += len(desc_overlap) * 5

            # Partial substring matches in name
            for kw in keywords:
                if any(kw in t for t in name_tokens):
                    score += 10
                if any(t in kw for t in name_tokens if len(t) > 2):
                    score += 5

            if score > 0:
                results.append({
                    "name": name,
                    "description": self._get_description(name)[:120],
                    "score": score,
                })

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:limit]

    def _fuzzy_suggest(self, skill_name: str, limit: int = 5) -> list[str]:
        """Suggest similar skill names for a failed install (typo correction)."""
        candidates = self.search(skill_name, limit=limit)
        if candidates:
            return [c["name"] for c in candidates]

        # Fallback: simple substring match on name parts
        parts = skill_name.replace("-", " ").replace("_", " ").split()
        avail = self.available()
        matches = []
        for name in avail:
            for part in parts:
                if part in name and len(part) > 2:
                    matches.append(name)
                    break
        return matches[:limit]

    def _find_skill_path(self, skill_name: str) -> Optional[Path]:
        """Search all sources for a skill directory containing SKILL.md."""
        # Check local dirs first
        for local_dir in self.local_dirs:
            candidate = local_dir / skill_name
            if (candidate / "SKILL.md").exists():
                return candidate
        # Then registry clone
        candidate = self.registry_dir / "skills" / skill_name
        if (candidate / "SKILL.md").exists():
            return candidate
        return None

    async def install(self, skill_name: str) -> dict:
        """Install a skill by name. Searches local dirs then registry."""
        skill_name = skill_name.strip().lower().replace(" ", "-")

        if self.is_installed(skill_name):
            return {"ok": True, "skill": skill_name, "status": "already_installed"}

        skill_path = self._find_skill_path(skill_name)
        if not skill_path:
            # Not in local dirs or registry — try sparse clone from repo
            ok = await self._sparse_clone(skill_name)
            if ok:
                skill_path = self.registry_dir / "skills" / skill_name
            else:
                suggestions = self._fuzzy_suggest(skill_name)
                if suggestions:
                    hint = f"Did you mean: {', '.join(suggestions)}? Use search_skills to find the right name."
                else:
                    hint = "Use search_skills with keywords to find available skills."
                return {"ok": False, "skill": skill_name, "error": f"Skill '{skill_name}' not found. {hint}"}

        skill_md_path = skill_path / "SKILL.md"
        if not skill_md_path.exists():
            return {"ok": False, "skill": skill_name, "error": "SKILL.md not found"}

        skill_md    = skill_md_path.read_text(encoding="utf-8")
        description = self._extract_description(skill_md)

        data = json.loads(self.log_file.read_text())
        data["skills"].append({
            "name":         skill_name,
            "installed_at": time.time(),
            "description":  description,
            "path":         str(skill_path),
        })
        self.log_file.write_text(json.dumps(data, indent=2))

        return {"ok": True, "skill": skill_name, "status": "installed", "description": description}

    def read_skill(self, skill_name: str) -> Optional[str]:
        """Return SKILL.md contents for an installed skill."""
        skill_path = self._find_skill_path(skill_name)
        if skill_path:
            return (skill_path / "SKILL.md").read_text(encoding="utf-8")
        return None

    def skill_count(self) -> int:
        """Return total number of available skills."""
        return len(self.available())

    async def _sparse_clone(self, skill_name: str) -> bool:
        """
        Sparse clone just the skill folder from the repo.
        Falls back to full clone if git sparse-checkout unavailable.
        """
        try:
            self.registry_dir.mkdir(parents=True, exist_ok=True)
            repo_dir = self.registry_dir

            if not (repo_dir / ".git").exists():
                # Init sparse repo
                proc = await asyncio.create_subprocess_exec(
                    "git", "clone", "--filter=blob:none", "--sparse",
                    self.repo_url, str(repo_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()

            # Add skill to sparse checkout
            proc = await asyncio.create_subprocess_exec(
                "git", "sparse-checkout", "add", f"skills/{skill_name}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(repo_dir),
            )
            await proc.communicate()
            return (repo_dir / "skills" / skill_name).exists()
        except Exception:
            return False

    def _extract_description(self, skill_md: str) -> str:
        match = re.search(r"description:\s*(.+?)(?:\n[a-z]|\Z)", skill_md, re.DOTALL)
        if match:
            return match.group(1).strip()[:200]
        return skill_md[:200]
