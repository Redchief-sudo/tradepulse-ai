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

## Running a scan cycle

Each invocation performs one AI-driven scan cycle (discovery via Claude, then risk-gated execution through Alpaca) and exits -- there is no internal scheduling loop. Point cron (or a systemd timer) at it:

```bash
.venv/bin/tradepulse scan
```

Requires `ALPACA_API_KEY`, `ALPACA_API_SECRET`, and `ANTHROPIC_API_KEY` set (see `.env.example`). Overlapping invocations are the operator's responsibility to prevent (e.g. `flock`) -- the process does not take an inter-process lock.

## Status

The core runtime (models, persistence, broker client, risk engine, execution gateway, settlement pipeline, AI-driven scan cycle) is built and tested. Still missing before this is a fully unattended paper-trading system: a stop-loss/target monitor for open positions, and reconciliation against Alpaca's own activity feed. See `docs/` for the full audit history.
