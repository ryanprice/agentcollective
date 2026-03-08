"""
Skill Manager
-------------
Pulls skills from https://github.com/anthropics/skills
Maintains per-agent skill registry.
Only allowlisted skills can be installed.
"""

import asyncio
import json
import os
import re
import subprocess
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
                avail = self.available()
                hint  = f"Available skills: {avail}" if avail else "Could not fetch skill list."
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
