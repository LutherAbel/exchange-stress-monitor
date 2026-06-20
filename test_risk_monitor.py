"""Regression tests for the depth-aware impact price helper.

Run with: python -m pytest test_risk_monitor.py    (or: python test_risk_monitor.py)
"""

from risk_monitor import impact_sell_price


def test_two_element_levels():
    # [price, amount] -- the classic ccxt shape.
    bids = [[100.0, 1.0], [99.0, 2.0]]
    assert impact_sell_price(bids, 50.0) == 100.0


def test_three_element_levels():
    # [price, amount, timestamp] -- Kraken et al. Must not raise; the third
    # field is ignored. Same numbers as above -> same answer.
    bids = [[100.0, 1.0, 1_700_000_000], [99.0, 2.0, 1_700_000_001]]
    assert impact_sell_price(bids, 50.0) == 100.0


def test_thin_book_returns_none():
    # Book can't absorb the order -> honest None, not a misleading number.
    bids = [[100.0, 0.1, 1_700_000_000]]
    assert impact_sell_price(bids, 1_000_000.0) is None


if __name__ == "__main__":
    import traceback
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:
                failed += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    raise SystemExit(1 if failed else 0)
