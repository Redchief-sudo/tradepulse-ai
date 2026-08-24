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

## Running the runtime

No command runs its own scheduling loop -- each does its work once and exits. Point cron (or a systemd timer) at them:

```bash
.venv/bin/tradepulse scan       # AI-driven candidate discovery + position monitor, run CONCURRENTLY
.venv/bin/tradepulse monitor    # position monitor alone, for a tighter cadence than scan's
.venv/bin/tradepulse settle     # drains any due settlement retries independently of new trades
.venv/bin/tradepulse reconcile  # after-the-fact audit against Alpaca's real positions/fills
```

`scan` requires `ALPACA_API_KEY`, `ALPACA_API_SECRET`, and whichever AI provider's key is currently selected; `monitor`/`settle`/`reconcile` need only the Alpaca credentials (see `.env.example`).

AI discovery backend is configurable via `TRADEPULSE_AI_PROVIDER` (`anthropic`, the default, or `openai`) -- set it plus the matching `ANTHROPIC_API_KEY`/`ANTHROPIC_MODEL` or `OPENAI_API_KEY`/`OPENAI_MODEL`. Both backends are validated against the identical fail-closed candidate schema (`tradepulse/providers/ai_provider.py`); the AI never controls price, quantity, stop-loss, or target regardless of which one is selected.

Example crontab:

```
*/15 9-16 * * 1-5  cd /path/to/repo && .venv/bin/tradepulse scan      >> /var/log/tradepulse.log 2>&1
*/2  9-16 * * 1-5  cd /path/to/repo && .venv/bin/tradepulse monitor   >> /var/log/tradepulse.log 2>&1
*    *    * * *    cd /path/to/repo && .venv/bin/tradepulse settle    >> /var/log/tradepulse.log 2>&1
0    */6  * * *    cd /path/to/repo && .venv/bin/tradepulse reconcile >> /var/log/tradepulse.log 2>&1
```

An overlapping invocation of the same command is blocked at the application level by a per-command database-enforced lease (not by cron/flock discipline) -- a caller that loses the race exits cleanly (status 0) rather than racing the live run. `scan` runs discovery and position protection under separate leases in the same process, so a slow AI call never delays protective exits.

## Status

The core runtime (models, persistence, broker client, risk engine, execution gateway, settlement pipeline, AI-driven scan cycle gated by both AI and deterministic technical/momentum/risk signals, position monitor, broker reconciliation) is built and tested. The execution gateway and reconciliation share one canonical path (`execution/fill_attribution.py`) that creates every local `Fill`/`SettlementEvent` from Alpaca's real, validated per-fill activity ID -- never a locally-synthesized one -- whether attributed live during polling or recovered later by reconciliation for an order the live poll window already gave up on. Known deferred items: session start/stop/status as the sole authority for the trading session (a missing session row correctly fails closed to disabled, but there's no normal operator command to enable one), equity market-hours enforcement via Alpaca's own clock endpoint (currently relies on cron scheduling), and wiring in the deterministic market-regime classifier once an intraday candle source exists (its volatility math currently assumes 5-minute bars; this codebase only fetches daily ones). See `docs/` for the full audit history.
