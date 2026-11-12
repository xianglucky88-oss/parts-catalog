from decimal import Decimal
from pathlib import Path
import tempfile

from parts_catalog.models import Quote
from parts_catalog.normalize import normalize_part, parse_package_quantity, to_quote
from parts_catalog.pipeline import PartsPipeline
from parts_catalog.sources import JsonFixtureSource
from parts_catalog.storage import PartsStore
from parts_catalog.unit_cost import (
    Dimension,
    IncomparabilityReason,
    MeasurementBasis,
    UnitCostStatus,
    compare_unit_costs,
    evaluate_unit_cost,
)

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "unit_cost_payload.json"


def _quote(unit, price, package_quantity=None, supplier="S", currency="USD"):
    return Quote(
        canonical_number="FX100",
        supplier=supplier,
        currency=currency,
        price=Decimal(price) if price is not None else None,
        lead_time_days=None,
        stock_status="available",
        source_ref="test",
        unit=unit,
        package_quantity=package_quantity,
    )


# --- packaging quantity parsing: missing is an explicit unknown, never a guess ---

def test_missing_package_quantity_is_preserved_as_none():
    part = normalize_part(
        {"supplier": "S", "part_number": "FX-100", "brand": "ACME", "unit": "BOX", "price": "12.00"}
    )
    assert part.package_quantity is None


def test_null_and_empty_package_quantity_become_none():
    assert parse_package_quantity({"package_quantity": None}) is None
    assert parse_package_quantity({"package_quantity": ""}) is None
    assert parse_package_quantity({}) is None


def test_invalid_package_quantity_is_rejected_not_defaulted():
    for bad in (0, -5, 1.5, "two"):
        try:
            parse_package_quantity({"package_quantity": bad}, "FX-100")
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {bad!r}")


def test_quote_carries_unit_and_nullable_package_quantity():
    part = normalize_part(
        {"supplier": "S", "part_number": "FX-100", "brand": "ACME", "unit": "box", "price": "12.00"}
    )
    quote = to_quote(part, "usd", "fixture")
    assert quote.unit == "BOX"
    assert quote.package_quantity is None
    assert quote.currency == "USD"


# --- per-each: price already is the provable unit cost ---

def test_per_each_quote_gives_exact_unit_cost_with_factor_one():
    result = evaluate_unit_cost(_quote("EA", "0.15", package_quantity=1))
    assert result.status is UnitCostStatus.EXACT
    assert result.measurement_basis is MeasurementBasis.PER_EACH
    assert result.dimension is Dimension.COUNT
    assert result.unit_cost == Decimal("0.15")
    assert result.conversion_factor == Decimal(1)
    assert result.reason is None


# --- per-package: exact only when the conversion is fully provable ---

def test_package_quote_with_known_quantity_gives_exact_divided_cost():
    result = evaluate_unit_cost(_quote("BOX", "12.00", package_quantity=100))
    assert result.status is UnitCostStatus.EXACT
    assert result.measurement_basis is MeasurementBasis.PER_PACKAGE
    assert result.unit_cost == Decimal("12.00") / Decimal(100)
    assert result.unit_cost == Decimal("0.12")
    assert result.conversion_factor == Decimal(1) / Decimal(100)
    assert "100" in result.conversion_basis


def test_package_quote_with_missing_quantity_is_indeterminate():
    result = evaluate_unit_cost(_quote("CASE", "48.00", package_quantity=None))
    assert result.status is UnitCostStatus.INDETERMINATE
    assert result.reason is IncomparabilityReason.PACKAGE_QUANTITY_MISSING
    assert result.measurement_basis is MeasurementBasis.PER_PACKAGE
    assert result.unit_cost is None
    assert result.conversion_factor is None


# --- incompatible dimensions and unknown units stay decidable, never guessed ---

def test_mass_unit_is_dimension_incompatible_with_each():
    result = evaluate_unit_cost(_quote("KG", "9.40", package_quantity=1))
    assert result.status is UnitCostStatus.INDETERMINATE
    assert result.dimension is Dimension.MASS
    assert result.measurement_basis is MeasurementBasis.INCOMPATIBLE_DIMENSION
    assert result.reason is IncomparabilityReason.INCOMPATIBLE_DIMENSION
    assert result.unit_cost is None


def test_unregistered_unit_is_reported_as_unknown():
    result = evaluate_unit_cost(_quote("PALLET", "60.00", package_quantity=500))
    assert result.status is UnitCostStatus.INDETERMINATE
    assert result.dimension is Dimension.UNKNOWN
    assert result.reason is IncomparabilityReason.UNKNOWN_UNIT
    assert result.unit_cost is None


def test_missing_price_blocks_exact_cost_even_for_each():
    result = evaluate_unit_cost(_quote("BOX", None, package_quantity=50))
    assert result.status is UnitCostStatus.INDETERMINATE
    assert result.reason is IncomparabilityReason.PRICE_MISSING
    assert result.unit_cost is None


# --- cross-quote comparison: only provable, same-currency costs are ranked ---

def test_comparison_ranks_exact_same_currency_quotes():
    comparison = compare_unit_costs([
        _quote("EA", "0.15", package_quantity=1, supplier="Acme Direct"),
        _quote("BOX", "12.00", package_quantity=100, supplier="Bolt Wholesale"),
    ])
    assert comparison.comparable is True
    assert comparison.reason is None
    assert [rank.supplier for rank in comparison.ranking] == ["Bolt Wholesale", "Acme Direct"]
    assert comparison.ranking[0].unit_cost == Decimal("0.12")
    assert comparison.ranking[0].rank == 1
    assert comparison.ranking[1].rank == 2
    assert comparison.incomparable == ()


def test_comparison_with_any_indeterminate_quote_is_unranked_with_reasons():
    comparison = compare_unit_costs([
        _quote("EA", "0.15", package_quantity=1, supplier="Acme Direct"),
        _quote("CASE", "48.00", package_quantity=None, supplier="Crate Supply"),
        _quote("KG", "9.40", package_quantity=1, supplier="Metric Metals"),
    ])
    assert comparison.comparable is False
    assert comparison.reason is IncomparabilityReason.PACKAGE_QUANTITY_MISSING
    assert comparison.ranking == ()
    reasons = {item.supplier: item.reason for item in comparison.incomparable}
    assert reasons["Crate Supply"] is IncomparabilityReason.PACKAGE_QUANTITY_MISSING
    assert reasons["Metric Metals"] is IncomparabilityReason.INCOMPATIBLE_DIMENSION
    # A provable quote inside an unranked group is simply not offered as comparable.
    assert "Acme Direct" not in reasons


def test_currency_mismatch_blocks_ranking_without_any_fx_conversion():
    comparison = compare_unit_costs([
        _quote("EA", "0.15", package_quantity=1, supplier="USD Shop", currency="USD"),
        _quote("EA", "0.10", package_quantity=1, supplier="EUR Shop", currency="EUR"),
    ])
    assert comparison.comparable is False
    assert comparison.reason is IncomparabilityReason.CURRENCY_MISMATCH
    assert comparison.ranking == ()
    assert {item.supplier for item in comparison.incomparable} == {"USD Shop", "EUR Shop"}
    assert all(item.reason is IncomparabilityReason.CURRENCY_MISMATCH for item in comparison.incomparable)


def test_empty_comparison_is_a_decided_non_result():
    comparison = compare_unit_costs([])
    assert comparison.comparable is False
    assert comparison.ranking == ()
    assert comparison.incomparable == ()


# --- end to end: persistence keeps packaging, basis and reasons ---

def test_pipeline_persists_packaging_basis_and_reasons():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "parts.sqlite3"
        store = PartsStore(db)
        try:
            result = PartsPipeline(JsonFixtureSource(FIXTURE), store).run("USD", str(FIXTURE))
            assert result.processed == 6
            assert result.exact_unit_costs == 2
            assert result.incomparable == 4
            stored = store.fetch_unit_costs("FX100")
            quote_rows = store.connection.execute(
                "SELECT supplier, unit, package_quantity FROM quotes ORDER BY supplier"
            ).fetchall()
        finally:
            store.close()

    by_supplier = {row.supplier: row for row in stored}
    assert by_supplier["Acme Direct"].status is UnitCostStatus.EXACT
    assert by_supplier["Acme Direct"].unit_cost == Decimal("0.15")
    box = by_supplier["Bolt Wholesale"]
    assert box.status is UnitCostStatus.EXACT
    assert box.unit_cost == Decimal("0.12")
    assert box.package_quantity == 100
    assert box.measurement_basis is MeasurementBasis.PER_PACKAGE
    assert by_supplier["Crate Supply"].reason is IncomparabilityReason.PACKAGE_QUANTITY_MISSING
    assert by_supplier["Crate Supply"].package_quantity is None
    assert by_supplier["Metric Metals"].reason is IncomparabilityReason.INCOMPATIBLE_DIMENSION
    assert by_supplier["Odd Traders"].reason is IncomparabilityReason.UNKNOWN_UNIT
    assert by_supplier["Pending Quoter"].reason is IncomparabilityReason.PRICE_MISSING

    # Quotes retain the packaging dimension that feeds the evaluation.
    quote_map = {row[0]: (row[1], row[2]) for row in quote_rows}
    assert quote_map["Acme Direct"] == ("EA", 1)
    assert quote_map["Crate Supply"] == ("CASE", None)
