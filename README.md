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
.venv/bin/tradepulse scan --asset-class=equity  # AI-driven equity/ETF discovery + position monitor, run CONCURRENTLY
.venv/bin/tradepulse scan --asset-class=crypto  # same, for the crypto lane -- independent schedule, independent lock
.venv/bin/tradepulse scan --asset-class=option  # same, for the options lane (long calls only -- see below)
.venv/bin/tradepulse scan --asset-class equity crypto option  # or fan all three lanes out from ONE invocation, concurrently
.venv/bin/tradepulse monitor    # position monitor alone, for a tighter cadence than scan's
.venv/bin/tradepulse settle     # drains any due settlement retries independently of new trades
.venv/bin/tradepulse reconcile  # after-the-fact audit against Alpaca's real positions/fills
```

`scan` requires `ALPACA_API_KEY`, `ALPACA_API_SECRET`, and whichever AI provider's key is currently selected; `monitor`/`settle`/`reconcile` need only the Alpaca credentials (see `.env.example`). Equity, crypto, and options are discovery-only lanes -- separate AI prompts and cron cadences (or run together from one `scan` invocation, see above), but one shared risk engine, execution gateway, and settlement/reconciliation pipeline underneath all three. The AI never picks a specific option contract either: it gives a directional view on an underlying, and a deterministic (non-AI) rule resolves the actual strike/expiry -- see `strategy/options_selection.py`. Options support is intentionally minimal for now: long calls only (no puts, no spreads, no short options), a flat pct-of-premium stop instead of Greeks, and a forced close within a configurable number of days before expiry.

AI discovery backend is configurable via `TRADEPULSE_AI_PROVIDER` (`anthropic`, the default, or `openai`) -- set it plus the matching `ANTHROPIC_API_KEY`/`ANTHROPIC_MODEL` or `OPENAI_API_KEY`/`OPENAI_MODEL`. Both backends are validated against the identical fail-closed candidate schema (`tradepulse/providers/ai_provider.py`); the AI never controls price, quantity, stop-loss, or target regardless of which one is selected.

Alpaca market-data feed is resolved via `ALPACA_MARKET_DATA_TIER` (default `auto`): probes SIP (equities) and OPRA (options) entitlement independently at the start of every `scan`/`monitor` invocation and uses whichever your account is actually authorized for, falling back per-feed to IEX/indicative where it isn't -- a free "Basic" Alpaca account works out of the box, no data subscription required. Set `basic` to force IEX/indicative without probing, or `algo_trader_plus` to require SIP+OPRA and fail startup cleanly if either isn't authorized (never a silent downgrade). The resolved feed is fixed for that whole invocation and recorded on every `Opportunity` (`market_data_feed`/`market_data_authority` in its metadata) so paper-trading results can be separated into consolidated (SIP/OPRA) vs. non-consolidated (IEX/indicative) evidence -- see `providers/market_data_capability.py`.

Every Alpaca HTTP call is rate-limit aware (`tradepulse/broker/alpaca_client.py`): a 429 is retried with bounded exponential backoff (1s/2s/4s/8s, capped at 16s, plus jitter, 4 retries max), honoring `Retry-After`/`X-RateLimit-Reset` when Alpaca actually sends them and falling back to plain backoff when it doesn't -- Alpaca's Basic tier throttles at 200 requests/minute/account, and this keeps a transient throttle from failing a whole scan/monitor cycle. Order submission is the one deliberate exception: a 429 from `place_order` is never auto-retried, since financial correctness can't depend on assuming Alpaca didn't process it -- it flows into the existing `client_order_id` recovery path instead (see `execution/gateway.py::_recover_unknown_submission`), never a duplicate order. The latest observed `X-RateLimit-*` snapshot is available at `GET /api/rate-limit` on the dashboard.

Example crontab:

```
*/15 9-16 * * 1-5  cd /path/to/repo && .venv/bin/tradepulse scan --asset-class=equity  >> /var/log/tradepulse.log 2>&1
*/10 *    * * *    cd /path/to/repo && .venv/bin/tradepulse scan --asset-class=crypto  >> /var/log/tradepulse.log 2>&1
*/20 9-16 * * 1-5  cd /path/to/repo && .venv/bin/tradepulse scan --asset-class=option   >> /var/log/tradepulse.log 2>&1
*/2  9-16 * * 1-5  cd /path/to/repo && .venv/bin/tradepulse monitor   >> /var/log/tradepulse.log 2>&1
*    *    * * *    cd /path/to/repo && .venv/bin/tradepulse settle    >> /var/log/tradepulse.log 2>&1
0    */6  * * *    cd /path/to/repo && .venv/bin/tradepulse reconcile >> /var/log/tradepulse.log 2>&1
```

An overlapping invocation of the same command is blocked at the application level by a per-command database-enforced lease (not by cron/flock discipline) -- a caller that loses the race exits cleanly (status 0) rather than racing the live run. `scan` runs discovery and position protection under separate leases in the same process, so a slow AI call never delays protective exits.

## Dashboard

A local-only operator dashboard -- read-only observability (session state, positions, opportunities, fills/settlement, PnL, risk exposure, reconciliation/audit alerts) plus start/stop/reset-risk/reset-integrity controls. Every control calls the exact same functions `tradepulse start`/`stop`/`reset-risk`/`reset-integrity` do (`tradepulse/session_commands.py`) -- a dashboard button is never a second implementation of the session state machine.

```bash
.venv/bin/pip install -e ".[web]"
cd frontend && npm install && npm run build && cd ..
.venv/bin/tradepulse dashboard  # serves both the API and the built frontend at http://127.0.0.1:8000
```

For frontend development with hot-reload, run `npm run dev` inside `frontend/` (proxies `/api/*` to a `tradepulse dashboard` process running separately) instead of building.

**Always binds `127.0.0.1` -- there is no `--host` flag and no remote-access option.** With no authentication/authorization layer yet, anything network-reachable would be unauthenticated `start`/`stop`/`reset-risk`/`reset-integrity` control-plane access; remote access is a later phase that must ship with real auth, not a flag that bypasses having one.

## `tradepulse run` -- one-command interactive startup

```bash
.venv/bin/tradepulse run  # dashboard + session activation + parallel equity/crypto/option/monitor/settle, until Ctrl+C
```

The normal way to run a paper session interactively: resolves market-data capabilities, opens the dashboard (same `127.0.0.1`-only bind as `tradepulse dashboard`, auto-opened in a browser unless `--no-browser`), activates the trading session (`tradepulse start`), then keeps equity, crypto, and option discovery, the position monitor, and settlement cycling on their own independent schedules -- five genuinely concurrent tasks, never a shared serial loop, so a slow equity AI call can never delay crypto/options or protective exits. This is the one deliberate, explicit exception to "no command runs its own scheduling loop" above; `scan`/`monitor`/`settle`/`reconcile` are unaffected and remain fully usable standalone for cron, debugging, or recovery. Reconciliation stays cron-only (an after-the-fact audit pass, not part of the same-cycle transaction path) -- settlement is included since a self-contained paper run must actually settle its own fills into holdings/PnL.

If session activation is refused (`RISK_STOPPED`, `FINANCIAL_INTEGRITY_BLOCKED`, broker unreachable, ...), the dashboard still comes up so you can see why and fix it, but no scan/monitor/settlement task starts -- restart `tradepulse run` once the condition is cleared. A lane that crashes from an unhandled error stops itself (never its siblings), and is reported via a critical `AuditEvent` (visible in the dashboard's existing alerts panel) rather than misusing `RISK_STOPPED`/`FINANCIAL_INTEGRITY_BLOCKED`, which have their own specific meanings. Ctrl+C (or SIGTERM) stops new cycles from starting, lets anything already in flight finish, then shuts the dashboard down and releases every lease cleanly. A second `tradepulse run` invoked while one is already active refuses immediately via its own whole-lifetime lock, never racing the first.

## Status

The core runtime (models, persistence, broker client, risk engine, execution gateway, settlement pipeline, AI-driven scan cycle gated by both AI and deterministic technical/momentum/risk signals, position monitor, broker reconciliation) is built and tested. The execution gateway and reconciliation share one canonical path (`execution/fill_attribution.py`) that creates every local `Fill`/`SettlementEvent` from Alpaca's real, validated per-fill activity ID -- never a locally-synthesized one -- whether attributed live during polling or recovered later by reconciliation for an order the live poll window already gave up on. Known deferred items: session start/stop/status as the sole authority for the trading session (a missing session row correctly fails closed to disabled, but there's no normal operator command to enable one), equity market-hours enforcement via Alpaca's own clock endpoint (currently relies on cron scheduling), and wiring in the deterministic market-regime classifier once an intraday candle source exists (its volatility math currently assumes 5-minute bars; this codebase only fetches daily ones). See `docs/` for the full audit history.
