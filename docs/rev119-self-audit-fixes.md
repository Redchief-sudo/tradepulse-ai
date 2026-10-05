# Rev.119: self-audit fixes

Rev.119 follows Rev.118 (`3dec7394`) on the `rev113-options-liquidity` branch. An audit of the whole branch before merge found seven gaps; this revision closes all of them. Protected source changed, so both soaks must be run again from Rev.119.

## 1. Reconciliation alert storm

**Finding.** Rev.115 gave a plain `tradepulse run` a reconcile lane every 60 s; before, it reconciled once at startup. Every pass re-sent its critical alerts. A drift that persists until an operator acts (accounting drift, a missed fill, an ambiguous broker order id) sent about 1,440 critical alerts a day per subject, and an Alpaca outage sent one a minute. The legacy database's GOOGL, SPY and IWM drift would have flooded Telegram as soon as the service started.

**Change.** `_alert_once` in `tradepulse/reconciliation/coordinator.py` keys each alert by a deterministic audit id: once per subject per UTC day for drift, missed fills and ambiguous order ids; once per kind per UTC hour for broker-outage alerts. This is the same pattern Rev.118 uses for stranded intents. Every pass still writes its reconciliation record, and latches are unchanged.

**Validation.** `test_persistent_accounting_drift_alerts_once_per_utc_day` (4 passes over 2 days: 2 alerts, 4 drift records) and `test_broker_outage_alerts_once_per_utc_hour` (2 alerts across 2 hours). Both failed with the old code (4 alerts each).

## 2. Service pointed at the legacy database

**Finding.** `deploy/tradepulse-run.service` read the database from `.env`, which named `tradepulse.db`: the FINANCIAL_INTEGRITY_BLOCKED legacy database preserved as evidence.

**Change.** The unit's `ExecStartPre` refuses to start unless `.env` contains exactly `TRADEPULSE_DATABASE_URL=sqlite:///tradepulse-service.db`. The service and operator commands (`tradepulse start/stop/status/reconcile`) therefore always share one database. The runbook documents this and the requirement to start with a flat paper account, because a fresh database treats any existing broker position as accounting drift.

## 3. Soak service check failed open

**Finding.** `_service_active()` treated any non-zero `systemctl` exit as "inactive", so an unreachable user bus or a unit still starting allowed a soak to run alongside the service: the two-writer condition behind the September integrity lock.

**Change.** Only systemctl's own `inactive` or `failed` answer counts as stopped. Anything else is `unverifiable` and the preflight refuses with `SUPERVISED_SERVICE_UNVERIFIABLE`. A missing `systemctl` binary still proceeds with `systemctl_unavailable` recorded, since no supervised service can exist without systemd.

**Validation.** The mapping test covers `active`, `inactive`, `failed`, a bus error (exit 1, no output), `activating`, `deactivating` and exit 4 with no output. A new preflight test proves the refusal happens before any broker is built. On this host the real check returns `inactive` (exit 4, unit not installed).

## 4. Rev.113 note overstated the limit change

**Finding.** It said 3 of the 7 observed option rejections would pass the new 2.5% limit. Only 1.87% and 1.93% do.

**Change.** Corrected to 2 of 7.

## 5. "Flaky under load" tests

**Finding.** Four tests had failed intermittently during the branch's work: the two concurrency tests named in the Rev.113 note, `test_actual_empty_reports_are_preserved_hashable_and_never_pass` and `test_run_trading_supervisor_lane_failure_is_isolated_and_recorded`. Running them in 24 parallel pytest processes reproduced the soak test failure in 70 of 72 runs. With a separate `--basetemp` per process, all 72 runs passed. The cause was concurrent pytest runs (parallel review agents) sharing `/tmp/pytest-of-<user>` and its rotating numbered directories, so one run's SQLite files were changed by another. The code under test was not at fault.

**Change.**
- `python_tests/conftest.py` gives every pytest process its own temp root and removes it afterwards.
- The CLI supervisor tests' 2-second wall-clock limits are now 30-second hang guards. A passing test still returns as soon as its condition holds.

**Validation.** The original 24-process scenario, without `--basetemp`, now passes 72 of 72, with no temp directories left behind.

## 6. Option selection gave up on an opening-inventory strike

**Finding.** When the chosen contract was held at the opening checkpoint, the scanner rejected the whole candidate even when a neighbouring strike was eligible.

**Change.** Opening-inventory contracts are dropped before quoting. The candidate is rejected with `OPENING_INVENTORY_INSTRUMENT` only when every candidate strike is held.

**Validation.** `test_options_scan_trades_a_neighbour_when_the_target_strike_is_opening_inventory` failed before the change and passes after it.

## 7. Option quotes and API load

**Finding.** Rev.113 quoted each of up to five candidate strikes in its own request, and nothing in the soak evidence measured API load against Alpaca's per-account limit.

**Change.**
- `AlpacaClient.get_latest_option_quotes` and `AlpacaMarketDataProvider.fetch_option_quotes` quote all candidate strikes in one request (the endpoint takes a comma-separated list). Each quote is validated exactly as `fetch_quote` validates it, through the shared `_market_quote`.
- The client counts 429 responses (`rate_limited_responses`).
- Each verification reconcile tick's `verification_broker_clock` event records `rate_limit_limit`, `rate_limit_remaining` and `rate_limited_responses`.
- The soak report adds `analysis.broker_rate_limit`: samples, limit, minimum remaining and 429 count. It is reported as evidence, not as an invariant.

**Validation.** Tests cover the batched client request, the 429 counter, the reconcile-tick fields and the soak summary. The scanner test above asserts that a single quote request covers all remaining strikes.
