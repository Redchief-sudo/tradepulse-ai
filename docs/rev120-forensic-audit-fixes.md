# Rev.120: forensic audit fixes

Rev.120 follows Rev.119 (`4378c278`). An external forensic audit of Rev.119 reproduced six defects: three high, two medium, one low. Each was confirmed against the code, given a test that fails on Rev.119, and fixed. Protected source changed, so both soaks must be run again from Rev.120.

## 1. High: an expired portfolio lock allowed overlapping capital decisions

**Finding.** The portfolio-risk lock (30 s lease) serializes the BUY decision window: quote, account, snapshot, risk evaluation, RISK_APPROVED. That window makes broker HTTP calls, and 429 backoff can stretch it past the lease. A second BUY could then take the lapsed lease, read the same exposure, be approved and fill. When the first resumed, it committed an approval made against capital the second had already spent. The audit submitted $2,000 against a $1,000 cap.

**Change.** `RecordRepository.update` accepts `lease=(lock_key, owner_token)` and checks it inside the write's own BEGIN IMMEDIATE transaction. The write raises `RepositoryLeaseLostError`, and commits nothing, unless that lease is still held and unexpired. The gateway fences the RISK_APPROVED write on the portfolio lease. A holder whose lease lapsed closes its intent as REJECTED (`PORTFOLIO_RISK_LOCK_LOST`) before submission.

**Validation.** `test_a_lapsed_portfolio_lease_never_commits_an_overlapping_approval`: the first BUY stalls inside the window and its lease is expired. The second BUY fills; the first is refused. One broker submission. On Rev.119 both filled.

## 2. High: a canceled order with partial fills stayed "in flight"

**Finding.** `terminal_status_for_order` mapped a canceled, expired, rejected or replaced order with partial fills to PARTIALLY_FILLED. That status is the same one a working partial order has, and it is in `IN_FLIGHT_STATUSES`. The intent never left it. It blocked every later order on the symbol, protective exits included, and kept reserving pending exposure and an outstanding-order slot in the risk engine.

**Change.** Terminal broker failure statuses map to their terminal local status (CANCELED, EXPIRED, REJECTED) whether or not part of the order filled. The filled part stays in the intent's Fill records and `filled_quantity`; the gateway still reports `partially_filled` to its caller. `done_for_day` is unchanged, since Alpaca may continue that order. Intents already stuck in PARTIALLY_FILLED are non-terminal, so reconciliation's in-flight recovery revisits them and finalizes them on its next pass.

**Validation.** `test_canceled_partial_fill_is_finalized_and_no_longer_blocks_the_symbol`: a stuck PARTIALLY_FILLED intent whose order was canceled after 3 of 5 shares ends CANCELED, with `filled_quantity` 3 and no in-flight block. It failed on Rev.119. The quantity-aware mapping table now expects CANCELED and EXPIRED for partial fills.

## 3. High: order recovery accepted a mismatched order and blank account numbers

**Finding.** Stranded-intent recovery adopted an order found by client order id when only client id, symbol and side matched; a different quantity or order type was accepted. Its account proof compared account numbers, so two blank strings "matched". The gateway's unknown-submission recovery had the same gap with no identity check at all: it adopted any order the lookup returned.

**Change.**
- `order_matches_intent` (`execution/fill_attribution.py`) requires client order id, symbol, side, order type and quantity (or notional, for notional orders) to equal what the intent submitted. A missing or malformed quantity never matches.
- Both recovery paths use it. A mismatch stays STRANDED_ORDER_IDENTITY_MISMATCH (stranded) or SUBMISSION_UNKNOWN with `BROKER_ORDER_IDENTITY_MISMATCH` (gateway) for an operator, and is never adopted.
- A blank or missing account number on either side is ACCOUNT_IDENTITY_UNPROVEN.

**Validation.**
- The stranded tests add quantity, order type, notional-only and malformed-quantity mismatches, plus blank-account cases.
- `test_unknown_submission_never_adopts_an_order_that_is_not_its_own`: the lookup returns a 50-share order for a 5-share intent; it is never polled or attributed.
- All of these fail on Rev.119.
- The gateway's recovery tests now mock the lookup as Alpaca answers it, echoing client order id, quantity and type.

## 4. Medium: a failed alert used up its window

**Finding.** Rev.119's once-per-window alerts recorded the dedupe audit id before sending. If Telegram was down, the alert was suppressed until the window ended (a day for drift).

**Change.** `alert_once` (`tradepulse/alerts/once.py`) records the id only after Telegram accepts the alert, or when alerting is not configured at all (new `TelegramAlerter.configured`). A failed delivery is retried on the next pass. Reconciliation, stranded-intent and unmanaged-position alerts all use it.

**Validation.** `test_alert_once.py`, and `test_drift_alert_that_fails_to_deliver_is_resent_next_pass` (failed, then delivered, then deduplicated: 2 attempts). It failed on Rev.119 with 1 attempt.

## 5. Medium: the service database guard could pass on the legacy database

**Finding.** The unit's `ExecStartPre` grepped `.env` for the service database line. The runtime, though, uses an exported `TRADEPULSE_DATABASE_URL` first, and otherwise the first matching `.env` line. A legacy line above the service line, or a value in the process environment, passed the grep while the runtime opened `tradepulse.db`.

**Change.** `tradepulse run --require-database URL` compares the database the runtime actually resolved (as an absolute file path) and refuses with exit 78 (`EX_CONFIG`) before opening anything. The unit passes `--require-database sqlite:///tradepulse-service.db`, drops the grep and sets `RestartPreventExitStatus=78`, so systemd does not restart into the same refusal.

**Validation.** `test_run_refuses_a_database_other_than_the_required_one`: a `.env` with the legacy line first is refused with 78 and creates no database. The same file named by an absolute path is accepted.

## 6. Low: rate-limit evidence missed reconciliation's 429s

**Finding.** Rev.119 sampled the runtime client's own counter, but each reconcile tick builds a separate Alpaca client, so its 429s never reached the evidence.

**Change.** Every `AlpacaClient` in the process updates a process-wide snapshot and 429 count, exposed through `process_rate_limit_evidence()`. The verification reconcile tick records those values.

**Validation.** The reconcile-tick test now produces the 429 from a separate client created inside the reconcile step. The recorded count and headroom include it.
