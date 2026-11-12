"""Packaging dimensions and provable unit costs.

This module works on top of quotes whose *amount* has already been validated.
It never invents a conversion: a precise per-each unit cost is produced only
when the quantity, the unit and the conversion relationship are all provable
from the quote itself.  Every other input yields a structured, decidable
result carrying an explicit reason.

Currency is treated only as a label.  There is deliberately no FX, currency
washing, historical-currency migration or exchange-rate logic here; quotes in
different currencies cannot be compared and are marked ``currency_mismatch``.
"""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal, getcontext, localcontext
from enum import Enum
from typing import Final

from .models import Quote

getcontext().prec = 28


class Dimension(str, Enum):
    """Physical dimension a unit measures."""

    COUNT = "count"
    MASS = "mass"
    LENGTH = "length"
    UNKNOWN = "unknown"


class MeasurementBasis(str, Enum):
    """What the quoted price actually covers."""

    PER_EACH = "per_each"
    PER_PACKAGE = "per_package"
    INCOMPATIBLE_DIMENSION = "incompatible_dimension"


class UnitCostStatus(str, Enum):
    EXACT = "exact"
    INDETERMINATE = "indeterminate"


class IncomparabilityReason(str, Enum):
    PRICE_MISSING = "price_missing"
    UNKNOWN_UNIT = "unknown_unit"
    PACKAGE_QUANTITY_MISSING = "package_quantity_missing"
    INCOMPATIBLE_DIMENSION = "incompatible_dimension"
    CURRENCY_MISMATCH = "currency_mismatch"


# Registry of accepted units and the dimension they measure.  Only COUNT units
# can be converted to a per-each cost; mass and length dimensions have no
# provable conversion into pieces.
UNIT_REGISTRY: Final[dict[str, Dimension]] = {
    "EA": Dimension.COUNT,
    "PCS": Dimension.COUNT,
    "PC": Dimension.COUNT,
    "PIECE": Dimension.COUNT,
    "BOX": Dimension.COUNT,
    "CASE": Dimension.COUNT,
    "PACK": Dimension.COUNT,
    "KG": Dimension.MASS,
    "G": Dimension.MASS,
    "M": Dimension.LENGTH,
    "MM": Dimension.LENGTH,
}


@dataclass(frozen=True)
class UnitCostResult:
    """Decidable outcome of evaluating one validated quote."""

    supplier: str
    canonical_number: str
    currency: str
    unit: str
    package_quantity: int | None
    dimension: Dimension
    measurement_basis: MeasurementBasis | None
    status: UnitCostStatus
    reason: IncomparabilityReason | None
    unit_cost: Decimal | None
    conversion_factor: Decimal | None
    conversion_basis: str | None

    @property
    def is_exact(self) -> bool:
        return self.status is UnitCostStatus.EXACT


def _indeterminate(
    quote: Quote,
    unit: str,
    dimension: Dimension,
    basis: MeasurementBasis | None,
    reason: IncomparabilityReason,
    conversion_basis: str | None = None,
) -> UnitCostResult:
    return UnitCostResult(
        supplier=quote.supplier,
        canonical_number=quote.canonical_number,
        currency=quote.currency,
        unit=unit,
        package_quantity=quote.package_quantity,
        dimension=dimension,
        measurement_basis=basis,
        status=UnitCostStatus.INDETERMINATE,
        reason=reason,
        unit_cost=None,
        conversion_factor=None,
        conversion_basis=conversion_basis,
    )


def evaluate_unit_cost(quote: Quote) -> UnitCostResult:
    """Evaluate one quote.

    The quoted amount is assumed valid (or explicitly missing).  The result is
    either an exact per-each cost with a fully recorded conversion, or an
    indeterminate result whose ``reason`` explains why no precise cost can be
    proven.  No speculative conversion is ever performed.
    """
    unit = (quote.unit or "").strip().upper()
    dimension = UNIT_REGISTRY.get(unit, Dimension.UNKNOWN)

    # Resolve the dimension/basis independently of the price first, so that the
    # packaging picture stays decidable even when an amount is missing.
    if dimension is Dimension.UNKNOWN:
        return _indeterminate(
            quote, unit, dimension, None, IncomparabilityReason.UNKNOWN_UNIT,
            conversion_basis=f"unit '{unit or '<empty>'}' is not a registered unit of measure",
        )

    if dimension is not Dimension.COUNT:
        basis = MeasurementBasis.INCOMPATIBLE_DIMENSION
        return _indeterminate(
            quote, unit, dimension, basis, IncomparabilityReason.INCOMPATIBLE_DIMENSION,
            conversion_basis=f"unit '{unit}' measures {dimension.value}; no provable conversion into an each",
        )

    if quote.price is None:
        return _indeterminate(
            quote, unit, dimension, None, IncomparabilityReason.PRICE_MISSING,
            conversion_basis="quoted price is missing",
        )

    if unit in ("EA", "PCS", "PC", "PIECE"):
        # A per-each quote: the price already is the unit cost, factor 1.
        return UnitCostResult(
            supplier=quote.supplier,
            canonical_number=quote.canonical_number,
            currency=quote.currency,
            unit=unit,
            package_quantity=quote.package_quantity,
            dimension=dimension,
            measurement_basis=MeasurementBasis.PER_EACH,
            status=UnitCostStatus.EXACT,
            reason=None,
            unit_cost=quote.price,
            conversion_factor=Decimal(1),
            conversion_basis=f"price quoted per each in unit '{unit}'; conversion factor 1",
        )

    # Package units (BOX/CASE/PACK): division is only provable when the package
    # quantity is known and positive.
    if quote.package_quantity is None:
        return _indeterminate(
            quote, unit, dimension, MeasurementBasis.PER_PACKAGE,
            IncomparabilityReason.PACKAGE_QUANTITY_MISSING,
            conversion_basis=f"unit '{unit}' is a package unit but package_quantity is unknown",
        )

    quantity = Decimal(quote.package_quantity)
    with localcontext() as ctx:
        ctx.prec = 28
        factor = Decimal(1) / quantity
        unit_cost = quote.price / quantity
    return UnitCostResult(
        supplier=quote.supplier,
        canonical_number=quote.canonical_number,
        currency=quote.currency,
        unit=unit,
        package_quantity=quote.package_quantity,
        dimension=dimension,
        measurement_basis=MeasurementBasis.PER_PACKAGE,
        status=UnitCostStatus.EXACT,
        reason=None,
        unit_cost=unit_cost,
        conversion_factor=factor,
        conversion_basis=(
            f"price quoted per {unit} of {quote.package_quantity} each; "
            f"unit_cost = price / {quote.package_quantity}"
        ),
    )


@dataclass(frozen=True)
class RankedQuote:
    supplier: str
    canonical_number: str
    currency: str
    unit_cost: Decimal
    rank: int


@dataclass(frozen=True)
class IncomparableQuote:
    supplier: str
    canonical_number: str
    reason: IncomparabilityReason


@dataclass(frozen=True)
class UnitCostComparison:
    canonical_number: str | None
    comparable: bool
    reason: IncomparabilityReason | None
    ranking: tuple[RankedQuote, ...]
    incomparable: tuple[IncomparableQuote, ...]


def compare_unit_costs(quotes: list[Quote]) -> UnitCostComparison:
    """Compare quotes for the same part on a provable per-each basis.

    A ranking is produced only when every quote yields an exact unit cost and
    all costs share one currency.  Otherwise the result is decidable but
    unranked, with a single reason and each unranked supplier listed.
    """
    if not quotes:
        return UnitCostComparison(None, False, IncomparabilityReason.PRICE_MISSING, (), ())

    canonical_number = quotes[0].canonical_number
    results = [evaluate_unit_cost(q) for q in quotes]

    incomparable = tuple(
        IncomparableQuote(r.supplier, r.canonical_number, r.reason)
        for r in results if r.reason is not None
    )
    exact = [r for r in results if r.is_exact]

    if incomparable:
        # Surface the first blocking reason deterministically; each supplier's
        # own reason is still preserved on the incomparable entries.
        reason = next(r.reason for r in results if r.reason is not None)
        return UnitCostComparison(canonical_number, False, reason, (), incomparable)

    currencies = {r.currency for r in exact}
    if len(currencies) > 1:
        mismatched = tuple(
            IncomparableQuote(r.supplier, r.canonical_number, IncomparabilityReason.CURRENCY_MISMATCH)
            for r in exact
        )
        return UnitCostComparison(
            canonical_number, False, IncomparabilityReason.CURRENCY_MISMATCH, (), mismatched
        )

    ordered = sorted(exact, key=lambda r: r.unit_cost)
    ranking = tuple(
        RankedQuote(
            supplier=r.supplier,
            canonical_number=r.canonical_number,
            currency=r.currency,
            unit_cost=r.unit_cost,
            rank=index + 1,
        )
        for index, r in enumerate(ordered)
    )
    return UnitCostComparison(canonical_number, True, None, ranking, ())
