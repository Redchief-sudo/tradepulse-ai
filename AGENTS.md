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

## Code Standard: top tier only

This is a financial execution system. Only top-tier, production-grade work is accepted. Basic, minimal or "good enough" work is rejected, whoever wrote it.

- **Coding agents run on the most capable model available.** That covers builders, reviewers and auditors. Lighter models may do read-only searches, never write or review code.
- **Complete implementations only.** No stubs, placeholders, `TODO`/`FIXME` left behind, "simplest thing that passes", half-handled cases or happy-path-only code. Every error path is handled deliberately and fails closed. Every input the spec implies is covered, including the ones it does not list.
- **A passing test is necessary, not sufficient.** In TDD, "minimal" means no speculative features beyond the spec. It never means low quality, missing error handling or skipped cases.
- **Fix at the root cause.** No `# type: ignore`, `noqa`, `cast`, `pragma: no cover`, asserts used to silence a checker, broad `except` that swallows errors, or skipped/xfail tests. Never weaken, loosen or delete an existing test assertion to make a change pass. If a test's expectation is genuinely wrong, the change is justified in writing and approved like any other decision.
- **Tests exercise the real code paths:** the real composition root, the real CLI and the real database, with `SimulatedBroker` or fakes only at the broker and network boundary. A test that only checks a mock it set up itself is not evidence. A new test must be shown to fail before the change.
- **Gates before every commit:** the full suite passes (exit 0), `mypy` is clean, and no changed file adds a `ruff` finding.
- **Evidence before claims.** Nothing is reported as done, fixed or passing without the command output that shows it. A builder's report is checked, not trusted.

## How Work Is Done

- **Design first:** each change starts from a written spec and plan. Each needs the external reviewer's sign-off before it is built.
- **Builds are task by task:** a fresh builder implements one task from a written brief. An independent reviewer then checks it against the brief before the next task starts. Findings go back to the builder for a fix, followed by a focused re-review. A whole-branch review and a final audit come before any merge.
- **Decisions are recorded:** every decision or deviation made during a build is recorded with its reason, in the task ledger and the Rev record under `docs/`.
- **Separate worktree:** feature work happens in a separate git worktree. Nobody edits `tradepulse/` in the main checkout while a soak or verification run is active.
- **Commits:** GPG-signed, with an attribution trailer for agent work. If signing fails, leave the work staged; never commit unsigned.
- **Merging and pushing:** only with the operator's approval. Before pushing to `main`, check the remote for commits by `base44-builder[bot]`.

## Working Notes

- Run `.venv/bin/python -m pytest python_tests -q` before finishing code changes (use `python -m pytest`, never bare `pytest`, which can import a different checkout).
- Never print or log real API keys/secrets. The IBKR Flex token is never requested, displayed or committed.
- Broker account numbers never appear in git, logs, notes or review packages. Scan every package for them before it is shared.
- Never submit, cancel, or close broker orders or positions outside the TradePulse execution gateway (no direct Alpaca API calls or ad-hoc scripts that trade) without the operator's explicit approval for that specific order. Out-of-band trades never reach `tradepulse.db` and trip the financial-integrity lock. Read-only broker calls also need the operator's go-ahead.
- This system is paper-trading-first; live trading requires `TRADEPULSE_EXECUTION_MODE=live` and `TRADEPULSE_LIVE_TRADING_ENABLED=true` -- treat any change touching that gate as safety-critical.
- `tradepulse dashboard` (and `tradepulse run`, which embeds the same dashboard server) must always bind `127.0.0.1` -- there is no authentication/authorization layer yet, so anything network-reachable would be unauthenticated start/stop/reset-risk/reset-integrity control-plane access. Never add a `--host` flag or any other way to bind elsewhere without also adding real auth first.
- `tradepulse run` is the one deliberate, explicit exception to "no command runs its own internal scheduling loop" (see `cli.py`'s module docstring) -- every other command stays one-shot and cron-external. Its scan/monitor/settlement lanes run as genuinely independent concurrent asyncio tasks (see `_run_trading_supervisor`), never a shared serial loop -- preserve that when touching it.
