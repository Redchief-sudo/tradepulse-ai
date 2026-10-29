# Rev.143 — Dividend cash recognition and verified attribution

**Spec:** `docs/superpowers/specs/2026-10-27-dividend-recognition-design.md` (rev. 5, plus the G0 results in §3.8)
**Plan:** `docs/superpowers/plans/2026-10-29-rev143-dividends.md`
**Branch:** `rev143`, stacked on Rev.144.
**Commits:**
- `a8906551`: G0 and plan;
- `4386814c`: task 1;
- `52c86a3e`: task 2;
- `acc02dfe`: tasks 3–4;
- this record.

Until now, a dividend paid during a verification run left the generation cash check mismatched for the rest of the run. With Rev.143, every in-run `DIV*` activity posts its own cash exactly once, so the cash check balances.

Attribution is separate. A dividend is placed with the strategy, excluded as inherited, or held as unresolved, and only on verified evidence.

## G0 (read-only, 2026-10-29)

Evidence: `~/tradepulse-evidence/2026-10-29-rev143-g0/` (manifest SHA-256 prefix `956dd0fec91531a9`).

- **No dividend has ever posted to the paper account.** That has three consequences:
  - `DIVIDEND_REPRESENTATION = "unknown"`, so every dividend's cash is recorded and its attribution is refused (`DIVIDEND_REPRESENTATION_UNKNOWN`);
  - the receipt shape is Alpaca's documented `NonTradeActivities` schema;
  - the first real dividend is the verification event that confirms both.
- **Announcement fields** are recorded:
  - `cash` is a decimal string, and `ex_date = record_date` in every sample;
  - the query window is not an ex-date filter, so candidates are filtered locally.
- **FILL activities have no trade-date field.** The trade date is the New York date of `transaction_time`.
- **The market-data corporate-actions endpoint works, but is not adopted.** Its rates are JSON numbers.

## What changed

| Spec | Change |
|---|---|
| §3.1–3.3 | `reconciliation/dividends.py`. Receipt validation (`DIVIDEND_RECEIPT_INVALID:{id}`). Each eligible activity writes one cash entry `broker:dividend:{id}`, one `generation_dividend:{id}` record, and a status row, inside the fee and adjustment transaction. Replays are re-validated, valuation re-validates every dividend, and the totals gain `dividend_income`. |
| §3.4 step 1 | `BrokerPort.get_dividend_announcements` for Alpaca and the simulator. Versions are stored append-only with their SHA-256 (`reconciliation/announcements.py`). |
| §3.4 steps 1–6, §3.5 | `reconciliation/dividend_attribution.py`. `decide` is pure: candidates, trade dates, S, E1, the three statuses, the representation checks and the largest-remainder split. `mirror` handles reversals. |
| §3.4 step 7 | `attribute_dividends`, each reconciliation cycle: Rev.144's `session_deadline`, the once-per-activity alerts, and announcements refreshed at most once per NY day. |
| §3.7 | Generation equity subtracts `D_excl` and `D_unresolved`. Open dividends set its status to `unresolved_dividend_attribution`, which now fails the mandatory invariants. `dividend_entitlement_not_established` is reported. |
| §3.10 | `corporate_action_announcements` and `dividend_attributions` are append-only, with verified triggers; `dividend_attribution_status` is mutable. |

## Behaviour changes

- **In a verification run, a dividend no longer breaks the cash check.**
- **Until the first real dividend confirms Alpaca's representation, every dividend is `unresolved`.** It is held in `D_unresolved`, so the snapshot is `INCOMPLETE_EVIDENCE` with a named reason, and a critical alert fires at the third session's close. That is the designed fail-closed behaviour, not a regression.
- **No new network calls happen while the representation is unknown.** Announcements are fetched only once attribution is possible.

## Deviations and interpretations (for the reviewer)

1. **When the escalation deadline is set.** It is computed in the first attribution pass, in the same reconciliation cycle as recording, not inside the recording transaction, because the calendar status read is asynchronous. It is computed once and never moved, so it can never pass an alert already sent (T25).
2. **An unknown representation is `unresolved` immediately,** with reason `DIVIDEND_REPRESENTATION_UNKNOWN`, rather than `pending`. It is alerted at its deadline like any other.
3. **Receipt shape.**
   - Any `activity_sub_type` makes the shape unverified, because a stock dividend is `SDIV` and the cash dividend's sub-type has never been observed. So does a missing or extra field.
   - `currency` and `status` are required, as the spec and Alpaca's documentation say. If the first real dividend lacks either, it is refused exactly as today: no record, cash check mismatched.
4. **Withholding with zero cash** is accepted, for the net representation. That representation's withheld amount is read from `withheld_amount`, a field name to be confirmed at the verification event.
5. **Rounding.** The amount check rounds `qty × rate` to the cent with `ROUND_HALF_UP`. G0 had no samples to measure the rule.
6. **E1 boundary.** The ex-date's trade date is taken to start at 20:00 ET on the day before (the overnight session). The checkpoint must predate that time, and the population must have been observed after it.
7. **Strategy dividend income** is a computed projection (`strategy_dividend_income`), not a new stored `TradeAttribution` field. Stored attribution payloads stay byte-identical, so the Rev.140 replay and digests are unaffected.
8. **Not built:**
   - the dashboard line for dividend income;
   - the `adopted` bucket, which Rev.142 adds once adopted lots exist.
   Telegram and the snapshot carry the information.
9. **T1 and T18** (the cash check and the `INCOMPLETE_EVIDENCE` snapshot) are pinned through the ledger entry, valuation's re-validation, and `dividend_equity`, rather than a full bound-generation valuation fixture.

## Tests

| Tests | File |
|---|---|
| T1–T5, T20, T23 | `test_rev143_recording.py` |
| T6 (announcement versions) and the broker shape | `test_rev143_announcements.py` |
| T6–T12, T16, T17, T19, T27–T34 (decision) | `test_rev143_attribution.py` |
| T4, T13–T15, T17–T19, T21, T22, T24–T26, T31 (pass) | `test_rev143_pass.py` |

## Verification (2026-10-29)

- **pytest:** 1691 passed, 0 failed, 0 skipped. Rev.144 had 1632, so Rev.143 adds 59.
- **mypy:** clean under the project config, and clean on the three new modules checked explicitly. Over the whole package: 117 errors, the same as `main`.
- **Ruff:** the new modules and tests are clean, and the edited files have no new findings.

## Follow-ups

- **The first real dividend is the verification event.** Compare its raw receipt with the fixture shape, establish `DIVIDEND_REPRESENTATION` and the rounding rule, and record the cash dividend's `activity_sub_type`.
- **Rev.142:** the `adopted` bucket in the same structures (spec §3.9).
