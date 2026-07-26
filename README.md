# Futures Bot

Telegram-assisted Bitunix futures bot. The default execution mode is
`dry-run`; write requests are simulated and private WebSocket monitoring is
disabled.

## Setup

Use Python 3.12 and install the project dependencies:

```bash
python -m pip install -e ".[test]"
```

Copy `.env.example` to `.env` and fill values locally. Never commit `.env`,
API keys, Telegram tokens, generated CSV journals, or proxy credentials.

## Modes

- `dry-run` is the safe default.
- `testnet` requires separate REST and WebSocket testnet URLs.
- `live` requires `BITUNIX_LIVE_TRADING_ENABLED=true` and a non-empty
  `TELEGRAM_ALLOWED_USER_IDS`.

Private monitoring additionally requires:

```env
ENABLE_PRIVATE_WEBSOCKET=true
```

It is forcibly disabled in `dry-run`. Do not enable it until the API key has
read/trade permissions only and withdrawals are disabled.

## Trading flow

- A signal is parsed in Telegram.
- Risk-based quantity is calculated from the planned entry and stop loss.
- Only TP1 is used, for 100% of the position.
- Price inside the entry range creates a market order.
- Price outside the range creates a limit order at the range midpoint.
- Every state-changing command requires a one-time confirmation.

## Monitoring and journal

When explicitly enabled, the bot logs in to the Bitunix private WebSocket and
subscribes to `order`, `position`, `balance`, and `tpsl` in one request. It
uses ping heartbeats and exponential reconnect delays.

After every connection it reconciles pending REST orders, open positions, and
pending TP/SL orders. Telegram warnings are sent for positions missing TP or
SL. Order fills, cancellations, position open/close events, and TP/SL events
are appended to `data/trade_journal.csv` and sent to allowed Telegram users.

The CSV journal is append-only, uses an OS file lock, and deduplicates
WebSocket events by source event ID. Bitunix remains the source of truth.

## Telegram commands

- `/mode`
- `/positions`
- `/orders`
- `/cancel_order SYMBOL ORDER_ID`
- `/close_position POSITION_ID`

## Tests

Tests use fakes and do not connect to Bitunix:

```bash
python -m pytest
```
