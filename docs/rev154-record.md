# Rev.154 — a not-found lookup never rejects a sent order

**Origin:** REV151-02, a delayed order request, from the Rev.151 review of the Rev.147 spec rev. 5. The stranded-intent sweep turned a broker 404 into `REJECTED` 120 s after `created_at`. A request that was only delayed could then become a live broker order that TradePulse believed was never sent.
**Candidate:** `TradePulse_AI_Rev154_review_candidate.zip`, the reviewer's source copy against `3042abe7`. It is applied unchanged as `eaf70789`, without its generated listings (`rev154.patch`, `rev154-changed-files.txt`, `rev154-test-environment.txt`, `rev154-validation.txt`). Its own write-up is `docs/rev154-dispatch-resolution-review.md`.
**Integration review:** the candidate was read in full against `3042abe7`, and its suite was re-run in a fresh environment built from `requirements-lock.txt`: 2,346 passed, mypy clean on 134 files, no new ruff findings. Five findings, R154-01..05, are fixed on this branch. The re-review of the rev.154 zip found a sixth, R154-06, also fixed.
**Branch:** `rev154-dispatch-resolution`, from main `3042abe7`. It is not merged without review and the operator's approval.

## What the candidate does (kept)

- **The sweep (`reconciliation/coordinator.py::_resolve_stranded`):** what a 404 means depends on how far the intent got.
  - An intent that is `RISK_APPROVED` and was never marked sent may still be closed (`STRANDED_BEFORE_SUBMISSION`), under the execution reservation as before.
  - A `SUBMITTED` intent becomes `SUBMISSION_UNKNOWN`. A `SUBMISSION_UNKNOWN` intent stays so. Its asset stays blocked and its frozen pending funding stays reserved.
  - The automatic `BROKER_NEVER_RECEIVED` rejection is removed.
- **The grace period** counts from the local dispatch write.
- **`get_order_history`** on the port, the Alpaca client and the simulator: a complete history of orders of every status, paged with overlapping timestamp boundaries, and refused when completeness cannot be proven.
- **`tradepulse resolve-dispatch <intent> --reason`.** It needs:
  - the account lease and a renewed asset reservation;
  - an unchanged sent intent, with no hold on it;
  - a dispatch at least 15 minutes old, on the same account;
  - not found by client order ID both before and after the history read;
  - a complete order history and activity population with no unexplained order or fill on the asset.

  One transaction writes `REJECTED` (`SUBMISSION_ABSENT_OPERATOR_RESOLVED`), the evidence and a 30-day watch. If the watch fails, the rejection rolls back.
- **The watch:** each reconciliation pass looks up every active watch. An appearance writes an integrity hold (`RESOLVED_DISPATCH_APPEARED`), a durable critical audit event and the financial-integrity latch.

## Findings and fixes

| ID | Severity | Finding | Fix |
|---|---|---|---|
| R154-01 | high | **An appearance locked the account permanently.** `run_reconciliation` returned `degraded` before any other step, on every pass, for ever. The late order's own fills, other assets' stranded intents and all other accounting stopped. `reset-integrity` needs a clean pass, and no command could clear the incident, so only a database edit could. A failed watch lookup also skipped the whole pass. | The appearance or lookup failure is recorded, and the pass continues. It is still `degraded`, and its error always leads with `RESOLVED_DISPATCH_APPEARED` or `DISPATCH_WATCH_UNAVAILABLE`, even when a later step fails. Nothing can adopt the order (see the proof below). **`resolve-dispatch <intent> --reason --acknowledge-appearance`** closes the incident; it is defined in the next section. |
| R154-02 | medium | **No clock-skew margin.** The order history started at the local dispatch time, and fills before it were skipped. If the laptop clock runs ahead of the broker's, the delayed order, or its fill, carries an earlier broker time and falls outside the window. | Both windows start `DISPATCH_EVIDENCE_SKEW` (5 minutes) earlier. The evidence records `window_start`. |
| R154-03 | medium | **The broker's acceptance time was overwritten.** On acceptance, the gateway stored the local dispatch time in `submitted_at`, which fills copy (`fill_attribution.py`), in place of Alpaca's own `submitted_at`. That silently changed the meaning of an existing evidence field. | A new `TradeIntent.dispatched_at` holds the local dispatch time, set by the `SUBMITTED` write. `submitted_at` is the broker's again (`placed.submitted_at`, falling back to the clock, as before Rev.154). The sweep and `resolve-dispatch` read `dispatched_at`. Older rows hydrate with `None` and stay unclearable, as the candidate intended. |
| R154-04 | low | **The alerts were misleading.** Moving `SUBMITTED` to `SUBMISSION_UNKNOWN` sent a *warning* saying the intent was "resolved". An absent `SUBMISSION_UNKNOWN` intent wrote a drift record on every pass, about 1,440 a day. | The transition sends a critical `SUBMISSION_UNKNOWN` alert naming the blocked asset and `resolve-dispatch`. `SUBMISSION_ABSENCE_UNPROVEN` joins the unprovable reasons: one critical alert and one drift record per intent per UTC day. |
| R154-05 | medium (Rev.140) | **A releasable short-exposure hold could never be released.** `run_reconciliation` counted every hold row as `ACTIVE_INTEGRITY_HOLD`. That included a `SHORT_EXPOSURE` hold whose exposure was fully resolved, which only `reset-integrity` releases, in its own transaction. Reset needs that same reconciliation to be clean, so it always refused. Valuation and the unlock proof already used `active_hold_count`, which excludes such holds. | Reconciliation uses `active_hold_count` as well. |
| R154-06 | high (merge blocker, from the re-review of rev.154) | **A stale watch row could recreate an acknowledged hold.** `check_resolved_dispatches` read the active watches, then awaited the broker lookup, then wrote the hold from the row it had read. An acknowledgement that committed during that lookup deleted the hold and closed the watch. The resumed pass then recreated the hold, leaving a closed watch with an active hold that later passes skip, which is a permanent block again. | The hold write re-reads the watch in its own write transaction. A watch acknowledged meanwhile is skipped: no hold, no latch, no alert. A watch whose recorded appeared order differs from the one now seen is refused (`DISPATCH_WATCH_ORDER_CHANGED`). Tested with the reviewer's exact sequence: the acknowledgement runs inside the paused lookup. The test fails on the previous code. |

### The acknowledgement (R154-01)

`resolve-dispatch <intent> --reason "<text>" --acknowledge-appearance`.

**It is refused unless:**
- the account lease, the account and the venue match;
- the watch has appeared and is not yet acknowledged;
- the appeared order is the watched intent's by client order ID;
- the order status is terminal: `filled`, `canceled`, `expired` or `rejected`. `done_for_day` can resume, and `replaced` passes its quantity on, so neither is accepted;
- the order's FILL activities in the complete activity population total its `filled_qty`;
- the order is unchanged on a second read.

**One transaction then:**
1. re-reads the watch;
2. checks that every fill is accounted by exact ID. This is the Rev.140 test: a local Fill, or a quarantine execution. Otherwise it refuses with `APPEARED_FILL_UNACCOUNTED`;
3. deletes the hold;
4. records the acknowledgement and an audit event.

**The session latch stays.** `reset-integrity` then applies all of its own gates.

**What it never does:** submit, cancel or adopt an order.

**Why continuing the pass is safe.** The resolved intent is `REJECTED` with no broker order ID, so the sweep never revisits it and nothing adopts its order. The order's fills have no local intent, so they reach the existing unresolved-execution path, which keeps reset blocked until they are accounted (Rev.140 §3.1a, invariant K1).

## New strings

- **Refusals:**
  - `APPEARED_ORDER_NOT_TERMINAL`, `APPEARED_FILLS_INCOMPLETE`, `APPEARED_FILL_UNACCOUNTED`, `APPEARED_FILL_QUANTITY_INVALID`;
  - `APPEARED_ORDER_IDENTITY_MISMATCH`, `APPEARED_ORDER_CHANGED`;
  - `DISPATCH_NOT_APPEARED`, `DISPATCH_WATCH_NOT_FOUND`, `APPEARANCE_ALREADY_ACKNOWLEDGED`, `DISPATCH_WATCH_CHANGED`, `DISPATCH_WATCH_ORDER_CHANGED`.
- **Other:**
  - the summary error `DISPATCH_WATCH_UNAVAILABLE`;
  - the audit event `resolved_dispatch_acknowledged`;
  - the stranded reason `SUBMISSION_ABSENCE_UNPROVEN`;
  - the flag `--acknowledge-appearance`;
  - the field `TradeIntent.dispatched_at`;
  - the watch columns `acknowledged_at` and `acknowledgement`.

The candidate's own strings are in `docs/rev154-dispatch-resolution-review.md`.

## Tests

`python_tests/test_rev154_integration.py` (22 cases):
- **R154-01:**
  - the pass continues after an appearance, so positions and activities are still reconciled, and the order is not adopted;
  - a later failure never hides the appearance;
  - an unavailable lookup degrades the pass but continues;
  - an acknowledgement is refused for five non-terminal statuses, for missing or unaccounted fills, for a changed order, and with no appearance or no reason;
  - an accounted, terminal appearance is acknowledged: the hold is deleted, the watch is closed for good, and a second acknowledgement is refused;
  - the CLI flag is present.
- **R154-02:** the history starts 5 minutes before the dispatch, and a fill stamped 2 minutes before the local dispatch time still refuses clearance.
- **R154-03:** on the simulator, acceptance keeps the broker's `submitted_at` and the local `dispatched_at` separate, and an old row without `dispatched_at` hydrates.
- **R154-04:** becoming unknown is a critical alert, not a resolution, and an absence that persists is alerted once per UTC day with one drift record.
- **R154-05:** a releasable short-exposure hold no longer degrades reconciliation, and an unreleased one still does.
- **R154-06:** an acknowledgement that commits during the watch's broker lookup is never undone: no hold, and the watch stays closed. A changed appeared order is refused.

Changes to the candidate's tests:
- `_sent` sets `dispatched_at`;
- the late-appearance test accepts a pass that continues, with the appearance reported first;
- the DBAE ambiguous-buy test checks `dispatched_at`.

## G3

Run on the branch head in a fresh virtualenv from `requirements-lock.txt` (Python 3.12.3). The results are in `review-evidence/` in the rev.154 zip.
- **pytest:** 2372 passed, 12 deselected, exit 0, at `5985a798`. That is the candidate's 2346, plus the 22 integration cases, plus the 4 simulator runs of the two conformance tests. The 2 extra deselected items are those tests' Alpaca paper variants, which ran separately (above).
- **mypy:** clean, 134 files.
- **ruff:** no file changed in this Rev gains a finding (each is compared with the candidate commit `eaf70789`).

## Real-broker conformance (Alpaca paper, 2026-11-09)

`python_tests/broker_conformance/test_dispatch_conformance.py`. It runs on both simulators by default, and on the paper account with `-m integration`:
- **`test_history_of_all_statuses_is_complete_with_page_proof`, read-only.**
  - **On paper it passed** over 120 days: 73 orders, all filled, on one page, with the completeness proof holding. The last 7 days had no orders.
  - **Paging across pages** was not exercised on real data, because the history is under the 500-order page limit. The simulator and HTTP-mock tests cover it.
- **`test_order_a_real_order_blocks_dispatch_resolution`, behind the order guard.**
  - **On paper it passed.** It placed one 1-share SPY limit buy at $1.00, cancelled it at once, and the order did not fill:
    - the client-order-ID lookup and the history both list the order as cancelled;
    - `resolve-dispatch` refuses with `BROKER_ORDER_PRESENT` for the order's own intent, and with `UNEXPLAINED_ASSET_ORDER` for an absent SPY dispatch;
    - neither refusal changes local state.
  - **Run by the operator.** Claude Code's permission policy did not allow it to place the order itself. The operator approved the order and ran the test in their own terminal.

The run's evidence is in `review-evidence/paper/` in the rev.154 zip.

**What a real broker cannot show:** an actual delayed request, a crash at an exact point, or a late appearance. These stay on the simulator and the fakes, because Alpaca cannot be made to hold a request on cue.

## Not in this Rev

- **Carrying watches across generations** is Rev.147 work (`watched_client_order_ids`, spec §7.1, §8 step 7). Until it is built, keep the current database while a watch is active.
- **Live (real-money) trading** was not exercised. The paper account was exercised only by the two conformance tests above.
