"""Parse and validate the cross-runtime batch handoff envelope.

The canonical machine-readable contract lives in
``contract/batch-envelope.schema.json`` (shared with the Node.js collector).
This module performs equivalent structural validation with zero third-party
dependencies so the importer never trusts collector output blindly.
"""
from __future__ import annotations
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

CONTRACT_VERSION = "1.0"
RESULT_STATUSES = ("ok", "error")


class EnvelopeError(ValueError):
    """Raised when a batch envelope violates the handoff contract."""


@dataclass(frozen=True)
class ResultError:
    code: str
    message: str


@dataclass(frozen=True)
class BatchResult:
    status: str
    part: dict | None
    error: ResultError | None


@dataclass(frozen=True)
class BatchEnvelope:
    batch_id: str
    supplier: str
    queried_at: str
    query_part_number: str
    results: tuple[BatchResult, ...]
    notes: str | None


def _require_string(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EnvelopeError(f"envelope field '{key}' must be a non-empty string")
    return value


def _parse_queried_at(payload: dict) -> str:
    value = _require_string(payload, "queried_at")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise EnvelopeError(f"envelope field 'queried_at' is not ISO 8601: {value!r}") from exc
    return value


def _parse_result(item: object, index: int) -> BatchResult:
    if not isinstance(item, dict):
        raise EnvelopeError(f"result #{index} must be an object")
    status = item.get("status")
    if status not in RESULT_STATUSES:
        raise EnvelopeError(f"result #{index} has invalid status {status!r}; expected one of {RESULT_STATUSES}")
    if status == "ok":
        part = item.get("part")
        if not isinstance(part, dict):
            raise EnvelopeError(f"result #{index} is 'ok' but carries no part object")
        return BatchResult(status="ok", part=part, error=None)
    error = item.get("error")
    if not isinstance(error, dict):
        raise EnvelopeError(f"result #{index} is 'error' but carries no error object")
    code, message = error.get("code"), error.get("message")
    if not isinstance(code, str) or not code.strip() or not isinstance(message, str) or not message.strip():
        raise EnvelopeError(f"result #{index} error requires non-empty 'code' and 'message'")
    return BatchResult(status="error", part=None, error=ResultError(code=code, message=message))


def parse_envelope(payload: object) -> BatchEnvelope:
    if not isinstance(payload, dict):
        raise EnvelopeError("batch envelope must be a JSON object")
    version = payload.get("contract_version")
    if version != CONTRACT_VERSION:
        raise EnvelopeError(f"unsupported contract_version {version!r}; expected {CONTRACT_VERSION!r}")
    query = payload.get("query")
    if not isinstance(query, dict):
        raise EnvelopeError("envelope field 'query' must be an object")
    query_part_number = query.get("part_number")
    if not isinstance(query_part_number, str) or not query_part_number.strip():
        raise EnvelopeError("envelope field 'query.part_number' must be a non-empty string")
    results = payload.get("results")
    if not isinstance(results, list):
        raise EnvelopeError("envelope field 'results' must be an array")
    notes = payload.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise EnvelopeError("envelope field 'notes' must be a string when present")
    return BatchEnvelope(
        batch_id=_require_string(payload, "batch_id"),
        supplier=_require_string(payload, "supplier"),
        queried_at=_parse_queried_at(payload),
        query_part_number=query_part_number,
        results=tuple(_parse_result(item, index) for index, item in enumerate(results)),
        notes=notes,
    )


def load_envelope(path: str | Path) -> BatchEnvelope:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EnvelopeError(f"batch envelope is not valid JSON: {exc}") from exc
    return parse_envelope(payload)
