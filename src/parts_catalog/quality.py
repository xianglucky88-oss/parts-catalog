"""Versioned business-quality policy for adapted supplier records.

The source adapters (:mod:`parts_catalog.normalize` / :mod:`parts_catalog.sources`)
are responsible for turning portal payloads into the unified
:class:`~parts_catalog.models.SupplierPart` shape.  This module intentionally
does **not** choose an adapter, parse nested portal fields or judge the raw
contract version.  It applies a *versioned business-quality policy* on top of
records that have already been adapted (or whose adaptation failed) and decides
whether each record may enter the business database:

``accepted``  -> catalog part + quote are written
``quarantined`` -> identity is usable but coverage/consistency is degraded;
                   the record and its field-level reasons are parked
``rejected``  -> critical identity missing or the record cannot be adapted at
                 all; it must never enter the business database

Policy versioning (:data:`QUALITY_POLICY_VERSION`) makes every stored verdict
replayable against the exact rule set that produced it.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from .models import SupplierPart
from .normalize import canonical_brand, canonical_part_number, normalize_part

QUALITY_POLICY_VERSION = "1.0"

ACCEPTED = "accepted"
QUARANTINED = "quarantined"
REJECTED = "rejected"
_VERDICTS = (ACCEPTED, QUARANTINED, REJECTED)

#: Identity fields without which a record cannot be trusted at all.
IDENTITY_FIELDS = ("supplier", "part_number", "brand")

#: Business fields used for the field-coverage ratio.  ``supplier`` /
#: ``part_number`` / ``brand`` are critical identity, not coverage fields.
COVERAGE_FIELDS = (
    "description",
    "unit",
    "package_quantity",
    "lead_time_days",
    "stock_status",
    "price",
)

#: Minimum fraction of :data:`COVERAGE_FIELDS` that must carry a usable value.
MIN_COVERAGE_RATIO = 0.5

#: Structural/identity attributes that must stay consistent across records of
#: the same supplier + canonical part number.  Price, lead time and stock are
#: quotation attributes: distinct offers may legitimately differ, so they are
#: not contradictions.  Description is free text and spelling may vary between
#: portal rows, so it is excluded as well.
CONSISTENCY_FIELDS = ("brand", "unit", "package_quantity")

# Reason codes --------------------------------------------------------------
MISSING_IDENTITY = "missing_identity"
MISSING_FIELD = "missing_field"
LOW_COVERAGE = "low_coverage"
ADAPTATION_ERROR = "adaptation_error"
CONTRADICTORY_VALUE = "contradictory_value"

_MISSING_TOKENS = ("", "n/a", "na", "-", "--", "null", "none", "unknown")


def _is_present(value: Any) -> bool:
    """Return whether a raw field carries a usable business value."""
    if value is None:
        return False
    if isinstance(value, str) and value.strip().lower() in _MISSING_TOKENS:
        return False
    return True


@dataclass(frozen=True)
class FieldReason:
    """A single field-level quality finding."""

    field: str
    code: str
    message: str
    value: Any = None

    def as_dict(self) -> dict:
        return {"field": self.field, "code": self.code, "message": self.message, "value": self.value}


@dataclass(frozen=True)
class QualityDecision:
    """Verdict for one adapted (or un-adaptable) record."""

    verdict: str
    policy_version: str
    supplier: str
    part_number: str
    coverage_ratio: float
    reasons: tuple[FieldReason, ...] = ()
    part: SupplierPart | None = None
    raw: dict | None = None
    fingerprint: str = ""

    def reason_dicts(self) -> list[dict]:
        return [reason.as_dict() for reason in self.reasons]


@dataclass
class SupplierQualityProfile:
    """Aggregate quality profile for one supplier (one run / policy version)."""

    supplier: str
    policy_version: str
    total: int = 0
    accepted: int = 0
    quarantined: int = 0
    rejected: int = 0
    coverage_sum: float = 0.0
    missing_counts: dict = field(default_factory=dict)

    @property
    def average_coverage(self) -> float:
        return round(self.coverage_sum / self.total, 4) if self.total else 0.0

    def as_dict(self) -> dict:
        return {
            "supplier": self.supplier,
            "policy_version": self.policy_version,
            "total": self.total,
            "accepted": self.accepted,
            "quarantined": self.quarantined,
            "rejected": self.rejected,
            "average_coverage": self.average_coverage,
            "missing_field_counts": dict(sorted(self.missing_counts.items())),
        }


@dataclass(frozen=True)
class ImportReport:
    """Full outcome of one quality-gated import run."""

    policy_version: str
    decisions: tuple[QualityDecision, ...]
    profiles: tuple[SupplierQualityProfile, ...]

    @property
    def totals(self) -> dict:
        counts = {verdict: 0 for verdict in _VERDICTS}
        for decision in self.decisions:
            counts[decision.verdict] += 1
        counts["total"] = len(self.decisions)
        counts["policy_version"] = self.policy_version
        return counts


class QualityPolicy:
    """Stateless versioned rules; one instance per policy version."""

    version = QUALITY_POLICY_VERSION
    min_coverage_ratio = MIN_COVERAGE_RATIO

    def __init__(self, version: str = QUALITY_POLICY_VERSION, min_coverage_ratio: float = MIN_COVERAGE_RATIO) -> None:
        self.version = version
        self.min_coverage_ratio = min_coverage_ratio

    # -- per-record checks --------------------------------------------------
    def _identity_reasons(self, raw: dict) -> list[FieldReason]:
        reasons: list[FieldReason] = []
        for name in IDENTITY_FIELDS:
            value = raw.get(name)
            if not _is_present(value):
                reasons.append(
                    FieldReason(name, MISSING_IDENTITY, f"critical identity field '{name}' is unavailable", value)
                )
                continue
            text = str(value).strip()
            if name == "part_number" and not canonical_part_number(text):
                reasons.append(
                    FieldReason(
                        name,
                        MISSING_IDENTITY,
                        "critical identity field 'part_number' has no usable canonical value",
                        value,
                    )
                )
            elif name == "brand" and not canonical_brand(text):
                reasons.append(
                    FieldReason(
                        name,
                        MISSING_IDENTITY,
                        "critical identity field 'brand' has no usable canonical value",
                        value,
                    )
                )
        return reasons

    def _coverage(self, raw: dict) -> tuple[float, list[FieldReason], list[str]]:
        reasons: list[FieldReason] = []
        missing: list[str] = []
        for name in COVERAGE_FIELDS:
            if not _is_present(raw.get(name)):
                missing.append(name)
                reasons.append(
                    FieldReason(name, MISSING_FIELD, f"field '{name}' is missing or unusable", raw.get(name))
                )
        ratio = round((len(COVERAGE_FIELDS) - len(missing)) / len(COVERAGE_FIELDS), 4)
        if ratio < self.min_coverage_ratio:
            reasons.append(
                FieldReason(
                    "coverage",
                    LOW_COVERAGE,
                    f"coverage {ratio:.2f} below minimum {self.min_coverage_ratio:.2f}",
                    ratio,
                )
            )
        return ratio, reasons, missing

    def _adaptation_reasons(self, raw: dict) -> list[FieldReason]:
        """Mirror adapter validation without raising, so failures carry reasons."""
        reasons: list[FieldReason] = []
        raw_price = raw.get("price")
        if _is_present(raw_price):
            try:
                Decimal(str(raw_price))
            except InvalidOperation:
                reasons.append(
                    FieldReason("price", ADAPTATION_ERROR, f"price {raw_price!r} cannot be parsed", raw_price)
                )
        if _is_present(raw.get("package_quantity")):
            try:
                quantity = int(raw.get("package_quantity"))
            except (TypeError, ValueError):
                reasons.append(
                    FieldReason(
                        "package_quantity",
                        ADAPTATION_ERROR,
                        f"package_quantity {raw.get('package_quantity')!r} is not an integer",
                        raw.get("package_quantity"),
                    )
                )
            else:
                if quantity < 1:
                    reasons.append(
                        FieldReason(
                            "package_quantity",
                            ADAPTATION_ERROR,
                            "package_quantity must be positive",
                            quantity,
                        )
                    )
        if raw.get("lead_time_days") is not None:
            try:
                int(raw["lead_time_days"])
            except (TypeError, ValueError):
                reasons.append(
                    FieldReason(
                        "lead_time_days",
                        ADAPTATION_ERROR,
                        f"lead_time_days {raw['lead_time_days']!r} is not an integer",
                        raw["lead_time_days"],
                    )
                )
        return reasons

    def evaluate_record(self, raw: dict) -> QualityDecision:
        """Classify one raw record without any cross-record context."""
        identity_reasons = self._identity_reasons(raw)
        coverage_ratio, coverage_reasons, _ = self._coverage(raw)
        adaptation_reasons = self._adaptation_reasons(raw)
        reasons = tuple(identity_reasons + adaptation_reasons + coverage_reasons)

        supplier = str(raw.get("supplier") or "").strip()
        part_number = canonical_part_number(str(raw.get("part_number") or ""))
        fingerprint = record_fingerprint(raw, self.version)

        if identity_reasons:
            return QualityDecision(
                REJECTED, self.version, supplier, part_number, coverage_ratio, reasons, None, raw, fingerprint
            )

        part = None
        if not adaptation_reasons:
            # Identity is present and validated; the adapter cannot raise here.
            part = normalize_part(raw)

        low_coverage = any(reason.code == LOW_COVERAGE for reason in coverage_reasons)
        if adaptation_reasons or low_coverage:
            verdict = QUARANTINED
        else:
            verdict = ACCEPTED
        return QualityDecision(
            verdict, self.version, supplier, part_number, coverage_ratio, reasons, part, raw, fingerprint
        )

    # -- batch level: contradiction detection + profiles -------------------
    def evaluate_batch(self, records: Iterable[dict]) -> ImportReport:
        decisions = [self.evaluate_record(raw) for raw in records]

        # Contradiction detection only among records whose identity is usable.
        groups: dict[tuple[str, str], list[int]] = {}
        for index, decision in enumerate(decisions):
            if decision.verdict == REJECTED or decision.part is None:
                continue
            groups.setdefault((decision.supplier, decision.part_number), []).append(index)

        for indexes in groups.values():
            # Pass 1: collect the distinct values observed per consistency field.
            distinct_values: dict[str, set] = {name: set() for name in CONSISTENCY_FIELDS}
            for index in indexes:
                part = decisions[index].part
                assert part is not None
                for name in CONSISTENCY_FIELDS:
                    distinct_values[name].add(getattr(part, name))

            # Pass 2: every record sharing a field whose values disagree is
            # quarantined and receives a field-level contradictory-value reason.
            for index in indexes:
                decision = decisions[index]
                part = decision.part
                assert part is not None
                for name in CONSISTENCY_FIELDS:
                    if len(distinct_values[name]) <= 1:
                        continue
                    value = getattr(part, name)
                    other_value = next(v for v in distinct_values[name] if v != value)
                    decision = _with_reason(
                        decision,
                        FieldReason(
                            name,
                            CONTRADICTORY_VALUE,
                            f"conflicting '{name}' values for {decision.supplier}/"
                            f"{decision.part_number}: {value!r} vs {other_value!r}",
                            value,
                        ),
                    )
                if decision.verdict == ACCEPTED and any(
                    reason.code == CONTRADICTORY_VALUE for reason in decision.reasons
                ):
                    decision = _escalate(decision, QUARANTINED)
                decisions[index] = decision

        profiles = self._build_profiles(decisions)
        return ImportReport(self.version, tuple(decisions), tuple(profiles))

    def _build_profiles(self, decisions: list[QualityDecision]) -> list[SupplierQualityProfile]:
        profiles: dict[str, SupplierQualityProfile] = {}
        for decision in decisions:
            key = decision.supplier or "<unidentified>"
            profile = profiles.setdefault(key, SupplierQualityProfile(key, self.version))
            profile.total += 1
            setattr(profile, decision.verdict, getattr(profile, decision.verdict) + 1)
            profile.coverage_sum += decision.coverage_ratio
            for reason in decision.reasons:
                if reason.code in (MISSING_FIELD, MISSING_IDENTITY):
                    profile.missing_counts[reason.field] = profile.missing_counts.get(reason.field, 0) + 1
        return [profiles[key] for key in sorted(profiles)]


def _with_reason(decision: QualityDecision, reason: FieldReason) -> QualityDecision:
    if any(existing.field == reason.field and existing.code == reason.code for existing in decision.reasons):
        return decision
    return QualityDecision(
        decision.verdict,
        decision.policy_version,
        decision.supplier,
        decision.part_number,
        decision.coverage_ratio,
        decision.reasons + (reason,),
        decision.part,
        decision.raw,
        decision.fingerprint,
    )


def _escalate(decision: QualityDecision, verdict: str) -> QualityDecision:
    return QualityDecision(
        verdict,
        decision.policy_version,
        decision.supplier,
        decision.part_number,
        decision.coverage_ratio,
        decision.reasons,
        decision.part,
        decision.raw,
        decision.fingerprint,
    )


def record_fingerprint(raw: dict, policy_version: str = QUALITY_POLICY_VERSION) -> str:
    """Stable SHA-256 over record content + policy version.

    Identical inputs always produce the same fingerprint, so replaying a
    payload upserts the same quarantine/decision rows instead of duplicating
    them.
    """
    payload = json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(policy_version.encode("utf-8") + b"|" + payload.encode("utf-8"))
    return digest.hexdigest()
