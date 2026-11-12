"""报价进入财务比较集合之前的准入边界。

规则只有两类，且都在数据“可比”之前生效：

1. 金额必须是**精确**且**有限**的十进制数——拒绝 float（二进制浮点不精确）、
   NaN / Infinity（非有限）以及早年迁移残留的价格文本（不可解析）。
2. 货币代码必须是受控白名单内的 ISO 4217 三字母代码；大小写与首尾空白属于
   可修复的格式变体，先规范化再判定，未知代码一律拒绝。

本模块明确**不做**汇率换算，也**不做**包装单位换算，只产出准入决定，
并把原始值与拒绝理由原样保留在决定中，供持久化层审计。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

#: 受控货币代码白名单（ISO 4217 三字母代码）。
ALLOWED_CURRENCIES = frozenset({"USD", "EUR", "GBP", "JPY", "CNY"})

#: 稳定的拒绝理由代码，可直接用于数据库查询与统计。
REASON_CURRENCY_MISSING = "currency_missing"
REASON_CURRENCY_FORMAT = "currency_format"
REASON_CURRENCY_UNKNOWN = "currency_unknown"
REASON_PRICE_MISSING = "price_missing"
REASON_PRICE_BAD_TYPE = "price_bad_type"
REASON_PRICE_NOT_NUMERIC = "price_not_numeric"
REASON_PRICE_NON_FINITE = "price_non_finite"
REASON_PRICE_NEGATIVE = "price_negative"

_CURRENCY_RE = re.compile(r"[A-Z]{3}")
_EMPTY_TOKENS = frozenset({"", "n/a", "na", "null", "none"})


@dataclass(frozen=True)
class AdmissionDecision:
    """一次准入判定的完整结果。

    admitted 为 False 时 amount/currency 为 None，但 raw_amount/raw_currency
    始终保留传入时的原始值，reason_code/reason 说明被拒绝的原因。
    """

    admitted: bool
    raw_amount: Any
    raw_currency: Any
    amount: Decimal | None
    currency: str | None
    reason_code: str | None
    reason: str | None


def normalize_currency(raw: Any) -> tuple[str | None, str | None, str | None]:
    """规范化并校验货币代码。

    先去除首尾空白并转大写（修复 " usd "、"eur" 之类格式变体），
    再校验三字母格式与受控白名单；内部空白不做静默删除，按格式错误拒绝。

    返回 ``(受控代码, 理由代码, 理由说明)``；准入成功时后两项为 None。
    """
    if raw is None:
        return None, REASON_CURRENCY_MISSING, "currency is missing"
    if not isinstance(raw, str):
        return (
            None,
            REASON_CURRENCY_FORMAT,
            f"currency {raw!r} must be a 3-letter ISO code, got {type(raw).__name__}",
        )
    if not raw.strip():
        return None, REASON_CURRENCY_MISSING, "currency is missing"
    # 仅折叠首尾空白；内部空白属于格式错误，不能静默删除字母间的字符。
    code = raw.strip().upper()
    if not _CURRENCY_RE.fullmatch(code):
        return None, REASON_CURRENCY_FORMAT, f"currency {raw!r} is not a 3-letter ISO code"
    if code not in ALLOWED_CURRENCIES:
        return (
            None,
            REASON_CURRENCY_UNKNOWN,
            f"currency {code} is not in the controlled list {sorted(ALLOWED_CURRENCIES)}",
        )
    return code, None, None


def _coerce_amount(raw: Any) -> tuple[Decimal | None, str | None, str | None]:
    """把原始金额收敛为精确有限的 Decimal，否则给出拒绝理由。"""
    if raw is None:
        return None, REASON_PRICE_MISSING, "price is missing"
    # bool 是 int 的子类，报价金额不接受布尔值。
    if isinstance(raw, bool):
        return (
            None,
            REASON_PRICE_BAD_TYPE,
            f"price {raw!r} has unsupported type {type(raw).__name__}",
        )
    if isinstance(raw, float):
        # 二进制浮点无法精确表达十进制报价，入口处直接拒绝。
        return (
            None,
            REASON_PRICE_BAD_TYPE,
            f"float price {raw!r} is rejected; use Decimal, int or an exact decimal string",
        )
    if isinstance(raw, int):
        value = Decimal(raw)
    elif isinstance(raw, Decimal):
        value = raw
    elif isinstance(raw, str):
        text = " ".join(raw.split())
        if text.lower() in _EMPTY_TOKENS:
            return None, REASON_PRICE_MISSING, f"price {raw!r} is missing"
        try:
            value = Decimal(text)
        except InvalidOperation:
            return None, REASON_PRICE_NOT_NUMERIC, f"price {raw!r} is not a numeric value"
    else:
        return (
            None,
            REASON_PRICE_BAD_TYPE,
            f"price {raw!r} has unsupported type {type(raw).__name__}",
        )
    if not value.is_finite():
        # NaN 与（带符号的）Infinity 都不是有限数，不能参与比较。
        return None, REASON_PRICE_NON_FINITE, f"price {raw!r} is not a finite number"
    if value < 0:
        return None, REASON_PRICE_NEGATIVE, f"price {raw!r} must not be negative"
    return value, None, None


def admit_quote(raw_amount: Any, raw_currency: Any) -> AdmissionDecision:
    """对一条报价的金额与货币执行准入判定。

    货币先于金额判定：货币不受控的报价没有任何比较资格，无需再解析金额。
    原始值与拒绝理由随决定一起返回，持久化层据此留痕。
    """
    currency, currency_reason_code, currency_reason = normalize_currency(raw_currency)
    if currency_reason_code is not None:
        return AdmissionDecision(
            admitted=False,
            raw_amount=raw_amount,
            raw_currency=raw_currency,
            amount=None,
            currency=None,
            reason_code=currency_reason_code,
            reason=currency_reason,
        )

    amount, price_reason_code, price_reason = _coerce_amount(raw_amount)
    if price_reason_code is not None:
        return AdmissionDecision(
            admitted=False,
            raw_amount=raw_amount,
            raw_currency=raw_currency,
            amount=None,
            currency=None,
            reason_code=price_reason_code,
            reason=price_reason,
        )

    return AdmissionDecision(
        admitted=True,
        raw_amount=raw_amount,
        raw_currency=raw_currency,
        amount=amount,
        currency=currency,
        reason_code=None,
        reason=None,
    )
