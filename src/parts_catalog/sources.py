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
        if isinstance(payload, dict) and isinstance(payload.get("parts"), list):
            payload = payload["parts"]
        if not isinstance(payload, list):
            raise ValueError("supplier payload must be a list or an object with a 'parts' list")
        return payload


class JsonHandoverSource:
    """Read entity/alias declarations shipped with an acquisition handover.

    Expected shape::

        {
          "entities": [{"entity_id": "...", "display_name": "..."}, ...],
          "aliases": [{"alias": "...", "entity_id": "...", "basis": "...",
                       "source_ref": "...", "confirmed": true}, ...]
        }
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def read(self) -> dict:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("handover payload must be an object")
        entities = payload.get("entities", [])
        aliases = payload.get("aliases", [])
        if not isinstance(entities, list) or not isinstance(aliases, list):
            raise ValueError("handover entities and aliases must be lists")
        return payload


def apply_handover(registry, payload: dict, default_source_ref: str) -> None:
    """Load handover entity/alias declarations into the registry.

    Idempotent: re-applying the same handover material neither duplicates
    entities nor accumulates alias claims.
    """
    for item in payload.get("entities", []):
        registry.create_entity(
            str(item["entity_id"]),
            str(item["display_name"]),
            created_by=str(item.get("created_by", "handover")),
        )
    for item in payload.get("aliases", []):
        registry.register_alias(
            str(item["alias"]),
            str(item["entity_id"]),
            basis=str(item.get("basis", "handover")),
            source_ref=str(item.get("source_ref", default_source_ref)),
            confirmed=bool(item.get("confirmed", True)),
        )
