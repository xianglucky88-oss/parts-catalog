from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .admission import AdmissionDecision, admit_quote
from .models import NormalizedPart, Quote

_SCHEMA = """
CREATE TABLE IF NOT EXISTS parts (
    canonical_number TEXT PRIMARY KEY, brand TEXT NOT NULL, description TEXT NOT NULL,
    unit TEXT NOT NULL, package_quantity INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS quotes (
    canonical_number TEXT NOT NULL, supplier TEXT NOT NULL, currency TEXT NOT NULL,
    price TEXT, lead_time_days INTEGER, stock_status TEXT NOT NULL, source_ref TEXT NOT NULL,
    PRIMARY KEY (canonical_number, supplier, source_ref)
);
-- 准入留痕：原始值（raw_*）与被拒绝理由始终保留；合格记录同时保存规范化后的精确值。
CREATE TABLE IF NOT EXISTS quote_admission_ledger (
    canonical_number TEXT NOT NULL,
    supplier TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    admitted INTEGER NOT NULL,
    currency TEXT,
    price TEXT,
    raw_currency TEXT,
    raw_price TEXT,
    reason_code TEXT,
    reason TEXT,
    decided_at TEXT NOT NULL,
    PRIMARY KEY (canonical_number, supplier, source_ref)
);
-- 财务比较集合的唯一入口：该视图只可能包含通过准入的记录，
-- 未通过或尚未准入的报价在物理上无法出现在这里。
CREATE VIEW IF NOT EXISTS eligible_quotes AS
SELECT
    q.canonical_number,
    q.supplier,
    q.source_ref,
    l.currency,
    l.price,
    q.lead_time_days,
    q.stock_status
FROM quotes AS q
JOIN quote_admission_ledger AS l
  ON l.canonical_number = q.canonical_number
 AND l.supplier = q.supplier
 AND l.source_ref = q.source_ref
WHERE l.admitted = 1;
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PartsStore:
    def __init__(self, database: str | Path) -> None:
        self.connection = sqlite3.connect(database)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(_SCHEMA)
        self.connection.commit()

    def save_part(self, part: NormalizedPart) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO parts VALUES (?, ?, ?, ?, ?)",
            (part.canonical_number, part.brand, part.description, part.unit, part.package_quantity),
        )

    def save_quote(self, quote: Quote) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO quotes VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                quote.canonical_number,
                quote.supplier,
                quote.currency,
                str(quote.price) if quote.price is not None else None,
                quote.lead_time_days,
                quote.stock_status,
                quote.source_ref,
            ),
        )

    def _write_ledger(self, decision: AdmissionDecision, key: tuple[str, str, str], decided_at: str) -> None:
        canonical_number, supplier, source_ref = key
        self.connection.execute(
            """INSERT OR REPLACE INTO quote_admission_ledger
               (canonical_number, supplier, source_ref, admitted, currency, price,
                raw_currency, raw_price, reason_code, reason, decided_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                canonical_number,
                supplier,
                source_ref,
                1 if decision.admitted else 0,
                decision.currency,
                str(decision.amount) if decision.amount is not None else None,
                None if decision.raw_currency is None else str(decision.raw_currency),
                None if decision.raw_amount is None else str(decision.raw_amount),
                decision.reason_code,
                decision.reason,
                decided_at,
            ),
        )

    def admit_and_store(
        self,
        quote: Quote,
        decided_at: str | None = None,
    ) -> AdmissionDecision:
        """对一条新报价执行准入并原子写入：报价行与准入留痕同事务落库。

        原始报价原样保留在 quotes 与留痕表的 raw_* 列中；只有 admitted=1 的
        记录才会出现在 eligible_quotes 视图里。
        """
        decision = admit_quote(quote.price, quote.currency)
        key = (quote.canonical_number, quote.supplier, quote.source_ref)
        timestamp = decided_at or _utc_now()
        with self.connection:
            self.save_quote(quote)
            self._write_ledger(decision, key, timestamp)
        return decision

    def admit_existing_quotes(self, decided_at: str | None = None) -> dict:
        """为迁移前已存在于 quotes 表的记录补齐准入留痕。

        以 quotes 表中的持久化文本作为原始值重新过闸，因此小写/空白货币、
        NaN、Infinity 与历史价格文本都会被如实识别。重复执行是安全的：
        已有留痕的记录会被跳过。
        """
        timestamp = decided_at or _utc_now()
        rows = self.connection.execute(
            """SELECT q.* FROM quotes AS q
               LEFT JOIN quote_admission_ledger AS l
                 ON l.canonical_number = q.canonical_number
                AND l.supplier = q.supplier
                AND l.source_ref = q.source_ref
               WHERE l.canonical_number IS NULL"""
        ).fetchall()
        screened = admitted = rejected = 0
        with self.connection:
            for row in rows:
                screened += 1
                decision = admit_quote(row["price"], row["currency"])
                if decision.admitted:
                    admitted += 1
                else:
                    rejected += 1
                self._write_ledger(
                    decision,
                    (row["canonical_number"], row["supplier"], row["source_ref"]),
                    timestamp,
                )
        return {"screened": screened, "admitted": admitted, "rejected": rejected}

    def eligible_quote_rows(self) -> list[dict]:
        """财务比较集合：仅含通过准入的报价。"""
        return [dict(row) for row in self.connection.execute("SELECT * FROM eligible_quotes").fetchall()]

    def admission_rows(self) -> list[dict]:
        """全量准入留痕，含合格与被拒记录、原始值及拒绝理由，供审计追查。"""
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM quote_admission_ledger ORDER BY canonical_number, supplier, source_ref"
            ).fetchall()
        ]

    def rejected_quote_rows(self) -> list[dict]:
        """被拒记录清单：原始值与理由仍可追查，但它们不在比较集合中。"""
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM quote_admission_ledger WHERE admitted = 0"
            ).fetchall()
        ]

    def commit(self) -> None:
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
