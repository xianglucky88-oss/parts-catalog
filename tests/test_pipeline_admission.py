import json

from parts_catalog.pipeline import PartsPipeline
from parts_catalog.sources import JsonFixtureSource
from parts_catalog.storage import PartsStore

FIXTURE = [
    {"supplier": "Northstar", "part_number": "AB-1204", "brand": "ACME", "price": "18.50", "currency": "USD"},
    {"supplier": "AcmeDirect", "part_number": "AB-1204", "brand": "ACME", "price": " 19.99 ", "currency": "usd"},
    {"supplier": "PrecisionParts", "part_number": "CD-9001", "brand": "NORTHSTAR", "price": "120.00", "currency": " eur "},
    {"supplier": "OverseasCo", "part_number": "IJ-4005", "brand": "NORTHSTAR", "price": "55.25", "currency": "CHF"},
    {"supplier": "LegacyBroker", "part_number": "GH-3004", "brand": "ACME", "price": "call for price", "currency": "USD"},
    {"supplier": "DataIssueCo", "part_number": "EF-2002", "brand": "ACME", "price": "NaN", "currency": "USD"},
    {"supplier": "DataIssueCo", "part_number": "EF-2003", "brand": "ACME", "price": "Infinity", "currency": "USD"},
    {"supplier": "EmptyQuote", "part_number": "KL-5006", "brand": "ACME", "price": None, "currency": "GBP"},
]


def test_pipeline_does_not_abort_and_segregates_quotes(tmp_path):
    fixture_path = tmp_path / "payload.json"
    fixture_path.write_text(json.dumps(FIXTURE), encoding="utf-8")
    store = PartsStore(tmp_path / "pipeline.sqlite3")

    summary = PartsPipeline(JsonFixtureSource(fixture_path), store).run(
        currency="USD", source_ref=str(fixture_path)
    )

    # 脏数据不会中断采集：8 条全部处理，3 条合格、5 条被拒
    assert summary["processed"] == 8
    assert summary["admitted"] == 3
    assert summary["rejected"] == 5
    assert [decision.admitted for decision in summary["decisions"]] == [
        True,
        True,
        True,
        False,
        False,
        False,
        False,
        False,
    ]

    eligible = store.eligible_quote_rows()
    assert {row["supplier"] for row in eligible} == {"Northstar", "AcmeDirect", "PrecisionParts"}
    # 小写与带空白货币在比较集合中均为规范化受控代码
    assert {row["currency"] for row in eligible} == {"USD", "EUR"}
    assert {row["price"] for row in eligible} == {"18.50", "19.99", "120.00"}

    # 被拒记录保留原始值与理由，可审计但不在比较集合
    rejected = store.rejected_quote_rows()
    assert {row["reason_code"] for row in rejected} == {
        "currency_unknown",
        "price_not_numeric",
        "price_non_finite",
        "price_missing",
    }
    assert len(eligible) + len(rejected) == 8
    store.close()


def test_pipeline_uses_global_currency_when_row_has_none(tmp_path):
    payload = [{"supplier": "S", "part_number": "X-1", "brand": "B", "price": "1.00"}]
    fixture_path = tmp_path / "payload.json"
    fixture_path.write_text(json.dumps(payload), encoding="utf-8")
    store = PartsStore(tmp_path / "fallback.sqlite3")

    PartsPipeline(JsonFixtureSource(fixture_path), store).run(currency="GBP", source_ref="ref")

    assert store.eligible_quote_rows()[0]["currency"] == "GBP"
    store.close()
