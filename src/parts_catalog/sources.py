from __future__ import annotations
import json
from pathlib import Path
from typing import Protocol
from .models import RawPartEnvelope


class PartSource(Protocol):
    def read(self) -> list[RawPartEnvelope]: ...


class JsonFixtureSource:
    """Reads supplier payloads into envelopes with per-record provenance.

    Supports the legacy bare list of records (supplier per record, no version)
    and versioned documents of the form
    {"supplier": ..., "contract_version": ..., "records": [...]}.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def read(self) -> list[RawPartEnvelope]:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        origin = str(self.path)
        if isinstance(payload, list):
            return [
                self._envelope(item, origin, f"$[{index}]",
                               supplier=item.get("supplier"),
                               contract_version=item.get("contract_version"))
                for index, item in enumerate(payload)
            ]
        if isinstance(payload, dict):
            records = payload.get("records")
            if not isinstance(records, list):
                raise ValueError("supplier payload document must contain a 'records' list")
            return [
                self._envelope(item, origin, f"$.records[{index}]",
                               supplier=payload.get("supplier"),
                               contract_version=payload.get("contract_version"))
                for index, item in enumerate(records)
            ]
        raise ValueError("supplier payload must be a list or a document with 'records'")

    @staticmethod
    def _envelope(record, origin: str, locator: str, *, supplier, contract_version) -> RawPartEnvelope:
        if not isinstance(record, dict):
            raise ValueError(f"record at {locator} must be a JSON object")
        return RawPartEnvelope(
            record=record,
            supplier=str(supplier) if supplier is not None else None,
            contract_version=str(contract_version) if contract_version is not None else None,
            origin=origin,
            locator=locator,
        )
