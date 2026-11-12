from __future__ import annotations
import json
from pathlib import Path
from typing import Protocol
from .models import ScopeEntry
from .normalize import to_scope_entry


class PartSource(Protocol):
    def read(self) -> list[dict]: ...


class JsonFixtureSource:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def read(self) -> list[dict]:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("supplier payload must be a list")
        return payload


class JsonScopeSource:
    """Reads a query-scope manifest: a JSON list of {"supplier", "part_number"}
    entries, normalized into the canonical cells a run pins at start."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def read(self) -> list[ScopeEntry]:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("scope manifest must be a list")
        return [to_scope_entry(raw) for raw in payload]
