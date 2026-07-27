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
- `live` is enabled by `TRADING_MODE=live` and requires a non-empty
  `TELEGRAM_ALLOWED_USER_IDS` plus private WebSocket monitoring.

Private monitoring additionally requires:

```env
ENABLE_PRIVATE_WEBSOCKET=true
```

It is forcibly disabled in `dry-run` and required in `live`, because partial
TP orders are installed only after the entry fill is confirmed. Do not enable
live mode until the API key has read/trade permissions only and withdrawals
are disabled.

## Trading flow

- A signal is parsed in Telegram.
- Risk-based quantity is calculated from the planned entry and stop loss.
- The entry order uses the full quantity and includes the common SL.
- After the entry is confirmed filled, partial TP orders are added. Three
  targets use 50/30/20; five use 40/25/15/10/10.
- After TP1 is confirmed filled by WebSocket, Telegram shows the current
  position details and asks for confirmation. Only after confirmation is the
  remaining SL moved to fee-aware break-even based on the actual average
  entry, deducted fees, paid funding, estimated closing taker fee, and one
  price tick of buffer.
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
