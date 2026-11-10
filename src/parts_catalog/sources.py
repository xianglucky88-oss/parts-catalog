from __future__ import annotations
import json
from pathlib import Path
from typing import Protocol


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
