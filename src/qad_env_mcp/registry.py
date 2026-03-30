"""
Local environment registry backed by ~/.qad/environments.yaml.

Stores env_id -> metadata mappings with optional aliases, tags,
descriptions, and owner info. Aliases allow referring to environments
by friendly names instead of opaque IDs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

REGISTRY_DIR = Path.home() / ".qad"
REGISTRY_FILE = REGISTRY_DIR / "environments.yaml"


@dataclass
class EnvironmentEntry:
    """A single registered environment."""

    env_id: str
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    tags: list[str] = field(default_factory=list)
    owner: str = ""

    def to_dict(self) -> dict:
        d: dict = {"env_id": self.env_id}
        if self.aliases:
            d["aliases"] = self.aliases
        if self.description:
            d["description"] = self.description
        if self.tags:
            d["tags"] = self.tags
        if self.owner:
            d["owner"] = self.owner
        return d

    @classmethod
    def from_dict(cls, data: dict) -> EnvironmentEntry:
        return cls(
            env_id=data["env_id"],
            aliases=data.get("aliases", []),
            description=data.get("description", ""),
            tags=data.get("tags", []),
            owner=data.get("owner", ""),
        )


class EnvironmentRegistry:
    """Read/write registry of QAD environments."""

    def __init__(self, path: Path = REGISTRY_FILE):
        self._path = path
        self._entries: dict[str, EnvironmentEntry] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            self._entries = {}
            return
        raw = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or "environments" not in raw:
            self._entries = {}
            return
        for item in raw["environments"]:
            entry = EnvironmentEntry.from_dict(item)
            self._entries[entry.env_id] = entry

    def save(self) -> None:
        """Write the registry back to disk."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "environments": [e.to_dict() for e in self._entries.values()]
        }
        self._path.write_text(
            yaml.dump(data, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )

    def resolve(self, name: str) -> str | None:
        """Resolve an alias or env_id to the canonical env_id. Returns None if not found."""
        name_lower = name.strip().lower()
        if name_lower in self._entries:
            return name_lower
        for entry in self._entries.values():
            if name_lower in (a.lower() for a in entry.aliases):
                return entry.env_id
        return None

    def get(self, name: str) -> EnvironmentEntry | None:
        """Look up an entry by env_id or alias."""
        env_id = self.resolve(name)
        if env_id is None:
            return None
        return self._entries.get(env_id)

    def add(
        self,
        env_id: str,
        alias: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        owner: str | None = None,
    ) -> EnvironmentEntry:
        """Register or update an environment. Merges aliases and tags with existing."""
        existing = self._entries.get(env_id)
        if existing:
            if alias and alias not in existing.aliases:
                existing.aliases.append(alias)
            if tags:
                for t in tags:
                    if t not in existing.tags:
                        existing.tags.append(t)
            if description:
                existing.description = description
            if owner:
                existing.owner = owner
            self.save()
            return existing

        entry = EnvironmentEntry(
            env_id=env_id,
            aliases=[alias] if alias else [],
            description=description or "",
            tags=tags or [],
            owner=owner or "",
        )
        self._entries[env_id] = entry
        self.save()
        return entry

    def remove(self, name: str) -> EnvironmentEntry | None:
        """Remove an environment by env_id or alias. Returns the removed entry."""
        env_id = self.resolve(name)
        if env_id is None:
            return None
        entry = self._entries.pop(env_id)
        self.save()
        return entry

    def add_alias(self, name: str, alias: str) -> EnvironmentEntry | None:
        """Add an alias to an existing environment. Returns updated entry or None."""
        entry = self.get(name)
        if entry is None:
            return None
        if alias not in entry.aliases:
            entry.aliases.append(alias)
            self.save()
        return entry

    def list_all(self) -> list[EnvironmentEntry]:
        """Return all registered environments."""
        return list(self._entries.values())

    def search(self, query: str) -> list[EnvironmentEntry]:
        """Search environments by matching query against env_id, aliases, tags, description, owner."""
        q = query.strip().lower()
        results = []
        for entry in self._entries.values():
            searchable = " ".join([
                entry.env_id,
                " ".join(entry.aliases),
                " ".join(entry.tags),
                entry.description,
                entry.owner,
            ]).lower()
            if q in searchable:
                results.append(entry)
        return results
