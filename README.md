# TradePulse AI

A standalone Python trading runtime: multi-asset (equities + crypto via Alpaca), paper-trading-first, with a typed persistence layer, risk engine, execution gateway, and settlement pipeline. See `docs/` for the audit history behind the current design decisions.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env  # fill in ALPACA_API_KEY / ALPACA_API_SECRET, etc.
```

## Tests

```bash
.venv/bin/python -m pytest python_tests -q
```

## Status

The core runtime (models, persistence, broker client, risk engine, execution gateway, settlement pipeline) is built and tested. There is no CLI entrypoint or scheduler yet -- see `docs/` for the current gap list before this can run an unattended paper-trading loop.
