"""Reusable skills: short, plain-text recipes that teach R2 how to do a recurring job your way.

A skill is a folder with a SKILL.md (the open agentskills.io layout): a small header with `name` and
`description`, then the instructions. R2 sees only the one-line index in its prompt, and reads a skill's
full text with the `use_skill` tool when a request matches, so many skills cost almost nothing.

R2 may *propose* a skill (`propose_skill`) after it solves something. A proposal is only a file in
`<data>/skills/_proposed/`; it does nothing until you approve it with `r2 skills approve NAME`.
Skills are instructions only. They cannot run code and they cannot widen what R2's tools are allowed to do."""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from r2.memory import _has_secret

NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_NAME = 64
MAX_DESCRIPTION = 300
MAX_BODY = 6000
INDEX_LIMIT = 12  # skills listed in the prompt; a small prompt is a fast prompt
BUNDLED = Path(__file__).resolve().parent.parent / "skills"


class SkillError(ValueError):
    pass


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    path: Path
    source: str  # "bundled" | "user"


def parse(text: str) -> tuple[dict[str, str], str]:
    """Split `---` header from body. The header is plain `key: value` lines (no YAML library needed)."""
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillError("SKILL.md must start with a --- header containing name and description")
    meta: dict[str, str] = {}
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return meta, "\n".join(lines[i + 1:]).strip()
        key, sep, value = lines[i].partition(":")
        if sep:
            meta[key.strip().lower()] = value.strip().strip("\"'")
    raise SkillError("the --- header is never closed")


def validate(name: str, description: str, body: str) -> None:
    if not NAME.match(name) or len(name) > MAX_NAME:
        raise SkillError("name must be lowercase letters, digits and single hyphens, up to 64 characters")
    if not description or len(description) > MAX_DESCRIPTION:
        raise SkillError(f"description is required and must be at most {MAX_DESCRIPTION} characters")
    if not body.strip():
        raise SkillError("a skill needs instructions")
    if len(body) > MAX_BODY:
        raise SkillError(f"instructions are too long (limit {MAX_BODY} characters)")
    if _has_secret(body) or _has_secret(description):
        raise SkillError("looks like it contains a password or card number; skills must never hold secrets")


def load_one(directory: Path, source: str) -> Skill:
    file = directory / "SKILL.md"
    if not file.is_file():
        raise SkillError(f"{directory.name}: no SKILL.md")
    meta, body = parse(file.read_text(encoding="utf-8"))
    name, description = meta.get("name", ""), meta.get("description", "")
    validate(name, description, body)
    if name != directory.name:
        raise SkillError(f"name '{name}' must match its folder '{directory.name}'")
    return Skill(name, description, body, file, source)


class SkillStore:
    def __init__(self, data_dir, bundled: Path | None = BUNDLED):
        self.user_dir = Path(data_dir) / "skills"
        self.proposed_dir = self.user_dir / "_proposed"
        self._bundled = bundled
        self.problems: list[str] = []

    def _scan(self, root: Path | None, source: str) -> dict[str, Skill]:
        found: dict[str, Skill] = {}
        if root is None or not root.is_dir():
            return found
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name.startswith("_"):
                continue
            try:
                skill = load_one(child, source)
                found[skill.name] = skill
            except (SkillError, OSError) as exc:
                self.problems.append(f"{child.name}: {exc}")
        return found

    def all(self) -> list[Skill]:
        """Bundled skills, then yours; yours win when a name is used twice."""
        self.problems = []
        merged = self._scan(self._bundled, "bundled")
        merged.update(self._scan(self.user_dir, "user"))
        return sorted(merged.values(), key=lambda s: s.name)

    def get(self, name: str) -> Skill | None:
        return next((s for s in self.all() if s.name == name), None)

    def index(self) -> str:
        skills = self.all()[:INDEX_LIMIT]
        return "\n".join(f"- {s.name}: {s.description[:110]}" for s in skills)

    # ---- proposals: R2 writes them, you approve them ----
    def propose(self, name: str, description: str, instructions: str) -> str:
        validate(name, description, instructions)
        target = self.proposed_dir / name
        target.mkdir(parents=True, exist_ok=True)
        (target / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n{instructions.strip()}\n", encoding="utf-8"
        )
        return f"Saved as a proposal. It is inactive until you approve it: r2 skills approve {name}"

    def proposed(self) -> list[Skill]:
        if not self.proposed_dir.is_dir():
            return []
        out = []
        for child in sorted(self.proposed_dir.iterdir()):
            try:
                out.append(load_one(child, "proposed"))
            except (SkillError, OSError):
                continue
        return out

    def approve(self, name: str) -> Skill:
        source = self.proposed_dir / name
        if not NAME.match(name) or not source.is_dir():
            raise SkillError(f"no proposed skill called '{name}'")
        skill = load_one(source, "proposed")  # re-validated: the file may have been edited since
        destination = self.user_dir / name
        if destination.exists():
            raise SkillError(f"you already have a skill called '{name}'")
        shutil.move(str(source), str(destination))
        return load_one(destination, "user")

    def reject(self, name: str) -> bool:
        source = self.proposed_dir / name
        if not NAME.match(name) or not source.is_dir():
            return False
        shutil.rmtree(source)
        return True
