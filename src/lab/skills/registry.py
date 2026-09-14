"""Discover SKILL.md files and disclose metadata until invoke()."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class SkillError(ValueError):
    pass


@dataclass(frozen=True)
class SkillSpec:
    name: str
    description: str
    body: str
    version: str
    path: str

    def catalog_entry(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
        }


def _parse_frontmatter(text: str, path: Path) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        raise SkillError(f"missing_frontmatter:{path}")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise SkillError(f"invalid_frontmatter:{path}")
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as exc:
        raise SkillError(f"illegal_yaml:{path}:{exc}") from exc
    if not isinstance(meta, dict):
        raise SkillError(f"frontmatter_not_object:{path}")
    body = parts[2].strip()
    return meta, body


class SkillRegistry:
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        self._by_name: dict[str, SkillSpec] = {}

    def load_directory(self) -> None:
        if not self.root.is_dir():
            raise SkillError(f"missing_skill_root:{self.root}")
        for path in sorted(self.root.rglob("SKILL.md")):
            self.register_file(path)

    def register_file(self, path: Path | str) -> SkillSpec:
        raw = Path(path).resolve()
        try:
            raw.relative_to(self.root)
        except ValueError as exc:
            raise SkillError(f"path_outside_root:{raw}") from exc
        if not raw.is_file():
            raise SkillError(f"missing_file:{raw}")
        meta, body = _parse_frontmatter(raw.read_text(encoding="utf-8"), raw)
        name = meta.get("name")
        description = meta.get("description")
        if not isinstance(name, str) or not name.strip():
            raise SkillError(f"missing_name:{raw}")
        if not isinstance(description, str) or not description.strip():
            raise SkillError(f"missing_description:{raw}")
        if name in self._by_name:
            raise SkillError(f"duplicate_name:{name}")
        version = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
        spec = SkillSpec(
            name=name.strip(),
            description=description.strip(),
            body=body,
            version=version,
            path=str(raw),
        )
        self._by_name[spec.name] = spec
        return spec

    def catalog(self) -> list[dict[str, Any]]:
        return [s.catalog_entry() for s in self._by_name.values()]

    def get(self, name: str) -> SkillSpec | None:
        return self._by_name.get(name)

    def invoke(self, name: str) -> dict[str, Any]:
        spec = self.get(name)
        if spec is None:
            return {
                "error_code": "unknown_skill",
                "retryable": True,
                "available": sorted(self._by_name),
            }
        return {
            "name": spec.name,
            "description": spec.description,
            "version": spec.version,
            "body": spec.body,
        }

    def names(self) -> list[str]:
        return sorted(self._by_name)
