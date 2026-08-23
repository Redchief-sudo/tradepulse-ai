# AGENTS.md

## Project Context

This is the TradePulse AI trading runtime: a standalone Python package (`tradepulse/`), not a Base44 app. There is no frontend and no hosted backend in this repo -- everything runs locally against SQLite and the Alpaca API.

Start with `README.md` for setup and test commands.

## Key Files

- `tradepulse/`: the runtime package (models, persistence, broker client, risk engine, execution gateway, settlement pipeline).
- `python_tests/`: pytest suite (`pytest-asyncio`, `respx` for HTTP mocking).
- `.env.example`: template for local secrets; never commit `.env`.
- `docs/`: audit history and design-decision records.

## Working Notes

- Run `.venv/bin/python -m pytest python_tests -q` before finishing code changes.
- Never print or log real API keys/secrets.
- This system is paper-trading-first; live trading requires `TRADEPULSE_EXECUTION_MODE=live` and `TRADEPULSE_LIVE_TRADING_ENABLED=true` -- treat any change touching that gate as safety-critical.
