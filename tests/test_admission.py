from decimal import Decimal

import pytest

from parts_catalog.admission import (
    ALLOWED_CURRENCIES,
    REASON_CURRENCY_FORMAT,
    REASON_CURRENCY_MISSING,
    REASON_CURRENCY_UNKNOWN,
    REASON_PRICE_BAD_TYPE,
    REASON_PRICE_MISSING,
    REASON_PRICE_NEGATIVE,
    REASON_PRICE_NON_FINITE,
    REASON_PRICE_NOT_NUMERIC,
    admit_quote,
    normalize_currency,
)


# ---------- 货币：格式变体被规范化，受控代码才准入 ----------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("usd", "USD"),
        (" eur ", "EUR"),
        ("GBP", "GBP"),
        ("cny", "CNY"),
    ],
)
def test_currency_format_variants_are_canonicalized(raw, expected):
    code, reason_code, reason = normalize_currency(raw)
    assert code == expected
    assert reason_code is None
    assert reason is None


def test_internal_whitespace_in_currency_is_a_format_error():
    # 内部空白不能被静默折叠——那会改变代码本身的字符构成。
    code, reason_code, _ = normalize_currency("  j p y  ")
    assert code is None
    assert reason_code == REASON_CURRENCY_FORMAT


def test_unknown_currency_code_is_rejected_even_when_well_formed():
    code, reason_code, reason = normalize_currency("CHF")
    assert code is None
    assert reason_code == REASON_CURRENCY_UNKNOWN
    assert "CHF" in reason


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_missing_currency_is_rejected(raw):
    _, reason_code, _ = normalize_currency(raw)
    assert reason_code == REASON_CURRENCY_MISSING


@pytest.mark.parametrize("raw", ["US", "USDD", "US1", 123])
def test_malformed_currency_is_rejected(raw):
    _, reason_code, reason = normalize_currency(raw)
    assert reason_code == REASON_CURRENCY_FORMAT
    assert reason


# ---------- 金额：精确、有限、非负 ----------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("18.50", Decimal("18.50")),
        ("  18.50  ", Decimal("18.50")),
        (Decimal("18.75"), Decimal("18.75")),
        (100, Decimal("100")),
        ("0", Decimal("0")),
    ],
)
def test_exact_finite_amounts_are_admitted(raw, expected):
    decision = admit_quote(raw, "USD")
    assert decision.admitted
    assert decision.amount == expected
    assert decision.currency == "USD"


def test_grouping_separators_are_not_silently_rewritten():
    # 千分位逗号可能掩盖区域性格式差异，入口不做静默改写，按不可解析拒绝。
    decision = admit_quote("1,234.50", "USD")
    assert not decision.admitted
    assert decision.reason_code == REASON_PRICE_NOT_NUMERIC


@pytest.mark.parametrize(
    "raw,reason_code",
    [
        (Decimal("NaN"), REASON_PRICE_NON_FINITE),
        (Decimal("Infinity"), REASON_PRICE_NON_FINITE),
        (Decimal("-Infinity"), REASON_PRICE_NON_FINITE),
        ("NaN", REASON_PRICE_NON_FINITE),          # SQLite 迁移文本
        ("Infinity", REASON_PRICE_NON_FINITE),     # SQLite 迁移文本
    ],
)
def test_non_finite_amounts_are_rejected(raw, reason_code):
    decision = admit_quote(raw, "USD")
    assert not decision.admitted
    assert decision.reason_code == reason_code
    assert decision.amount is None
    # NaN 不等于自身，无法用 == 比较；按类型与原始表示核对留痕。
    assert type(decision.raw_amount) is type(raw)
    assert str(decision.raw_amount) == str(raw)


def test_float_is_rejected_to_protect_exactness():
    decision = admit_quote(18.5, "USD")
    assert not decision.admitted
    assert decision.reason_code == REASON_PRICE_BAD_TYPE


def test_bool_is_rejected_as_unsupported_type():
    decision = admit_quote(True, "USD")
    assert not decision.admitted
    assert decision.reason_code == REASON_PRICE_BAD_TYPE


@pytest.mark.parametrize("raw", ["call for price", "N/A", "", "   ", None, "面议"])
def test_legacy_price_text_and_missing_prices_are_rejected(raw):
    decision = admit_quote(raw, "USD")
    assert not decision.admitted
    assert decision.reason_code == (
        REASON_PRICE_MISSING if raw is None or (isinstance(raw, str) and (not raw.strip() or raw.strip().upper() == "N/A"))
        else REASON_PRICE_NOT_NUMERIC
    )


def test_negative_amount_is_rejected():
    decision = admit_quote("-0.01", "USD")
    assert not decision.admitted
    assert decision.reason_code == REASON_PRICE_NEGATIVE


def test_unknown_currency_short_circuits_amount_parsing():
    decision = admit_quote(Decimal("NaN"), "ZZZ")
    assert not decision.admitted
    assert decision.reason_code == REASON_CURRENCY_UNKNOWN


def test_decision_keeps_raw_values_for_audit():
    decision = admit_quote(" call for price ", " usd ")
    assert not decision.admitted
    assert decision.reason_code == REASON_PRICE_NOT_NUMERIC
    assert decision.raw_amount == " call for price "
    assert decision.raw_currency == " usd "
    assert decision.amount is None
    assert decision.currency is None


def test_allowed_currency_list_is_controlled():
    assert ALLOWED_CURRENCIES == frozenset({"USD", "EUR", "GBP", "JPY", "CNY"})
