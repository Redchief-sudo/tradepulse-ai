# Rev.117: verification, operations and consistency

Rev.117 follows Rev.116.

## F8: soak preflight, account binding, per-report evidence

**Scope.** Alpaca exposes no fee-finality signal. The accounting policy (`reconciliation/activity_cursor.py` docstring) holds that "neither a wall-clock delay nor an ID cutoff establishes fee finality." The preflight is therefore a **preliminary screen**: it checks necessary conditions using explicit per-day fee evidence, and refuses when any is missing. Generation membership's fail-closed `unresolved_generation_membership` latch remains the **sufficient** backstop for any fee the preflight cannot see.

Changes:
- The soak runner runs a preflight from the effective paper configuration and writes `<report stem>.preflight.json` beside the report, an immutable record of its checks. Evidence is named per report, so a second soak can share the directory, and a refused run must be retried with a new report path.
- Refusals: `OPEN_BROKER_ORDERS`, `PRE_GENERATION_TRADE_TODAY`, `FEE_DAY_NOT_CLOSED:<day>`, `FEE_EVIDENCE_MISSING:<day>:<REG|OCC>`, `ACKNOWLEDGEMENT_INVALID:<value>`.
- `--acknowledge-fee-day YYYYMMDD` (repeatable) waives only a missing fee subtype for a validated past day within the 10-day window.
- The frozen opening checkpoint must match the preflight's account (`soak_opening_account_mismatch`).

## F7: deterministic concurrency tests

- The portfolio-lock concurrency test uses an asyncio barrier inside the lock: two real concurrent executions, exactly one submission.
- The 1100-row backlog test seeds sequentially.

This removes the load-dependent failures noted in Rev.113.

## Operations: paper-only, resume-safe supervised runtime

- `deploy/tradepulse-run.service` is a systemd user unit that runs `tradepulse run --no-browser --resume` with `TRADEPULSE_EXECUTION_MODE=paper` and `TRADEPULSE_LIVE_TRADING_ENABLED=false` in the process environment. It restarts `on-failure`.
- `_load_dotenv` already uses `environ.setdefault`, so `.env` cannot override the unit's environment. No production code changed.
- `--resume` (Rev.115) continues only an already-active session, so a restart never re-activates a stopped, risk-stopped or integrity-blocked one. Scan lanes idle and re-check every 30 s; `tradepulse start` resumes scanning without a restart.
- `docs/operations-runbook.md` covers the host, restart semantics, starting and stopping a soak, secrets and repository hygiene. After deploying Rev.116's dashboard guard, rebuild the frontend.

## Validation

Full Python suite passed. New coverage (`python_tests/test_service_unit.py`):
- the unit enforces paper mode, live trading disabled, `run --no-browser --resume` and `Restart=on-failure`
- process environment wins over a `.env` that asks for live trading

Task 8 and Task 9 tests cover the preflight and the deterministic concurrency changes. Restart behaviour after a stop, risk stop and integrity hold is covered by the Rev.115 `_should_activate`, scan-idle and resume-after-`start` tests. Because protected source changed in this series, soaks must be run again from Rev.117.
