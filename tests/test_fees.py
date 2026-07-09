"""Fee computation: exact values, order-level rounding, both Poly regimes,
and hypothesis property tests over the full price/size space.

Wrong fees are the most dangerous bug class in this bot: overstatement
silently rejects profitable arbs, understatement executes losing trades.
"""
from decimal import Decimal as D

from hypothesis import given, strategies as st

import arb_bot as ab

PRICES = st.integers(min_value=1, max_value=99).map(lambda c: D(c) / 100)
CONTRACTS = st.integers(min_value=1, max_value=500)


# ── Exact values ─────────────────────────────────────────────────────

def test_kalshi_fee_single_contract():
    # 0.07 * 0.40 * 0.60 = 0.0168 -> ceil to 0.02
    assert ab.kalshi_taker_fee(D("0.40"), 1) == D("0.02")


def test_kalshi_fee_exact_penny_not_rounded_up():
    # 0.07 * 4 * 0.25 = 0.07 exactly — ceiling must not add a penny
    assert ab.kalshi_taker_fee(D("0.5"), 4) == D("0.07")


def test_kalshi_fee_order_level_rounding():
    # Fees round once per ORDER: ceil(0.07*50*0.05*0.95 = 0.16625) = 0.17,
    # not ceil-per-contract (0.01) * 50 = 0.50.
    assert ab.kalshi_taker_fee(D("0.05"), 50) == D("0.17")
    assert ab.kalshi_taker_fee(D("0.05"), 1) * 50 == D("0.50")


def test_poly_fee_sports_quadratic():
    # 0.25 * 10 * (0.25)^2 = 0.15625 -> Q4 ceiling = 0.1563
    assert ab.poly_taker_fee(D("0.5"), 10) == D("0.1563")


def test_poly_fee_crypto_linear(monkeypatch):
    monkeypatch.setattr(ab, "POLY_FEE_RATE", D("0.0175"))
    monkeypatch.setattr(ab, "POLY_FEE_EXPONENT", 1)
    # 0.0175 * 0.25 = 0.004375 -> Q4 ceiling = 0.0044
    assert ab.poly_taker_fee(D("0.5"), 1) == D("0.0044")


def test_zero_rate_and_zero_contracts():
    assert ab.poly_taker_fee(D("0.5"), 0) == ab.ZERO
    assert ab.kalshi_taker_fee(D("0.5"), 0) == ab.ZERO


def test_poly_zero_rate(monkeypatch):
    monkeypatch.setattr(ab, "POLY_FEE_RATE", ab.ZERO)
    assert ab.poly_taker_fee(D("0.5"), 10) == ab.ZERO


# ── Property tests ───────────────────────────────────────────────────

@given(price=PRICES, n=CONTRACTS)
def test_kalshi_fee_invariants(price, n):
    fee = ab.kalshi_taker_fee(price, n)
    assert fee >= ab.ZERO
    # Order-level rounding never exceeds per-contract-ceil * N
    assert fee <= ab.kalshi_taker_fee(price, 1) * n
    # Monotone in contract count
    assert ab.kalshi_taker_fee(price, n + 1) >= fee
    # Symmetric around p = 0.5
    assert fee == ab.kalshi_taker_fee(ab.ONE - price, n)
    # Quantized to the penny
    assert fee == fee.quantize(ab.Q2)


@given(price=PRICES, n=CONTRACTS)
def test_poly_fee_invariants(price, n):
    fee = ab.poly_taker_fee(price, n)
    assert fee >= ab.ZERO
    assert fee <= ab.poly_taker_fee(price, 1) * n
    assert ab.poly_taker_fee(price, n + 1) >= fee
    assert fee == ab.poly_taker_fee(ab.ONE - price, n)
    # Quantized to 4 decimal places (smallest Poly fee unit)
    assert fee == fee.quantize(ab.Q4)


@given(price=PRICES, n=CONTRACTS)
def test_fee_never_exceeds_notional(price, n):
    """A taker fee can never exceed the order's notional value."""
    notional = price * D(n)
    assert ab.kalshi_taker_fee(price, n) <= notional + D("0.01")
    assert ab.poly_taker_fee(price, n) <= notional + D("0.0001")
