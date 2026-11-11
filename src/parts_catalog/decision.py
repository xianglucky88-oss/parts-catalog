"""Deterministic quotation decision projection.

This module never writes to the catalog database. It opens the SQLite file
read-only and rebuilds, for every canonical part, the same procurement
decision an offline data-engineering warehouse would recompute:

* records that *cannot participate* (no usable price, unavailable stock,
  unparseable price, currency that cannot be compared) are reported as
  ineligible with an explicit reason code;
* records that *can* participate are ranked with a total, order-independent
  sort key and every non-winning candidate is reported as a true loser with
  machine-readable rationale: the exact decimal price delta for price losers,
  or the single decisive tie-break dimension for equal-price losers.

Determinism guarantees:

* all ordering is done explicitly in Python with a total-order key, so the
  physical row order / read order of SQLite cannot change the result;
* prices are compared as :class:`decimal.Decimal` (no binary floats) and
  serialized as their exact text form;
* every emitted collection has a documented, fixed ordering.
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"

#: Stock statuses that may take part in a decision.
UNAVAILABLE_STATUS = "unavailable"

#: Reason codes for records that cannot participate in a decision.
INELIGIBLE_REASONS = (
    "price_missing",
    "invalid_price",
    "unavailable_stock",
    "currency_mismatch",
)

#: Tie-break dimensions, in the order they are applied after price.
TIE_BREAK_DIMENSIONS = ("lead_time_days", "supplier", "source_ref")


def _connect_readonly(database: str | Path) -> sqlite3.Connection:
    """Open *database* with the SQLite ``mode=ro`` URI flag."""
    uri = Path(database).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _load_quote_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    columns = (
        "canonical_number",
        "supplier",
        "currency",
        "price",
        "lead_time_days",
        "stock_status",
        "source_ref",
    )
    cursor = connection.execute(
        """
        SELECT canonical_number, supplier, currency, price, lead_time_days,
               stock_status, source_ref
        FROM quotes
        """
    )
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _load_part_numbers(connection: sqlite3.Connection) -> set[str]:
    return {row[0] for row in connection.execute("SELECT canonical_number FROM parts")}


def _parsed_price(row: dict[str, Any]) -> Decimal | None:
    """Return the decimal price, or ``None`` when missing/unparseable."""
    raw = row["price"]
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        return None


def _ineligible_reason(row: dict[str, Any], currency: str) -> str | None:
    """Classify a record that cannot participate.

    Precedence is fixed so that a record failing several rules gets one
    stable reason code: missing price, unparseable price, unavailable
    stock, then non-matching currency.
    """
    if row["price"] is None:
        return "price_missing"
    if _parsed_price(row) is None:
        return "invalid_price"
    if row["stock_status"] == UNAVAILABLE_STATUS:
        return "unavailable_stock"
    if row["currency"] != currency:
        return "currency_mismatch"
    return None


def _rank_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """Total-order ranking key for eligible candidates.

    Ordering: lowest exact price first, then shortest lead time (unknown
    lead time sorts last), then supplier name, then source reference.
    The key is total, so equal-price quotes always get a stable winner
    regardless of database read order.
    """
    price = _parsed_price(row)
    lead = row["lead_time_days"]
    lead_key = (1, 0) if lead is None else (0, lead)
    return (price, lead_key, row["supplier"], row["source_ref"])


def _quote_view(row: dict[str, Any]) -> dict[str, Any]:
    price = _parsed_price(row)
    return {
        "supplier": row["supplier"],
        "source_ref": row["source_ref"],
        "currency": row["currency"],
        "price": str(price) if price is not None else None,
        "lead_time_days": row["lead_time_days"],
        "stock_status": row["stock_status"],
    }


def _tie_break_dimensions(candidate: dict[str, Any], winner: dict[str, Any]) -> list[str]:
    """Return the single decisive tie-break dimension.

    Dimensions are checked in their application order; the first one on
    which *winner* beats the equal-price *candidate* decides the tie.
    The remaining candidate/writer rows stay available in the output for
    any further comparison. A decisive dimension always exists because
    the ranking key is total.
    """
    if candidate["lead_time_days"] != winner["lead_time_days"]:
        return ["lead_time_days"]
    if candidate["supplier"] != winner["supplier"]:
        return ["supplier"]
    return ["source_ref"]


def _considered_entry(rank: int, row: dict[str, Any], winner: dict[str, Any] | None) -> dict[str, Any]:
    entry: dict[str, Any] = {"rank": rank, **_quote_view(row)}
    if winner is None or rank == 1:
        entry["outcome"] = "selected"
        entry["reason_code"] = None
        return entry
    entry["outcome"] = "lost"
    candidate_price = _parsed_price(row)
    winner_price = _parsed_price(winner)
    if candidate_price > winner_price:
        entry["reason_code"] = "higher_price"
        entry["price_delta"] = str(candidate_price - winner_price)
    else:
        entry["reason_code"] = "tie_broken"
        entry["tie_broken_by"] = _tie_break_dimensions(row, winner)
    return entry


def _build_part_decision(canonical_number: str, rows: list[dict[str, Any]], currency: str) -> dict[str, Any]:
    eligible: list[dict[str, Any]] = []
    ineligible: list[dict[str, Any]] = []
    for row in rows:
        reason = _ineligible_reason(row, currency)
        if reason is None:
            eligible.append(row)
        else:
            view = _quote_view(row)
            view["reason_code"] = reason
            ineligible.append(view)

    # Fixed ordering: ranking key is total; ineligible records order by
    # supplier then source reference so output never depends on read order.
    eligible.sort(key=_rank_key)
    ineligible.sort(key=lambda item: (item["supplier"], item["source_ref"], item["currency"]))

    winner = eligible[0] if eligible else None
    considered = [
        _considered_entry(rank, row, winner)
        for rank, row in enumerate(eligible, start=1)
    ]
    recommendation = _quote_view(winner) if winner is not None else None

    return {
        "canonical_number": canonical_number,
        "status": "recommended" if winner is not None else "no_eligible_candidate",
        "candidate_count": len(rows),
        "eligible_count": len(eligible),
        "ineligible_count": len(ineligible),
        "recommendation": recommendation,
        "ineligible": ineligible,
        "considered": considered,
    }


def build_decision_projection(database: str | Path, currency: str = "USD") -> dict[str, Any]:
    """Rebuild the deterministic quote decision projection from *database*.

    The database is opened read-only and never modified. ``currency``
    selects the decision currency; quotes in another currency cannot be
    compared without exchange rates and are reported as ineligible.
    """
    currency = currency.upper()
    connection = _connect_readonly(database)
    try:
        part_numbers = _load_part_numbers(connection)
        quote_rows = _load_quote_rows(connection)
    finally:
        connection.close()

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in quote_rows:
        grouped.setdefault(row["canonical_number"], []).append(row)

    # Parts with no quotes at all are still projected (as having no
    # eligible candidate), and quote-only numbers are included too.
    canonical_numbers = sorted(part_numbers | set(grouped))
    decisions = [
        _build_part_decision(canonical, grouped.get(canonical, []), currency)
        for canonical in canonical_numbers
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "decision_currency": currency,
        "decisions": decisions,
    }


def project_to_file(database: str | Path, output: str | Path, currency: str = "USD") -> dict[str, Any]:
    """Write the projection as deterministic UTF-8 JSON and return it."""
    projection = build_decision_projection(database, currency)
    payload = json.dumps(projection, ensure_ascii=False, indent=2) + "\n"
    Path(output).write_text(payload, encoding="utf-8")
    return projection
