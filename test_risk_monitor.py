"""Tests for the decision logic -- the part that decides whether to wake you up.

The failure modes listed in README.md are claims; these are the checks that make
them verifiable. Two tests are named after bugs that existed in v1 and would
silently come back if the fix were undone.

No network, no filesystem: `__init__` builds ccxt clients and opens the CSV, so
`make_monitor` bypasses it and sets only the state each unit actually reads.

Run with: python -m pytest test_risk_monitor.py    (or: python test_risk_monitor.py)
"""

from __future__ import annotations

import asyncio
import logging
import time

from risk_monitor import Config, DislocationMonitor, Sample, impact_sell_price

logging.disable(logging.CRITICAL)  # alert() logs CRITICAL; keep test output readable


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def make_monitor(**overrides) -> DislocationMonitor:
    """A monitor with no exchange clients and no CSV. Config values are passed
    explicitly so a local .env cannot change what the tests assert."""
    cfg_kwargs = dict(
        spread_threshold_pct=1.5,
        velocity_threshold_pct_min=0.5,
        confirm_seconds=180,
        min_samples=6,
        poll_interval_s=20,
        tg_retries=0,          # one attempt: no retry sleeps in tests
    )
    cfg_kwargs.update(overrides)

    m = DislocationMonitor.__new__(DislocationMonitor)
    m.cfg = Config(**cfg_kwargs)
    m.history = []
    m.last_alert = 0.0
    m.started_at = time.time()
    m.latest = None
    m.last_alert_info = None
    m.tg_fail_count = 0
    m.tg_last_error = None
    return m


def capture_alerts(m) -> list[dict]:
    """Replace alert() so evaluate() tests observe the decision, not the send."""
    calls: list[dict] = []

    async def _fake_alert(spread, velocity, reason):
        calls.append({"spread": spread, "velocity": velocity, "reason": reason})

    m.alert = _fake_alert
    return calls


def fill_history(m, spreads, span_s: float) -> None:
    """Evenly spaced samples spanning `span_s` seconds and ending now."""
    now = time.time()
    step = span_s / (len(spreads) - 1) if len(spreads) > 1 else 0.0
    m.history = [Sample(now - span_s + i * step, s) for i, s in enumerate(spreads)]


# --------------------------------------------------------------------------- #
# impact_sell_price -- depth-aware exit price
# --------------------------------------------------------------------------- #

def test_two_element_levels():
    # [price, amount] -- the classic ccxt shape.
    bids = [[100.0, 1.0], [99.0, 2.0]]
    assert impact_sell_price(bids, 50.0) == 100.0


def test_three_element_levels():
    # [price, amount, timestamp] -- Kraken et al. Must not raise; the third
    # field is ignored. Same numbers as above -> same answer.
    bids = [[100.0, 1.0, 1_700_000_000], [99.0, 2.0, 1_700_000_001]]
    assert impact_sell_price(bids, 50.0) == 100.0


def test_walks_multiple_levels():
    # $150 eats all of level 1 ($100) then $50 of level 2, so the answer must be
    # a weighted blend -- not the top-of-book price.
    vwap = impact_sell_price([[100.0, 1.0], [99.0, 2.0]], 150.0)
    assert abs(vwap - 99.6644) < 1e-3


def test_thin_book_returns_none():
    # Book can't absorb the order -> honest None, not a misleading number.
    bids = [[100.0, 0.1, 1_700_000_000]]
    assert impact_sell_price(bids, 1_000_000.0) is None


# --------------------------------------------------------------------------- #
# evaluate() -- sustained + accelerating confirmation
# --------------------------------------------------------------------------- #

def test_below_min_samples_does_not_fire():
    m = make_monitor()
    calls = capture_alerts(m)
    fill_history(m, [2.0, 2.0, 2.0], 174)      # 3 samples, min_samples is 6
    asyncio.run(m.evaluate())
    assert calls == []


def test_short_window_does_not_fire():
    # v1 bug: six fast samples spanning ~40s satisfied a "180s" confirmation.
    m = make_monitor()
    calls = capture_alerts(m)
    fill_history(m, [2.0] * 6, 40)
    asyncio.run(m.evaluate())
    assert calls == []


def test_one_sample_below_threshold_does_not_fire():
    # "Sustained" means every sample clears the threshold, not the average.
    m = make_monitor()
    calls = capture_alerts(m)
    fill_history(m, [2.0, 2.0, 1.0, 2.0, 2.0, 2.0], 174)
    asyncio.run(m.evaluate())
    assert calls == []


def test_sustained_dislocation_fires():
    m = make_monitor()
    calls = capture_alerts(m)
    fill_history(m, [2.0] * 6, 174)
    asyncio.run(m.evaluate())
    assert len(calls) == 1
    assert calls[0]["reason"] == "Sustained dislocation"


def test_velocity_is_per_minute_not_raw_difference():
    # v1 bug: velocity was last-minus-first. Here the spread climbs 2.9 points
    # over 2.9 minutes, so the correct answer is 1.0 %/min -- not 2.9.
    m = make_monitor()
    calls = capture_alerts(m)
    fill_history(m, [2.0, 2.58, 3.16, 3.74, 4.32, 4.9], 174)
    asyncio.run(m.evaluate())
    assert len(calls) == 1
    assert abs(calls[0]["velocity"] - 1.0) < 1e-9
    assert calls[0]["reason"] == "Sustained dislocation, accelerating"


def test_samples_older_than_window_are_dropped():
    m = make_monitor()
    calls = capture_alerts(m)
    now = time.time()
    m.history = (
        [Sample(now - 400 + i, 2.0) for i in range(3)]        # outside 180s window
        + [Sample(now - 20 + i * 10, 2.0) for i in range(3)]  # inside
    )
    asyncio.run(m.evaluate())
    assert calls == []
    assert len(m.history) == 3      # stale samples pruned in place


# --------------------------------------------------------------------------- #
# alert() -- cooldown and delivery reporting
# --------------------------------------------------------------------------- #

def test_alert_cooldown_suppresses_second_alert():
    m = make_monitor()
    sent: list[str] = []

    async def _fake_send(message):
        sent.append(message)
        return True

    m.send_telegram = _fake_send
    asyncio.run(m.alert(2.0, 1.0, "Sustained dislocation"))
    asyncio.run(m.alert(2.5, 1.2, "Sustained dislocation"))
    assert len(sent) == 1


def test_alert_records_delivery_failure():
    # The panel must not report an undelivered alert as delivered.
    m = make_monitor()

    async def _failing_send(message):
        return False

    m.send_telegram = _failing_send
    asyncio.run(m.alert(2.0, 1.0, "Sustained dislocation"))
    assert m.last_alert_info["delivered"] is False


def test_failed_send_is_counted_for_the_panel():
    """A send that never succeeds has to be visible, not silent."""
    import risk_monitor as rm

    m = make_monitor(tg_retries=0)
    saved = (rm.TG_TOKEN, rm.TG_CHAT_ID, rm.requests.post)
    rm.TG_TOKEN, rm.TG_CHAT_ID = "test-token", "@test-channel"

    def _boom(*args, **kwargs):
        raise ConnectionError("network down")

    rm.requests.post = _boom
    try:
        delivered = asyncio.run(m.send_telegram("hello"))
    finally:
        rm.TG_TOKEN, rm.TG_CHAT_ID, rm.requests.post = saved

    assert delivered is False
    assert m.tg_fail_count == 1
    assert "network down" in m.tg_last_error["error"]


# --------------------------------------------------------------------------- #
# _status() -- what the public panel reports
# --------------------------------------------------------------------------- #

def test_status_alive_on_fresh_snapshot():
    m = make_monitor()
    m.latest = {"ts": time.time(), "fee_adj_spread_pct": 0.5}
    status = m._status()
    assert status["alive"] is True
    assert status["seconds_since_last_check"] < 1


def test_status_stale_when_fetch_loop_stalls():
    # poll_interval_s 20 -> anything older than 60s counts as stalled.
    m = make_monitor()
    m.latest = {"ts": time.time() - 300, "fee_adj_spread_pct": 0.5}
    assert m._status()["alive"] is False


def test_status_not_alive_before_first_snapshot():
    m = make_monitor()
    status = m._status()
    assert status["alive"] is False
    assert status["last_check_ts"] is None


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
    print(f"\n{'FAILED' if failed else 'OK'} ({failed} failed)")
    raise SystemExit(1 if failed else 0)
