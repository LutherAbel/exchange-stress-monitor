# Cross-Exchange Liquidity & Dislocation Monitor

A real-time system that watches for **structural price dislocations in BTC** between
Binance (BTC/USDT) and USD reference venues (Coinbase, Kraken), and raises an early
warning when liquidity stress looks like it is *building and accelerating* — the kind
of regime shift that precedes a cascade.

> **The decision it supports:** "Should I de-risk *right now*?" A desk running a
> grid/market-making strategy needs to know that the venue it is exposed to is
> dislocating from the rest of the market **before** the move completes, not after.

It runs 24/7 as a deployed service on AWS EC2. It is **quiet by design** — Telegram
only receives a message when a real dislocation is confirmed (no periodic "still
alive" noise). Liveness and live metrics are exposed instead through a lightweight
**HTTP status panel**.

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
            ┌── Binance  BTC/USDT ─┐                                                       ┌─► snapshots.csv   (every snapshot)
 concurrent ├── Coinbase BTC/USD  ─┤   depth-aware      basis-adjusted     time-normalized │
 fetch ─────┤── Kraken   BTC/USD  ─┼─► impact price ─► USD spread (median ─► sustained &  ─┼─► status panel    (live, always)
 (20s)      └── Kraken   USDT/USD ─┘   per venue        benchmark, fee-adj)  accelerating? └─► Telegram channel (anomaly ONLY)
```

1. **Ingest** four books/tickers concurrently; reject if the fetch window is too wide.
2. **Price** the real exit cost on each venue for the configured USD size.
3. **Normalize** Binance into USD via the USDT/USD basis; build a median USD benchmark.
4. **Score** the fee-adjusted discount and its %/minute velocity over a rolling window.
5. **Emit:** every snapshot is logged to CSV and published to the status panel; Telegram
   fires **only** on a confirmed sustained dislocation (with cooldown) — never on a timer.

---

## Runtime architecture

A single Python process runs **two concurrent jobs in one asyncio event loop**:

- the **monitor loop** — fetch → score → maybe-alert, every `POLL_INTERVAL_S`
- the **status-panel server** (aiohttp) — serves `GET /` and `GET /healthz`

The loop keeps the latest snapshot in memory; the panel reads that shared state, so it
adds no extra exchange calls. Deployed topology:

```
 AWS EC2  (Ubuntu 24.04, ap-southeast-2 / Sydney)
   └─ systemd service ............... auto-restart on crash, starts on boot
        └─ .venv/bin/python risk_monitor.py   (config from .env)
             ├─ outbound → Binance / Coinbase / Kraken  (REST, every 20s)
             ├─ outbound → Telegram Bot API → channel    (anomaly only)
             └─ inbound  ← :8080  via Elastic IP + security group → public status panel
```

---

## Status panel

A small read-only web panel runs in the same process (no extra service) and is the
way to answer *"is it alive, and what is it seeing right now?"* — replacing the old
periodic Telegram heartbeat.

- `GET /` — auto-refreshing HTML panel: alive/stale badge, current fee-adjusted
  spread, Binance-USD vs benchmark, last check age, uptime, and the last alert.
- `GET /healthz` — JSON for uptime monitors. Returns **200** when a fresh snapshot
  arrived within ~3 poll intervals, **503** when the fetch loop has gone stale.

Binds `0.0.0.0:PANEL_PORT` (default `8080`). On EC2, open that port in the instance's
security group to reach it at `http://<ec2-public-ip>:8080/`. The panel is public and
read-only — it exposes market metrics only (no keys, no positions).

## Telegram: bot vs channel

The bot is the *sender*; the recipient is whatever `TG_CHAT_ID` points at. To broadcast
alerts to a **channel**: create the channel, add the bot as an **admin**, and set
`TG_CHAT_ID` to the channel's `@username` (public) or `-100…` id (private). No code change.

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
Once running, open the status panel at <http://localhost:8080/>.

## Deploy (AWS EC2, systemd)

Clone the repo, create the `.venv`, fill in `.env`, then run it under a `systemd` unit so
it survives disconnects/reboots and auto-restarts on crash. Expose the panel by opening
`PANEL_PORT` in the instance security group; use an **Elastic IP** for a stable public URL.
Step-by-step commands (SSH, AWS setup, systemd, update/redeploy) live in `note.md`.

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
