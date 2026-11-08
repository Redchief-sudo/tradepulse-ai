# Rev.152 — remediation of the Rev.151 forensic audit

**Audit:** "TradePulse Rev.151 — Forensic Code and Financial Integrity Audit" (2026-11-06), against `vegabond-tradepulse(rev.151).zip` (main `24291c40`).
**Branch:** `rev152-audit-fixes`, from main `24291c40`.
**Scope:** F151-01 (fixed), F151-02 (fixed), F151-03 (a conservation proof added, plus the existing proof chain stated), F151-04 (triaged; the cleanup is an operator decision, see below). Not merged without review and the operator's approval.

## Status

| Finding | Audit class | Status |
|---|---|---|
| F151-01 zero-weight fee split | confirmed edge-case defect | fixed; two further defects of the same guard found and fixed |
| F151-02 dependency mismatch blocks testing | environment | fixed: a lock file, verified by a fresh install and a full run |
| F151-03 fee attribution exactly once | unverified risk | a runtime conservation proof added; the existing exactly-once chain is stated below; one coverage gap named |
| F151-04 lint baseline not clean | maintainability | triaged; cleanup proposed as its own Rev |

## Rulings

1. **F151-01, the split's domain.** `adoption/accounting.py::_split` now refuses a non-finite amount (`FEE_SPLIT_AMOUNT_INVALID`) and an empty, zero, negative or non-finite weight (`FEE_SPLIT_WEIGHTS_INVALID`), as `FeeSplitError`, a `ValueError`. Run on the old code, the audit's inputs show three defects, not one:
   - weights 0 and 0, or amount 0.00 with weight 0: `decimal.InvalidOperation` (the audit's case);
   - **no weights:** returned `{}`, so the whole amount was silently dropped;
   - **a negative weight** (2 and −1, amount $1.00): returned +$2.00 and −$1.00, a wrong-signed split.

   None of these is reachable through stored fills, because `Fill.quantity` is validated positive and a group exists only when it has a fill. The guard makes the function safe on its own, rather than relying on its caller. A property test (hypothesis) proves exact conservation and sign for every valid amount and weight set within the tested ranges.
2. **F151-02, the environment.** The suite needs the exact pinned `exchange_calendars==4.13.2`: the calendar refuses any other version by design (Rev.144, because the frozen calendar fingerprint depends on it). The reviewer's environment had 4.13.1 and no `respx`, so it had not installed the project's dependencies. `requirements-lock.txt` now records the exact environment the suite passes in (Python 3.12.3). The README gives the three install commands. Verified by creating a new virtualenv from the lock file alone: `pip check` was clean, then the full suite, mypy and ruff were run in it (see G3).
3. **F151-03, exactly once.** The audit is right that `closing_fee_allocation` does not itself prove that a fill's own fee and a broker receipt are distinct charges. They are distinct by contract, and an existing check would catch a breach. The proof chain:
   - **Two sources:**
     - `Fill.fees` is a fee field embedded in the FILL activity (`execution/fill_attribution.py::_activity_costs`). Alpaca's equity FILL omits it, and it is then recorded as `unavailable` with a value of 0.
     - A receipt is a separate FEE or CFEE activity (`reconciliation/generation_fees.py::cash_fee_receipt`).
     - The simulator, built from the measured Alpaca contract, never puts the same charge in both. Rev.142 and its tests (T15, T18, and the late-fee correction test) deliberately treat both as additive, for example a commission plus a regulatory fee. So a guard that refused both on one fill would be wrong.
   - **Each receipt once, per activity:** the record ID is `generation_fee:<activity_id>`, and the database key makes it unique. A replayed activity with identical content is a no-op, and changed content is refused (`GENERATION_FEE_RECEIPT_OR_CASH_MISMATCH`). A duplicate inside one batch is refused (`GENERATION_FEE_DUPLICATE_ACTIVITY`). Existing tests cover this: three persistence passes give one record and one cash entry, and changed content fails closed.
   - **Each receipt once, per allocation (new):** the allocation is recomputed from immutable rows on every call and never accumulated. Rev.152 makes it prove its own result. Each split must sum to its receipt (`FEE_SPLIT_NOT_CONSERVED: <record>`), and the total allocated must equal the fills' own fees plus each applied receipt, once (`FEE_ALLOCATION_NOT_CONSERVED`).
   - **Against the broker:** if the broker ever reported one charge both ways, the ledger would carry it twice (`fill:cash:` = notional − `fill.fees`, plus `broker:fee:<id>`), while broker cash carries it once. The generation cash check (`valuation.py::_generation_invariants`, expected = opening + Σ ledger) would then be a `mismatch` by exactly that amount. It could not pass silently.
   - **Coverage gap (named, not fixed here):** no test asserts the result of the generation cash or equity check in a bound generation. The tests that call `marked_snapshot` run without one. This is the audit's "cash and equity conservation not independently established". It is the subject of the proposed conservation-test Rev (below), not of a one-line fix.
4. **F151-04, lint.** 1,750 findings at `24291c40`:
   - **1,633 FURB157:** `Decimal("0")` should be written `Decimal(0)`. Cosmetic, auto-fixable, and value-identical.
   - **56 RUF100:** suppression comments that no longer suppress anything.
   - **About 60 others.** The safety-relevant ones are:
     - 7 bare `except Exception` blocks in production code without a stated reason (`alerts/telegram.py`, `reconciliation/coordinator.py` ×3, `settlement/repair.py`, `verification/commands.py`, `verification/service.py`);
     - `TRY004` in `persistence/codec.py`;
     - `PLW1510` in a test;
     - 3 naive `datetime` calls (`DTZ001`), all in tests, and apparently deliberate.

   The project gate so far has been "no new findings per file against the baseline". Reaching a clean result is a mechanical sweep over many money-handling files, plus a review of each of the 7 blind catches. It is proposed as its own Rev, so that it does not mix with behaviour changes.

## Tests added

`python_tests/test_rev152_fee_split.py`:
- invalid weight sets (zeros, a zero amount with zero weight, empty, one zero, a negative) each refused with `FEE_SPLIT_WEIGHTS_INVALID`; NaN and ±Infinity refused with `FEE_SPLIT_AMOUNT_INVALID`. Each failed on the old code: an `InvalidOperation`, a dropped amount, or a wrong-signed split;
- the audit's correct cases stay correct (0.001 over two weights, a zero amount, a negative fee split by quantity);
- property: for any finite amount (6 places, up to ±1,000,000) and 1–8 positive weights, the parts sum to the amount exactly, and no part has the wrong sign;
- a split that loses a cent (simulated) is refused with `FEE_SPLIT_NOT_CONSERVED: generation_fee:f1`;
- repeated allocation gives identical figures, and a receipt for another order is never applied.

## G3

Run in a new virtualenv created from `requirements-lock.txt` alone (Python 3.12.3, `pip check` clean). The evidence is in `~/tradepulse-evidence/2026-11-06-rev152-g3/`.
- **pytest:** 2321 passed, 10 deselected. That is the 2309 at `b010090d` plus the 12 new tests.
- **mypy:** clean, 133 files.
- **ruff:** the first run found 1754, which is 4 FURB157 findings in the new test file. They were fixed with `ruff --fix` on that file only, and its 12 tests were run again (passed). After the fix: 1750, the unchanged baseline. `tradepulse/adoption/accounting.py` has no findings.

## Proposed next Revs (operator decision)

- **Conservation tests:** the audit's highest-value next phase. Bound-generation tests of the cash and equity bridges across partial fills, fees (both sources), late fees, retries, restarts and reconciliation. They include the double-reported-fee case, which must end in a `mismatch`, and a crash at each persisted transition.
- **Lint to zero:** the FURB157 and RUF100 sweep as one mechanical commit, verified by the full suite; then the about 60 manual findings, each of the 7 blind catches reviewed and either narrowed or documented, so that the gate becomes a clean `ruff check`.
