# Rev.149 — runtime remediation of the 2026-11-04 forensic audit

**Audit:** `TradePulse_Rev147_Forensic_Audit.pdf` (2026-11-04), against `vegabond-tradepulse(rev.147-wind-down-spec-rev4).zip` (sha256 `707e6133…`, main `05dfa330`).
**Branch:** `rev149-runtime-fixes` (worktree `~/tradepulse-ai-rev149`), from main `05dfa330`.
**Scope:** the six runtime findings F01–F06, in the audit's order: step 1 is F01 and F04, step 2 is F02, F03, F05 and F06. The Rev.147 design findings D01–D05 are a spec amendment (Rev.147 rev. 5), not part of this branch.
**Method:** for each finding, first a test of the *desired* behaviour that fails on the original code, then the fix at its root cause. The audit's own probes assert the defective behaviour, so they are not copied in. Its acceptance tests are the basis of the new tests.

## Status

| Finding | Severity | Status | Commit |
|---|---|---|---|
| F01 unchanged ratchet suppresses exit checks | CRITICAL | fixed | `4e2b601e` |
| F04 one bad holding blocks unrelated protection | HIGH | fixed | (this commit) |
| F02 open orders are one default page | HIGH | fixed (gate G-F02 open) | (this commit) |
| F03 pending buys leave count limits unreserved | HIGH | not started | |
| F05 quote age measured from a stale `now` | MEDIUM | not started | |
| F06 broker-only inventory missing from account risk | HIGH | not started | |

## Rulings and deviations

1. **F01, where the fix lives.** `RecordRepository.mutate` returns `None` both for a missing row and for a no-op decision. Only `_ratchet_stop` read that `None` as "row gone" (the scale-out `_mark` deliberately treats "already scaled out" like "gone", which is safe). The fix is in `_ratchet_stop`: it keeps the row its own transaction read, and returns it when the stop is unchanged. `mutate`'s contract is unchanged, so its other callers (`_fold_lot_extrema`, `_mark`/`_unmark`, settlement) are untouched.
2. **F01, an existing test depended on the defect.** `test_break_even_and_trailing_never_reverse_on_a_pullback` put its mark (151) below its own stop (152), and passed only because the exit check was skipped. With the fix, the position correctly tries to exit. Its mark is now 153: still a pullback that must not loosen the stop, but no longer a breach.
3. **F04 reaches into the gateway.** Isolating positions in the monitor was not enough. Every exit went through `ExecutionGateway.execute_intent`, which built a portfolio snapshot that hydrates *every* holding (`risk/engine.py::build_portfolio_snapshot`), so one damaged holding still blocked every other exit. `evaluate_risk` reads no portfolio figure for a sell (protective or plain): it returns first. The gateway now builds the snapshot only when it adds exposure (`needs_portfolio_lock`: a non-protective buy), and `evaluate_risk` raises `RISK_SNAPSHOT_REQUIRED` for new exposure without one. A buy still builds the snapshot and still fails closed on damaged holding evidence.
4. **F04, account-wide versus instrument failures.** `DatabaseError` (which `AsyncSQLiteDatabase.run` raises for every `sqlite3.Error`) and `RepositoryLeaseLostError` still propagate and end the cycle, so the lane supervisor alerts and restarts as before. Any other exception while protecting one position becomes a durable critical `monitor_position_protection_failed` audit event (one per asset per hour, retried every cycle). The cycle reports `degraded` with `POSITION_PROTECTION_FAILED: <symbols>`, and the loop moves on to the next position.
5. **F04, damaged lots.** The lot pre-pass hydrates each lot on its own. If a damaged lot's `asset` field still decodes (new public `persistence.decode_asset_identity`), that asset fails closed: no order is built for it (`PositionEvidenceDamaged`), and an incident is recorded. If even the asset is unreadable, the lot gets its own incident (`lot:<record_id>`) and the cycle is `degraded`. Other positions are still evaluated, and the gateway's Rev.140 sell gate re-reads lots before any sell.

6. **F02, the pager.** `AlpacaClient.get_open_orders` now pages: `status=open`, `direction=asc`, `limit=500` (`OPEN_ORDERS_PAGE_LIMIT`). After a full page, it restarts 1 µs before that page's latest `submitted_at` and de-duplicates by order ID, which is the Rev.139 census algorithm (`scripts/a0_order_census.py::fetch_orders`). It raises `AlpacaDataIntegrityError` in each of these cases, and the gateway already maps that error to `BROKER_OPEN_ORDERS_UNAVAILABLE`:
   - a page larger than the limit;
   - a full page that brings no new ID (saturation);
   - an order without an ID;
   - an order without `submitted_at` on a full page;
   - a continuation row older than `after`.

   Results stay newest first, as Alpaca's old default and the simulator return them. The simulator's `get_open_orders` already returns the whole population, so it needed no change.
7. **Gate G-F02 (before merge, read-only, needs the operator's go-ahead for paper GETs).** The 2026-10-26 measurement covered `status=all`. Re-measure on `status=open`:
   - that `direction=asc` with no `after` starts from the oldest open order;
   - that `after` is inclusive at microsecond precision;
   - that `limit=500` is accepted.

   The account is flat, so the measurement shows the request shape and the empty result. The paging itself is covered by the tests.

## Tests added

`python_tests/test_rev149_monitor_protection.py`:
- **F01:** with an unchanged stop, an exit at break-even, at a trailing stop, at the target, at the time stop, at a stall, and at option expiry. All six failed on the original code with `status="ok"` and zero exits. A holding deleted during the cycle (the control) gets no exit.
- **F04:**
  - a malformed holding, a malformed lot, and an injected gateway failure for AAPL each still let a later MSFT exit in the same cycle, with no AAPL order, a critical incident for AAPL, and `degraded` status. The symbol reservation is released after the failure;
  - a `DatabaseError` still fails the cycle;
  - `evaluate_risk` approves a protective exit without a snapshot, and refuses new exposure without one.
- **F02** (`python_tests/test_rev149_open_orders.py`, against a model of Alpaca's paging, where a request with no `limit` gets 50 rows), with all eight failing on the original client:
  - 51 orders, with the oldest a foreign sell;
  - 1203 orders across three pages;
  - six equal timestamps straddling the 500th row;
  - a saturated page (501 orders on one timestamp);
  - an unstamped order on a full page;
  - an oversized page;
  - the population changing mid-scan;
  - a continuation row outside the request.
