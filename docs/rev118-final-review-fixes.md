# Rev.118: final-review fixes

Rev.118 follows Rev.117 (`5b261f68`) on the `rev113-options-liquidity` branch. A whole-branch review approved the branch with fixes; this revision applies them.

## 1. README and cli docs contradicted Rev.115

**Finding.** `README.md` and the `tradepulse/cli.py` module docstring still said a refused activation starts no tasks, that `run` has five tasks, that reconciliation is cron-only and that a crashed lane stops itself. A code comment still tied the cadences to the README crontab; the monitor is now 30 s.

**Change.** Both now state the truth: six lanes always start; scan lanes idle (no AI call, no market data, no scan record) until the session is `ACTIVE` or `MARKET_CLOSED` with trading active, re-checking every 30 s, so `tradepulse start` resumes scanning without a restart; cadences come from `tradepulse/config/lanes.py` (monitor 30 s, settle 60 s, reconcile 60 s); a crashed lane is restarted by `_supervised_lane` with capped backoff; `run --resume` never re-activates a stopped or latched session. The crontab example is marked as a one-shot alternative to `run`.

**Validation.** Docs only; `test_cli.py` passes.

## 2. Runner fixture leaked the real `.env`

**Finding.** `run()` in `scripts/run_accounting_soak.py` calls `_load_dotenv()` relative to the cwd. Run from the real checkout, the pipeline test injected real credentials into `os.environ` for every later test.

**Change.** The `runner` fixture neutralizes `_load_dotenv` (and, with finding 4, `_service_active`).

**Validation.** `test_runner_fixture_never_loads_a_dotenv_into_the_pytest_environment` writes a `.env` with a sentinel into the cwd and asserts it never reaches `os.environ`. It failed (sentinel leaked) with the neutralization removed and passes with it.

## 3. Unprovable stranded intents were silent and noisy

**Finding.** An intent that cannot be resolved (`ACCOUNT_IDENTITY_UNPROVEN`, including every pre-Rev.115 intent; `STRANDED_ORDER_IDENTITY_MISMATCH`; `STRANDED_INTENT_UNDER_INTEGRITY_HOLD`) stays forever, blocks its asset for every order including protective exits, and wrote a drift record every 60 s with no alert.

**Change.** For those reasons `_resolve_stranded` creates a deterministic audit event `stranded_intent_unresolved:<trade_intent_id>:<UTC date>` with `create_once`. Only when it is newly created does it write the drift record and send one critical alert (the asset is blocked for all orders including protective exits; manual resolution required). Transient lookup errors and commit refusals keep per-pass drift and get no alert. No finality condition, lease fence or `_commit_stranded` logic changed.

**Validation.** Three new tests in `test_stranded_intents.py`: same-day double sweep gives one alert, one audit event, one drift record; a later UTC day alerts again; a transient lookup error writes drift each pass with no alert. The first two failed before the change and pass after.

## 4. Soak/service mutual exclusion was only a runbook step

**Finding.** The soak preflight did not check whether `tradepulse-run` was running against the same paper account, which recreates the two-writer integrity lock.

**Change.** `_broker_preflight` first awaits `_service_active()` (`systemctl --user is-active --quiet tradepulse-run`). Active: the evidence file records `SUPERVISED_SERVICE_ACTIVE` and the run is refused with `soak_preflight_refused:SUPERVISED_SERVICE_ACTIVE` before any broker is built. `systemctl` missing is treated as not active and recorded as `"service_check": "systemctl_unavailable"`. The runbook's soak section documents the enforcement.

**Validation.** New tests for active (refused, code in evidence, no broker built), inactive (proceeds), systemctl missing (proceeds with note) and the real `_service_active` mapping against a faked subprocess. Tests never run systemctl.

## 5. Stale comments

- `verification/soak.py` `_lane_evidence` now references `LANE_MAX_GAP_SECONDS`.
- `reconciliation/coordinator.py` `unresolved` now says the fence suppresses evidence once tripped, and that a commit refused with `STRANDED_RESERVATION_LOST` before the fence trips still records drift.

## Operator steps at deploy

1. Rebuild the frontend: `cd frontend && npm run build`.
2. Query the main database for `risk_approved` or `submitted` intents without a `broker_order_id` and resolve them before enabling the service.
3. Run `tradepulse reconcile` once by hand before enabling the service.
4. Expect one `UNMANAGED_POSITION` alert per day per broker position without a local holding, and one `stranded_intent_unresolved` alert per day per unprovable stranded intent.
5. Re-run both soaks from Rev.118 with the service stopped (the runner now refuses otherwise).
