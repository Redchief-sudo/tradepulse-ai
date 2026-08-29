# AGENTS.md

## Project Context

This is the TradePulse AI trading runtime: a standalone Python package (`tradepulse/`), not a Base44 app. Everything runs locally against SQLite and the Alpaca API -- there is no hosted backend. There IS an optional local-only web dashboard (`tradepulse dashboard`, `tradepulse/web/` + `frontend/`) for operator observability/control; it always binds `127.0.0.1` and is never network-reachable -- treat any change that could widen that as safety-critical (see Working Notes).

Start with `README.md` for setup and test commands.

## Key Files

- `tradepulse/`: the runtime package (models, persistence, broker client, risk engine, execution gateway, settlement pipeline).
- `tradepulse/session_commands.py`: the sole session-control authority (start/stop/status/reset-risk/reset-integrity) -- both `cli.py` and `tradepulse/web/` call these same functions, never a second implementation of the state machine.
- `tradepulse/web/`: the local dashboard's FastAPI backend (optional `web` extra -- `pip install -e ".[web]"`).
- `frontend/`: the dashboard's React/Vite frontend (`npm install && npm run build`; built assets are served by `tradepulse dashboard`).
- `python_tests/`: pytest suite (`pytest-asyncio`, `respx` for HTTP mocking).
- `.env.example`: template for local secrets; never commit `.env`.
- `docs/`: audit history and design-decision records.

## Working Notes

- Run `.venv/bin/python -m pytest python_tests -q` before finishing code changes.
- Never print or log real API keys/secrets.
- This system is paper-trading-first; live trading requires `TRADEPULSE_EXECUTION_MODE=live` and `TRADEPULSE_LIVE_TRADING_ENABLED=true` -- treat any change touching that gate as safety-critical.
- `tradepulse dashboard` must always bind `127.0.0.1` -- there is no authentication/authorization layer yet, so anything network-reachable would be unauthenticated start/stop/reset-risk/reset-integrity control-plane access. Never add a `--host` flag or any other way to bind elsewhere without also adding real auth first.
