"""
Cross-Exchange Liquidity & Dislocation Monitor
==============================================

Real-time monitor that detects structural price dislocations for BTC between
Binance (BTC/USDT) and USD reference venues (Coinbase, Kraken). It adjusts for
the USDT/USD basis and for order-book depth (the price you would *actually*
receive selling a given USD size), then alerts when a sustained, fee-adjusted
dislocation accelerates -- the kind of signal that precedes a liquidity cascade.

This is an alerting / decision-support tool, NOT an automated trade executor.
See README.md ("Known limitations & roadmap") for scope and assumptions.
"""

from __future__ import annotations

import asyncio
import csv
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

import ccxt.async_support as ccxt
import requests
from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------------------------------- #
# Configuration (all overridable via environment variables / .env)
# --------------------------------------------------------------------------- #

TG_TOKEN = os.environ.get("TG_TOKEN")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID")


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass
class Config:
    depth_usd: float = _f("DEPTH_USD", 500_000)                 # simulated market-sell size
    spread_threshold_pct: float = _f("SPREAD_THRESHOLD_PCT", 1.5)
    velocity_threshold_pct_min: float = _f("VELOCITY_THRESHOLD_PCT_MIN", 0.5)
    fee_buffer_pct: float = _f("FEE_BUFFER_PCT", 0.2)           # round-trip fee/withdrawal buffer
    confirm_seconds: float = _f("CONFIRM_SECONDS", 180)
    min_samples: int = int(_f("MIN_SAMPLES", 6))
    max_fetch_window_s: float = _f("MAX_FETCH_WINDOW_S", 2.0)   # reject non-simultaneous reads
    benchmark_tolerance_pct: float = _f("BENCHMARK_TOLERANCE_PCT", 0.5)
    poll_interval_s: float = _f("POLL_INTERVAL_S", 20)
    heartbeat_interval_s: float = _f("HEARTBEAT_INTERVAL_S", 3600)
    data_log_path: str = os.environ.get("DATA_LOG_PATH", "snapshots.csv")


@dataclass
class Sample:
    ts: float
    spread_pct: float


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

def impact_sell_price(bids, usd_size: float) -> float | None:
    """Volume-weighted price received when market-selling `usd_size` worth of
    the base asset into `bids`. Returns None if the book is too thin to absorb
    the order (an honest 'unknown' rather than a misleading number)."""
    remaining = usd_size
    total_base = 0.0
    total_quote = 0.0
    for price, amount in bids:
        level_value = price * amount
        take = min(level_value, remaining)
        total_quote += take
        total_base += take / price
        remaining -= take
        if remaining <= 0:
            break
    if remaining > 0 or total_base == 0:
        return None
    return total_quote / total_base


# --------------------------------------------------------------------------- #
# Monitor
# --------------------------------------------------------------------------- #

class DislocationMonitor:
    def __init__(self, config: Config | None = None):
        self.cfg = config or Config()
        self.history: list[Sample] = []
        self.last_heartbeat = 0.0
        self.last_alert = 0.0

        self.binance = ccxt.binance({"enableRateLimit": True})
        self.coinbase = ccxt.coinbase({"enableRateLimit": True})
        self.kraken = ccxt.kraken({"enableRateLimit": True})

        self._init_csv()

    # -- persistence -------------------------------------------------------- #
    def _init_csv(self) -> None:
        p = Path(self.cfg.data_log_path)
        if not p.exists():
            with p.open("w", newline="") as f:
                csv.writer(f).writerow(
                    ["ts_iso", "binance_usdt", "usdt_usd", "binance_usd",
                     "coinbase_usd", "kraken_usd", "benchmark_usd",
                     "raw_spread_pct", "fee_adj_spread_pct", "fetch_window_s"]
                )

    def _record(self, row: list) -> None:
        with Path(self.cfg.data_log_path).open("a", newline="") as f:
            csv.writer(f).writerow(row)

    # -- ingestion (concurrent) -------------------------------------------- #
    async def fetch_snapshot(self) -> dict | None:
        """Fetch all books concurrently so the reads are near-simultaneous,
        then basis-adjust and compute the depth-aware, fee-adjusted spread."""
        t0 = time.time()
        b_ob, c_ob, k_ob, usdt_tkr = await asyncio.gather(
            self.binance.fetch_order_book("BTC/USDT", limit=200),
            self.coinbase.fetch_order_book("BTC/USD", limit=200),
            self.kraken.fetch_order_book("BTC/USD", limit=200),
            self.kraken.fetch_ticker("USDT/USD"),
            return_exceptions=True,
        )
        fetch_window_s = time.time() - t0

        for name, r in (("binance", b_ob), ("coinbase", c_ob),
                        ("kraken", k_ob), ("usdt/usd", usdt_tkr)):
            if isinstance(r, Exception):
                logging.warning("fetch failed for %s: %s", name, r)
                return None

        b_usdt = impact_sell_price(b_ob["bids"], self.cfg.depth_usd)
        c_usd = impact_sell_price(c_ob["bids"], self.cfg.depth_usd)
        k_usd = impact_sell_price(k_ob["bids"], self.cfg.depth_usd)
        usdt_usd = usdt_tkr.get("last") or usdt_tkr.get("close")

        if not all([b_usdt, c_usd, k_usd, usdt_usd]):
            logging.warning("insufficient depth or missing USDT/USD; skipping")
            return None

        # Basis adjustment: convert Binance's USDT price into USD terms so a
        # USDT depeg can't masquerade as a Binance discount.
        b_usd = b_usdt * usdt_usd

        # Reference sanity: if the two USD venues disagree, don't trust either.
        disagreement = abs(c_usd - k_usd) / median([c_usd, k_usd]) * 100
        if disagreement > self.cfg.benchmark_tolerance_pct:
            logging.warning("reference venues disagree by %.2f%%; skipping", disagreement)
            return None
        benchmark = median([c_usd, k_usd])

        raw_spread = (benchmark - b_usd) / benchmark * 100
        fee_adj_spread = raw_spread - self.cfg.fee_buffer_pct

        self._record([
            datetime.now(timezone.utc).isoformat(),
            round(b_usdt, 2), round(usdt_usd, 5), round(b_usd, 2),
            round(c_usd, 2), round(k_usd, 2), round(benchmark, 2),
            round(raw_spread, 4), round(fee_adj_spread, 4), round(fetch_window_s, 3),
        ])

        logging.info(
            "spread(fee-adj): %.3f%% | BN(USD): %.0f | bench: %.0f | window: %.2fs",
            fee_adj_spread, b_usd, benchmark, fetch_window_s,
        )

        # Enforce simultaneity: stale/uneven reads are recorded but not evaluated.
        if fetch_window_s > self.cfg.max_fetch_window_s:
            logging.warning("fetch window %.2fs > max; recorded but not evaluated",
                            fetch_window_s)
            return None

        return {"spread_pct": fee_adj_spread}

    # -- evaluation -------------------------------------------------------- #
    async def evaluate(self) -> None:
        now = time.time()
        self.history = [s for s in self.history if now - s.ts <= self.cfg.confirm_seconds]
        if len(self.history) < self.cfg.min_samples:
            return

        span = self.history[-1].ts - self.history[0].ts
        # Actually enforce the confirmation window (the v1 bug: 3 samples ~= 40s).
        if span < self.cfg.confirm_seconds * 0.9:
            return

        spreads = [s.spread_pct for s in self.history]
        sustained = all(s > self.cfg.spread_threshold_pct for s in spreads)
        if not sustained:
            return

        # Time-normalized velocity in %/minute (the v1 bug: raw last-minus-first).
        velocity = (spreads[-1] - spreads[0]) / (span / 60.0)
        accelerating = velocity > self.cfg.velocity_threshold_pct_min

        reason = "Sustained dislocation, accelerating" if accelerating else "Sustained dislocation"
        await self.alert(spreads[-1], velocity, reason)

    # -- alerting (NOT trading) -------------------------------------------- #
    async def alert(self, spread: float, velocity: float, reason: str) -> None:
        now = time.time()
        if now - self.last_alert < self.cfg.confirm_seconds:
            return  # cooldown
        self.last_alert = now

        msg = (
            f"*{reason}*\n"
            f"------------------\n"
            f"Fee-adjusted discount: *{spread:.2f}%*\n"
            f"Velocity: *{velocity:.2f}%/min*\n"
            f"------------------\n"
            f"Suggested action: de-risk / halt strategy and review."
        )
        logging.critical(msg.replace("\n", " | "))
        await self.send_telegram(msg)
        # Integration hook: wire your own execution layer here if desired.
        # Intentionally not implemented in this repo (no trading keys committed).

    # -- telegram (async-safe) --------------------------------------------- #
    async def send_telegram(self, message: str) -> None:
        if not TG_TOKEN or not TG_CHAT_ID:
            logging.info("[telegram disabled] %s", message.replace("\n", " | "))
            return

        def _post():
            url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
            return requests.post(
                url,
                data={"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "Markdown"},
                timeout=5,
            )

        try:
            resp = await asyncio.to_thread(_post)
            if resp.status_code != 200:
                logging.error("telegram send failed: %s", resp.text)
        except Exception as e:
            logging.error("telegram connection error: %s", e)

    async def heartbeat(self) -> None:
        now = time.time()
        if now - self.last_heartbeat > self.cfg.heartbeat_interval_s:
            await self.send_telegram("Monitor alive | spread within normal range")
            self.last_heartbeat = now

    # -- main loop --------------------------------------------------------- #
    async def tick(self) -> None:
        snap = await self.fetch_snapshot()
        if snap is not None:
            self.history.append(Sample(time.time(), snap["spread_pct"]))
            await self.evaluate()
        await self.heartbeat()

    async def run(self) -> None:
        logging.info("monitor started | depth: %s USD | interval: %ss",
                     self.cfg.depth_usd, self.cfg.poll_interval_s)
        if not TG_TOKEN or not TG_CHAT_ID:
            logging.warning("Telegram not configured; alerts will log only.")
        await self.send_telegram("Monitor started | initializing market data...")
        try:
            while True:
                try:
                    await self.tick()
                except Exception as e:
                    logging.error("loop error: %s", e)
                await asyncio.sleep(self.cfg.poll_interval_s)
        finally:
            await self.close()

    async def close(self) -> None:
        await asyncio.gather(
            self.binance.close(), self.coinbase.close(), self.kraken.close(),
            return_exceptions=True,
        )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.FileHandler("risk_monitor_log.txt"), logging.StreamHandler()],
    )
    try:
        asyncio.run(DislocationMonitor().run())
    except KeyboardInterrupt:
        logging.info("stopped by user")
