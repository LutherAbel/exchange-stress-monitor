# Cross-Exchange Liquidity & Dislocation Monitor

A real-time system that watches for **structural price dislocations in BTC** between
Binance (BTC/USDT) and USD reference venues (Coinbase, Kraken), and raises an early
warning when liquidity stress looks like it is *building and accelerating* — the kind
of regime shift that precedes a cascade.

> **The decision it supports:** "Should I de-risk *right now*?" A desk running a
> grid/market-making strategy needs to know that the venue it is exposed to is
> dislocating from the rest of the market **before** the move completes, not after.

It runs 24/7 as a deployed service on AWS EC2 and pushes alerts to Telegram.

---

## Why this is not a naive price-spread bot

A naive version ("Binance mid-price vs Coinbase mid-price, alert if gap > X") produces
constant false alarms. This system is built around the ways that signal breaks:

| Failure mode of the naive approach | How this system handles it |
| --- | --- |
| **USDT depeg masquerades as a Binance discount** (BTC/USDT vs BTC/USD aren't comparable) | Fetches `USDT/USD` and converts the Binance price into **USD terms** before comparing |
| **Mid-price ignores real exit cost** | Walks the order book to compute the **depth-aware price** you'd actually receive selling a configurable USD size (default $500k) |
| **Reads taken at different moments** look like a spread | All books are fetched **concurrently** (`asyncio.gather`); the fetch window is measured and the sample is **discarded if reads aren't near-simultaneous** |
| **One reference venue glitches** and triggers a false alert | Uses the **median** of references plus a disagreement check — if Coinbase and Kraken diverge, the sample is dropped |
| **Fees / withdrawal costs** make small spreads meaningless | A configurable **fee buffer** is subtracted before evaluation |
| **"Velocity" not time-normalized** | Computed as **%/minute** over the actual elapsed window |
| **Confirmation window not enforced** (a few fast samples fire early) | An alert requires the window to **actually span** `CONFIRM_SECONDS` *and* a minimum number of samples, with **every** sample above threshold |

Every snapshot (prices, basis, spread, fetch window) is **persisted to `snapshots.csv`**,
which doubles as the training/evaluation dataset for the anomaly-detection work below.

---

## How it works

```
            ┌── Binance  BTC/USDT ─┐
 concurrent ├── Coinbase BTC/USD  ─┤   depth-aware      basis-adjusted     time-normalized
 fetch ─────┤── Kraken   BTC/USD  ─┼─► impact price ─► USD spread (median ─► sustained +    ─► Telegram alert
            └── Kraken   USDT/USD ─┘   per venue        benchmark, fee-adj)   accelerating?     + CSV log
```

1. **Ingest** four books/tickers concurrently; reject if the fetch window is too wide.
2. **Price** the real exit cost on each venue for the configured USD size.
3. **Normalize** Binance into USD via the USDT/USD basis; build a median USD benchmark.
4. **Score** the fee-adjusted discount and its %/minute velocity over a rolling window.
5. **Confirm & alert** only on a sustained, accelerating dislocation (with cooldown).

---

## Tech stack

- **Python** (async) · **ccxt** for unified exchange APIs · **Telegram Bot API** for alerts
- Deployed on **AWS EC2** as a **systemd** service (auto-restart, boot persistence, log capture)

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env          # then edit .env
python risk_monitor.py
```

Telegram is optional — with no token set, alerts are logged to console/file instead.

## Deploy (AWS EC2, systemd)

A `systemd` unit keeps it running across disconnects and reboots with auto-restart;
logs are captured to disk. (Unit file in deployment notes.)

---

## Known limitations & roadmap

Stated explicitly, because knowing where a model breaks is part of the job:

- **Alerting only, by design.** This raises a signal; it does **not** place or close
  orders. There is a documented integration hook, but no trading keys live in this repo.
- **Single pair (BTC).** The basis/benchmark logic generalizes, but only BTC is wired up.
- **Latency is best-effort.** It bounds the fetch window rather than guaranteeing
  exchange-timestamp alignment; it is not a co-located HFT system, and isn't trying to be.
- **Thresholds are still rule-based.** That motivates the next phase:

### v2 — anomaly detection (in progress)
Using the persisted `snapshots.csv`: replace the hand-set thresholds with a
**time-series anomaly detector** (robust z-score / EWMA control charts, with an
isolation-forest / autoencoder baseline), and — the part that matters —
**evaluate it against labeled historical stress events** (precision/recall vs the
naive threshold) rather than asserting it works.

---

*Disclaimer: research/educational project. Not financial advice.*
