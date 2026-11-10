"""Memory you can open in Notepad. `r2 memory export` writes what R2 knows about you to a plain
markdown file; edit it, then `r2 memory import` reads it back. Your data, in your hands.

Each fact is one line:  `- [12] Prefers meetings after 11am`
  * a line without a [number] is a new fact and is added (passwords and card numbers are refused)
  * a line whose [number] you deleted is only removed from memory if you pass --prune
  * changing the text after a [number] replaces that fact"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from r2.memory import Memory, SecretRejected

HEADER = "# What R2 knows about you\n\nOne fact per line. Edit freely, then run: r2 memory import\n\n"
_LINE = re.compile(r"^\s*[-*]\s+(?:\[(\d+)\]\s+)?(.+?)\s*$")


@dataclass
class ImportReport:
    added: int = 0
    changed: int = 0
    removed: int = 0
    refused: list[str] = field(default_factory=list)  # lines that look like secrets

    def summary(self) -> str:
        text = f"Added {self.added}, changed {self.changed}, removed {self.removed}."
        if self.refused:
            text += f" Refused {len(self.refused)} line(s) that look like passwords or card numbers."
        return text


def export_facts(memory: Memory, path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    facts = memory.list_facts()
    body = "".join(f"- [{f.id}] {f.text}\n" for f in facts)
    path.write_text(HEADER + body, encoding="utf-8")
    return len(facts)


def import_facts(memory: Memory, path, prune: bool = False) -> ImportReport:
    report = ImportReport()
    existing = {f.id: f for f in memory.list_facts()}
    seen: set[int] = set()
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        m = _LINE.match(raw)
        if not m:
            continue
        fact_id = int(m.group(1)) if m.group(1) else None
        text = m.group(2)
        if fact_id is not None and fact_id in existing:
            seen.add(fact_id)
            if existing[fact_id].text == text:
                continue
            try:
                memory.update_fact(fact_id, text, "file")
            except SecretRejected:
                report.refused.append(text[:40])
                continue
            report.changed += 1
        elif fact_id is None and text not in {f.text for f in existing.values()}:
            try:
                memory.save_fact(text, "file")
            except SecretRejected:
                report.refused.append(text[:40])
                continue
            report.added += 1
    if prune:
        for fact_id in set(existing) - seen:
            if memory.delete_fact(fact_id):
                report.removed += 1
    return report
