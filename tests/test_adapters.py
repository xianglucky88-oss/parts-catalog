import json
import tempfile
from pathlib import Path

import pytest

from parts_catalog.adapters import (
    AdapterRegistry,
    NorthstarV1Adapter,
    RecordContractViolationError,
    SourceBatchRejectedError,
    UnknownContractError,
    VertexV21Adapter,
    default_registry,
)
from parts_catalog.pipeline import PartsPipeline
from parts_catalog.sources import JsonFixtureSource
from parts_catalog.storage import PartsStore

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def run_payload(payload, source_ref="payload.json"):
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "payload.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    store = PartsStore(":memory:")
    pipeline = PartsPipeline(JsonFixtureSource(path), store, default_registry())
    return tmp, pipeline, store, source_ref


def rows(store, query):
    return store.connection.execute(query).fetchall()


def test_legacy_fixture_keeps_importing_with_traceable_refs():
    store = PartsStore(":memory:")
    pipeline = PartsPipeline(
        JsonFixtureSource(FIXTURES / "supplier_payload.json"), store, default_registry())
    count = pipeline.run("USD", "fixtures/supplier_payload.json")
    assert count == 2
    assert rows(store, "SELECT canonical_number, brand, description, unit, package_quantity FROM parts") == [
        ("AB1204", "ACME", "Sealed bearing 20mm", "EA", 1),
    ]
    quotes = rows(store, "SELECT canonical_number, supplier, price, source_ref FROM quotes ORDER BY source_ref")
    assert quotes == [
        ("AB1204", "Northstar", "18.50", "fixtures/supplier_payload.json#$[0]"),
        ("AB1204", "Northstar", "18.75", "fixtures/supplier_payload.json#$[1]"),
    ]


def test_nested_vertex_fixture_is_adapted():
    fixture = FIXTURES / "supplier_payload_nested.json"
    store = PartsStore(":memory:")
    pipeline = PartsPipeline(JsonFixtureSource(fixture), store, default_registry())
    count = pipeline.run("USD", str(fixture))
    assert count == 2
    parts = rows(store, "SELECT canonical_number, brand, description, unit, package_quantity FROM parts ORDER BY canonical_number")
    assert parts == [
        ("TR552", "NORTHSTAR", "Thrust roller 55 mm", "EA", 1),
        ("VX8810Q", "VERTEX TOOLS", "Deep-groove bearing 25 mm", "BOX", 10),
    ]
    quotes = rows(store, "SELECT canonical_number, stock_status, price, lead_time_days, source_ref FROM quotes ORDER BY source_ref")
    assert quotes == [
        ("VX8810Q", "available", "102.40", 7, f"{fixture}#$.records[0]"),
        ("TR552", "backorder", None, None, f"{fixture}#$.records[1]"),
    ]


def test_registry_selects_adapter_by_supplier_and_version():
    registry = default_registry()
    assert isinstance(registry.resolve("Northstar", None), NorthstarV1Adapter)
    assert isinstance(registry.resolve(" northstar ", "1.0"), NorthstarV1Adapter)
    assert isinstance(registry.resolve("Vertex", "2.1"), VertexV21Adapter)

    class VertexV30Stub:
        supplier = "vertex"
        contract_version = "3.0"

        def adapt(self, envelope):  # pragma: no cover - selection is what matters
            raise NotImplementedError

    stub = VertexV30Stub()
    registry.register(stub)
    assert registry.resolve("VERTEX", "3.0") is stub
    assert isinstance(registry.resolve("vertex", "2.1"), VertexV21Adapter)
    with pytest.raises(UnknownContractError):
        registry.resolve("vertex", "9.9")
    with pytest.raises(UnknownContractError):
        registry.resolve("unknown-supplier", None)


def test_registry_rejects_duplicate_registration():
    registry = AdapterRegistry()
    registry.register(NorthstarV1Adapter())
    with pytest.raises(ValueError):
        registry.register(NorthstarV1Adapter())


def test_unknown_contract_version_rejects_batch_before_any_write():
    payload = [{
        "supplier": "Northstar", "contract_version": "9.9",
        "part_number": "AB-120 / 4", "brand": "ACME", "package_quantity": 1,
        "stock_status": "In Stock", "price": "18.50",
    }]
    tmp, pipeline, store, source_ref = run_payload(payload)
    with tmp:
        with pytest.raises(SourceBatchRejectedError) as err:
            pipeline.run("USD", source_ref)
        message = str(err.value)
        assert "Northstar" in message and "9.9" in message and "$[0]" in message
        assert rows(store, "SELECT * FROM parts") == []
        assert rows(store, "SELECT * FROM quotes") == []


def test_contract_violation_rejects_whole_batch_with_provenance():
    valid = {
        "sku": {"code": "vx-88.10-q", "maker": "Vertex Tools"},
        "pack": {"uom": "box", "qty": 10},
        "availability": {"state": "IN_STOCK", "lead_days": 7},
        "pricing": {"amount": "102.40"},
    }
    missing_part_number = {
        "sku": {"maker": "Vertex Tools"},
        "pack": {"uom": "box", "qty": 10},
        "availability": {"state": "IN_STOCK"},
    }
    payload = {"supplier": "Vertex", "contract_version": "2.1",
               "records": [valid, missing_part_number]}
    tmp, pipeline, store, source_ref = run_payload(payload)
    with tmp:
        with pytest.raises(SourceBatchRejectedError) as err:
            pipeline.run("USD", source_ref)
        assert len(err.value.rejections) == 1
        rejection = err.value.rejections[0]
        assert isinstance(rejection, RecordContractViolationError)
        assert rejection.supplier == "Vertex"
        assert rejection.contract_version == "2.1"
        assert rejection.locator == "$.records[1]"
        assert "sku.code" in str(rejection)
        # the valid first record must not have been written either
        assert rows(store, "SELECT * FROM parts") == []
        assert rows(store, "SELECT * FROM quotes") == []


def test_unified_contract_is_checked_after_adaptation():
    payload = [{"supplier": "Northstar", "part_number": "///", "brand": "ACME", "package_quantity": 1}]
    tmp, pipeline, store, source_ref = run_payload(payload)
    with tmp:
        with pytest.raises(SourceBatchRejectedError) as err:
            pipeline.run("USD", source_ref)
        assert "part_number" in str(err.value)
        assert rows(store, "SELECT * FROM parts") == []
