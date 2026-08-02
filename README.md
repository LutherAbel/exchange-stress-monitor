# Cross-Exchange Liquidity and Dislocation Monitor

This compares the BTC price on Binance (BTC/USDT) against Coinbase and Kraken (BTC/USD)
and alerts when the gap is both large and widening.

The question it answers is whether to de-risk now. A desk running a grid strategy on
Binance needs to know its venue is separating from the rest of the market before the
move finishes. Whether the gap then closes or cascades is not something this can tell you.

It runs on an AWS EC2 instance as a systemd service. Telegram receives a message only
when a dislocation is confirmed; liveness and current readings go to an HTTP status
panel instead.

---

## Method

```
            ┌── Binance  BTC/USDT ─┐                                                       ┌─► snapshots.csv   (every snapshot)
 concurrent ├── Coinbase BTC/USD  ─┤   depth-aware      basis-adjusted     time-normalized │
 fetch ─────┤── Kraken   BTC/USD  ─┼─► impact price ─► USD spread (median ─► sustained &  ─┼─► status panel    (live, always)
 (20s)      └── Kraken   USDT/USD ─┘   per venue        benchmark, fee-adj)  accelerating? └─► Telegram channel (anomaly only)
```

Binance quotes BTC in USDT, so the price is converted to USD with the USDT/USD rate
before comparison. Without this a USDT depeg reads as a Binance discount.

Mid-price is not used. The order book is walked to the price a market sell of a
configured size (default $500k) would actually receive, since that is the number that
matters when deciding whether to exit.

The three books and the USDT/USD ticker are fetched concurrently and the elapsed window
is recorded. A sample is discarded when that window is too wide, or when Coinbase and
Kraken disagree by more than the tolerance, because a spread measured from reads taken
at different moments is not a spread. The benchmark is the median of the two USD venues.

A fee buffer is subtracted before evaluation. An alert then requires every sample in the
window above threshold, a window that actually spans `CONFIRM_SECONDS`, and a minimum
sample count; velocity is per minute over the elapsed window rather than a raw
first-to-last difference.

Every snapshot is written to `snapshots.csv`, which is also the dataset for the anomaly
detection work below.

---

## Runtime architecture

A single Python process runs two jobs in one asyncio event loop: the monitor loop
(fetch, score, maybe alert, every `POLL_INTERVAL_S`) and the aiohttp panel server. The
loop keeps the latest snapshot in memory and the panel reads that state, so the panel
adds no exchange calls.

```
 AWS EC2  (Ubuntu 24.04, ap-southeast-2 / Sydney)
   └─ systemd service ............... auto-restart on crash, starts on boot
        └─ .venv/bin/python risk_monitor.py   (config from .env)
             ├─ outbound → Binance / Coinbase / Kraken  (REST, every 20s)
             ├─ outbound → Telegram Bot API → channel    (anomaly only)
             └─ inbound  ← :8080  via Elastic IP + security group → public status panel
```

## Status panel

`GET /` returns an auto-refreshing page: alive/stale badge, current fee-adjusted spread,
Binance USD against the benchmark, last check age, uptime, and the last alert.
`GET /healthz` returns the same state as JSON, with status 200 when a snapshot arrived
within about three poll intervals and 503 once the fetch loop has gone stale, so an
uptime monitor can watch it.

The server binds `0.0.0.0:PANEL_PORT` (default `8080`). It is public and read-only, and
exposes market metrics only.

## Telegram

The bot is the sender and `TG_CHAT_ID` is the recipient. To post into a channel, create
the channel, add the bot as an admin, and set `TG_CHAT_ID` to its `@username` or `-100…`
id. No code change is needed.

A send that fails is retried, and repeated failures are counted and exposed on the panel,
so a broken alert channel is visible rather than silent.

## Running it

Python with ccxt for the exchange APIs and aiohttp for the panel.

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env          # then edit .env
python risk_monitor.py
```

Without a Telegram token, alerts are logged to console and file instead. The panel is at
<http://localhost:8080/>.

Tests run with `python -m pytest test_risk_monitor.py` or `python test_risk_monitor.py`.
They cover the alert decision logic and the panel state, and need no network.

## Deployment

Clone the repo, create the `.venv`, fill in `.env`, then run it under a systemd unit so
it survives disconnects and reboots. Open `PANEL_PORT` in the instance security group to
reach the panel, and attach an Elastic IP for a stable address.

---

## Limitations

- Alerting only. It does not place or close orders, and no trading keys are in this repo.
- BTC only. The basis and benchmark logic generalises, but nothing else is wired up.
- The fetch window is bounded rather than aligned on exchange timestamps, so simultaneity
  is approximate. This is not a co-located system.
- Thresholds are hand-set. Whether they generalise beyond the observed period is untested.

### Next: anomaly detection

Replace the hand-set thresholds with a time-series detector (robust z-score or EWMA
control charts, with an isolation forest or autoencoder as a baseline) fitted on
`snapshots.csv`, then score it against labelled historical stress events for precision
and recall against the current threshold.

---

*Disclaimer: research/educational project. Not financial advice.*
