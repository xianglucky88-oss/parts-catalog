from decimal import Decimal

from parts_catalog.models import Quote
from parts_catalog.storage import PartsStore

_LEGACY_ROWS = [
    # 正常历史记录：规范大写货币 + 精确金额
    ("AB1204", "Northstar", "fixture", "USD", "18.50", 4, "available"),
    # 货币格式变体：小写 / 带空白，金额本身有效 -> 规范化后可准入
    ("AB1204", "AcmeDirect", "legacy-2019", " usd ", "19.99", None, "available"),
    ("CD9001", "Northstar", "legacy-2020", "eur", "120.00", 7, "backorder"),
    # 异常数值：SQLite 中以 TEXT 残留的 NaN / Infinity
    ("EF2002", "Northstar", "legacy-2018", "USD", "NaN", None, "unknown"),
    ("EF2003", "Northstar", "legacy-2018", "USD", "Infinity", None, "unknown"),
    # 早年迁移残留的价格文本
    ("GH3004", "OldBroker", "legacy-2017", "USD", "call for price", None, "unknown"),
    # 未知货币代码：格式合法但不在受控白名单
    ("IJ4005", "OverseasCo", "legacy-2016", "CHF", "55.25", 14, "available"),
]


def _seed_legacy_db(store: PartsStore) -> None:
    # 列顺序与 quotes 表一致：number, supplier, currency, price, lead, status, source_ref
    for number, supplier, ref, currency, price, lead, status in _LEGACY_ROWS:
        store.connection.execute(
            "INSERT OR REPLACE INTO quotes VALUES (?, ?, ?, ?, ?, ?, ?)",
            (number, supplier, currency, price, lead, status, ref),
        )
    store.commit()


def test_legacy_migration_admits_only_qualified_records(tmp_path):
    store = PartsStore(tmp_path / "legacy.sqlite3")
    _seed_legacy_db(store)

    summary = store.admit_existing_quotes(decided_at="2026-11-11T00:00:00+00:00")

    assert summary == {"screened": 7, "admitted": 3, "rejected": 4}

    eligible = store.eligible_quote_rows()
    assert {(row["canonical_number"], row["supplier"]) for row in eligible} == {
        ("AB1204", "Northstar"),
        ("AB1204", "AcmeDirect"),
        ("CD9001", "Northstar"),
    }
    # 比较集合中的货币与金额均为规范化后的精确值
    by_supplier = {row["supplier"]: row for row in eligible}
    assert by_supplier["AcmeDirect"]["currency"] == "USD"
    assert by_supplier["AcmeDirect"]["price"] == "19.99"
    assert by_supplier["Northstar"]["currency"] == "EUR"
    assert by_supplier["Northstar"]["price"] == "120.00"

    rejected = {
        (row["canonical_number"], row["supplier"], row["source_ref"]): row
        for row in store.rejected_quote_rows()
    }
    nan_row = next(row for row in rejected.values() if row["raw_price"] == "NaN")
    assert nan_row["reason_code"] == "price_non_finite"
    inf_row = next(row for row in rejected.values() if row["raw_price"] == "Infinity")
    assert inf_row["reason_code"] == "price_non_finite"
    text_row = next(row for row in rejected.values() if row["raw_price"] == "call for price")
    assert text_row["reason_code"] == "price_not_numeric"
    unknown_row = next(row for row in rejected.values() if row["raw_currency"] == "CHF")
    assert unknown_row["reason_code"] == "currency_unknown"

    # 原始值逐字保留，供后续追查
    assert nan_row["raw_currency"] == "USD"
    assert text_row["raw_currency"] == "USD"
    store.close()


def test_unqualified_records_never_appear_in_comparison_set(tmp_path):
    store = PartsStore(tmp_path / "gate.sqlite3")
    _seed_legacy_db(store)
    store.admit_existing_quotes()

    eligible_keys = {
        (row["canonical_number"], row["supplier"], row["source_ref"])
        for row in store.eligible_quote_rows()
    }
    ledger_keys = {
        (row["canonical_number"], row["supplier"], row["source_ref"])
        for row in store.admission_rows()
    }
    # 留痕覆盖全部记录，但比较视图是其严格子集
    assert len(ledger_keys) == 7
    assert eligible_keys < ledger_keys
    # 比较视图只暴露受控货币与精确金额文本，不存在拒绝理由列
    from parts_catalog.admission import ALLOWED_CURRENCIES

    assert all(row["currency"] in ALLOWED_CURRENCIES for row in store.eligible_quote_rows())
    assert all(Decimal(row["price"]).is_finite() for row in store.eligible_quote_rows())

    # 原始 quotes 表也未被删除或篡改
    raw_currencies = {
        row[0] for row in store.connection.execute("SELECT currency FROM quotes").fetchall()
    }
    assert {" usd ", "eur", "CHF"} <= raw_currencies
    store.close()


def test_migration_is_idempotent(tmp_path):
    store = PartsStore(tmp_path / "idempotent.sqlite3")
    _seed_legacy_db(store)

    first = store.admit_existing_quotes()
    second = store.admit_existing_quotes()

    assert first == {"screened": 7, "admitted": 3, "rejected": 4}
    assert second == {"screened": 0, "admitted": 0, "rejected": 0}
    assert len(store.admission_rows()) == 7
    store.close()


def test_new_quote_write_routes_through_gate(tmp_path):
    store = PartsStore(tmp_path / "writes.sqlite3")

    good = Quote("AB1204", "Northstar", " usd ", Decimal("18.50"), 4, "available", "fixture")
    bad_currency = Quote("AB1204", "OverseasCo", "CHF", Decimal("55.25"), 14, "available", "fixture")
    bad_price = Quote("EF2002", "Northstar", "USD", Decimal("NaN"), None, "unknown", "fixture")

    assert store.admit_and_store(good).admitted
    assert store.admit_and_store(bad_currency).reason_code == "currency_unknown"
    assert store.admit_and_store(bad_price).reason_code == "price_non_finite"

    eligible = store.eligible_quote_rows()
    assert len(eligible) == 1
    assert eligible[0]["currency"] == "USD"
    assert eligible[0]["price"] == "18.50"

    rejected = store.rejected_quote_rows()
    assert {row["reason_code"] for row in rejected} == {"currency_unknown", "price_non_finite"}
    # 被拒报价行同样留在 quotes 表，但视图不会暴露
    assert store.connection.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 3
    store.close()


def test_reprocessing_a_quote_updates_its_ledger_row(tmp_path):
    store = PartsStore(tmp_path / "rewrite.sqlite3")

    rejected = store.admit_and_store(
        Quote("AB1204", "Northstar", "USD", Decimal("NaN"), None, "unknown", "fixture")
    )
    assert not rejected.admitted
    assert len(store.eligible_quote_rows()) == 0

    admitted = store.admit_and_store(
        Quote("AB1204", "Northstar", "USD", Decimal("18.50"), 4, "available", "fixture")
    )
    assert admitted.admitted
    assert len(store.eligible_quote_rows()) == 1
    # 同一主键只保留一条最新留痕
    rows = store.admission_rows()
    assert len(rows) == 1
    assert rows[0]["admitted"] == 1
    store.close()
