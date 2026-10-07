# Rev.123: SUBMISSION_UNKNOWN recovery and the blocker report

Rev.123 follows Rev.122 (`b0564522`). An external review recommended checking the current databases and the recovery scenarios before freezing source, and making every blocked state explainable. Protected source changed, so both soaks must be run from Rev.123.

## Database inspection (read-only)

| Database | Session | Integrity holds | Blocking intents | Blank or `"None"` order ids |
|---|---|---|---|---|
| `tradepulse.db` (legacy evidence) | FINANCIAL_INTEGRITY_BLOCKED | 0 | 0 | 0 |
| Four soak databases (2026-09-25 to 2026-10-01) | active / market closed | 0 | 0 | 0 |

The paper account (read-only) holds one IWM 2026-10-30 286 call left by the 2026-09-29 soak, and no open orders. `tradepulse-service.db` does not exist yet.

## 1. SUBMISSION_UNKNOWN intents were never retried

**Finding.** A submission whose outcome is ambiguous, and whose own client-order-id lookup also fails (a network blip, say), becomes SUBMISSION_UNKNOWN with no broker order id. In-flight recovery needs an order id; the stranded sweep covered only RISK_APPROVED and SUBMITTED. The gateway retries only when the same idempotency key runs again, which a new scan never does. One temporary failure therefore blocked the asset, protective exits included, until an operator noticed.

**Change.** The stranded sweep covers SUBMISSION_UNKNOWN too. Each reconciliation pass, after the 120 s grace, it repeats the client-order-id lookup:
- adopts the order when `order_matches_intent` proves it;
- closes the intent as REJECTED (`BROKER_NEVER_RECEIVED`) on Alpaca's definitive not-found;
- leaves it unchanged when the lookup fails again;
- raises the once-a-day unresolved alert when identity cannot be proven.

The gateway's alert text no longer says the state needs manual review.

**Validation.** Three new stranded-sweep tests: adopt a proven order, close on not-found, and stay unchanged on a failed lookup. The first two failed on Rev.122.

## 2. Recovery scenario coverage

| Scenario | Coverage |
|---|---|
| Crash before broker submission | `test_resume_after_crash_before_broker_submission`, stranded sweep not-found close |
| Crash after Alpaca accepted, before the id was saved | stranded sweep adoption tests |
| Crash between acceptance and fill polling | `test_resume_after_crash_between_acceptance_and_fill_polling`, `test_known_accepted_order_recovers_fill_older_than_daily_lookback` |
| Retry never resubmits | `test_terminal_intent_resumes_idempotently_without_resubmitting`, `test_second_call_while_submission_unknown_retries_recovery_without_resubmitting` |
| Delayed fills | the late-fill recovery tests, and the gateway's fill-poll timeout leaving the intent pending |
| Partial cancellation | `test_canceled_partial_fill_is_finalized_and_no_longer_blocks_the_symbol` (Rev.120) |
| Failed lookups | the stranded lookup-error tests, plus the Rev.123 additions in this table |
| Delayed fees | `test_accounting_epochs.py`, `test_durable_accounting.py`, `test_generation_membership.py` (fee_pending) |

Rev.123 adds two cases:
- `test_inflight_order_survives_a_failed_lookup_and_finalizes_on_broker_evidence`: a known order whose lookup fails stays in flight, then finalizes when Alpaca reports it filled. It already passed on Rev.122.
- The failed-lookup case for SUBMISSION_UNKNOWN, from section 1.

## 3. Blocker report

**Finding.** `tradepulse status` logged only the session state and latch reason. Nothing listed integrity holds, unfinished accounting epochs, in-flight or stranded intents, when each was last checked, or how it clears.

**Change.** `tradepulse/blockers.py` builds a read-only report from the database. `tradepulse status` prints it, `GET /api/blockers` serves it, and the dashboard shows it in a Blockers panel.

Each entry gives:
- the kind and the affected asset;
- the cause;
- what it blocks;
- since when;
- when reconciliation last checked it;
- whether it clears automatically or needs the operator, and how to resolve it.

On the legacy database it reports the integrity latch and four equity epochs whose finalization fails with `CHECKPOINT_POSITION_QUANTITY_MISMATCH`, which is what keeps that database locked. The runbook has a new section 2a.

**Validation.**
- `test_blockers.py` covers each kind, its automatic or operator classification, its last-checked time and the status output.
- `test_web_app.py` covers the endpoint.
- `BlockersPanel.test.tsx` covers the panel.
- The frontend builds and its 31 tests pass.
