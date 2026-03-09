#!/usr/bin/env python3
"""
One-time cleanup: deduplicate EPISODIC entries across all agents.
Removes entries that are >50% word-overlap with earlier entries.
"""

import re
import sys
from pathlib import Path

MEMORY_DIR = Path(__file__).parent.parent / "memory"

def extract_entries(text: str, tier: str) -> tuple[str, list[str], str]:
    """Split text into before-tier, tier entries, and after-tier."""
    header = f"## [{tier}]"
    if header not in text:
        return text, [], ""
    start = text.index(header) + len(header)
    # Find next ## header
    next_h = re.search(r"\n## \[", text[start:])
    end = start + next_h.start() if next_h else len(text)

    before = text[:start]
    after = text[end:]
    section = text[start:end]

    entries = []
    for line in section.strip().splitlines():
        line = line.strip()
        if line.startswith("- "):
            entries.append(line)
    return before, entries, after


def dedup_entries(entries: list[str], threshold: float = 0.50) -> list[str]:
    """Remove entries that are >threshold word-overlap with any earlier entry."""
    kept = []
    kept_words = []
    removed = 0

    for entry in entries:
        text = re.sub(r"^- \[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]\s*", "", entry).lower().strip()
        new_words = set(text.split())
        if len(new_words) < 3:
            kept.append(entry)
            continue

        is_dup = False
        for prev_words in kept_words:
            if len(prev_words) > 3:
                overlap = len(new_words & prev_words) / max(len(new_words), len(prev_words))
                if overlap > threshold:
                    is_dup = True
                    break

        if is_dup:
            removed += 1
        else:
            kept.append(entry)
            kept_words.append(new_words)

    return kept, removed


def clean_agent(agent_dir: Path):
    agent_id = agent_dir.name
    working_path = agent_dir / "working.md"
    if not working_path.exists():
        return

    text = working_path.read_text(encoding="utf-8", errors="replace")
    before, entries, after = extract_entries(text, "EPISODIC")

    if not entries:
        print(f"  {agent_id}: no EPISODIC entries")
        return

    kept, removed = dedup_entries(entries)
    print(f"  {agent_id}: {len(entries)} entries → {len(kept)} kept, {removed} duplicates removed")

    if removed > 0:
        new_section = "\n" + "\n".join(kept) + "\n"
        new_text = before + new_section + after
        working_path.write_text(new_text, encoding="utf-8")


def main():
    print("Episodic memory cleanup")
    print("=" * 50)

    for agent_dir in sorted(MEMORY_DIR.iterdir()):
        if agent_dir.is_dir() and not agent_dir.name.startswith("."):
            clean_agent(agent_dir)

    # Also dedup SEMANTIC in core.md
    print("\nSemantic memory dedup")
    print("=" * 50)
    for agent_dir in sorted(MEMORY_DIR.iterdir()):
        if agent_dir.is_dir() and not agent_dir.name.startswith("."):
            core_path = agent_dir / "core.md"
            if not core_path.exists():
                continue
            text = core_path.read_text(encoding="utf-8", errors="replace")
            before, entries, after = extract_entries(text, "SEMANTIC")
            if not entries:
                print(f"  {agent_dir.name}: no SEMANTIC entries")
                continue
            kept, removed = dedup_entries(entries)
            print(f"  {agent_dir.name}: {len(entries)} → {len(kept)} kept, {removed} duplicates removed")
            if removed > 0:
                new_section = "\n" + "\n".join(kept) + "\n"
                new_text = before + new_section + after
                core_path.write_text(new_text, encoding="utf-8")

    print("\nDone!")


if __name__ == "__main__":
    main()
