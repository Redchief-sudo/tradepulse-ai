# Rev.147 rev. 6 — response to the Rev.151 review of rev. 5

**Spec:** `docs/superpowers/specs/2026-11-01-run-wind-down-design.md` (rev. 6).
**Review answered:** "TradePulse Rev.151 — Rev.147 revision 5 forensic review" (2026-11-07). It scored rev. 5 8.9/10, conditional, with revisions required. Findings REV151-01..03 are answered here, and the reviewer's decisions on open questions 7–9 are recorded.
**Scope:** design only. No production code changed, no broker calls, no orders, no database writes.
**Base:** main `24291c40` (rev. 5). The code facts below were checked at that commit.

## Findings answered

| Finding | Review requirement | Rev. 6 answer | Sections |
|---|---|---|---|
| **REV151-01** HIGH, the abandoned head | Define what the next freeze does after `abandoned`: the effective predecessor, proof that the generation sent no order, a damaged or restored database, and the timestamp that anchors broker corroboration. Require broker verification before abandon. | **The effective head:** the latest `sealed` or `genesis` entry before the abandoned entries, which stay in the chain as evidence. The next freeze uses that head's rules, and `--predecessor` must name it. **Abandon now requires five read-only checks:** (1) the database is G's (`database_id`, manifest digest) and passes `integrity_check`; (2) it has no intent; (3) G never assumed protection, because ratcheted stops would otherwise be lost; (4) a completeness-proven broker order census since G's freeze is empty; (5) no FILL since the effective head is uncited. **A damaged or lost database:** `--database-lost` is a broker-only path that keeps checks 4 and 5, with a critical alert. **The anchor:** the next freeze's stale-head corroboration starts at the effective head's `sealed_at`, so each abandoned generation's whole life is re-checked from the broker. **Afterwards:** an abandoned generation can never run, freeze or seal again (`registry_generation_abandoned`). | §7.7, §10, §11 |
| **REV151-02** HIGH, a delayed POST | A missing broker order lookup must not become a terminal rejection. Add a deterministic test that delays the POST past the recovery lookup. The intent stays unresolved until the broker or the client-order-ID resolution establishes the outcome, and is never cleared just so the wind-down can complete. | **This is a live defect in the current code, not only a gap in the spec.** `reconciliation/coordinator.py::_resolve_stranded` turns a 404 into `REJECTED` 120 s after `created_at`: `STRANDED_BEFORE_SUBMISSION` for a `SUBMITTED` intent and `BROKER_NEVER_RECEIVED` for a `SUBMISSION_UNKNOWN` one. **Rev. 6 replaces that rule everywhere, in its own build commit:** a 404 keeps the intent `SUBMISSION_UNKNOWN`, with its asset blocked and the lookup retried every pass. No time limit ends it. Only a `RISK_APPROVED` intent, which is never POSTed, may still be closed. The grace period counts from `submitted_at`. **The one manual exit:** an operator `resolve-dispatch`, accepted only after a provisional `DISPATCH_FINALITY_MINUTES`, with a fresh 404 and a completeness-proven order and fill census. **Afterwards:** the client order ID stays watched for 30 days and is carried in the handover. If it appears, that raises `RESOLVED_DISPATCH_APPEARED`, an integrity hold. **Tests:** the delayed-POST interleaving, with variants for a long hold, a POST never released, the order appearing after resolution, the successor, and the time base. | §7.1, §8 steps 4 and 7, §10, §11, §12 q. 10 |
| **REV151-03** clarification, monitoring an unverified mark | Stale marks must not drive price-dependent decisions. Time and expiry checks continue. Evaluation starts at a fresh executable quote, and exits still pass the gateway's quote freshness check. | **No stop, target, trailing ratchet or scale-out decision from any unaccepted price,** and no MFE/MAE fold from one. The restored stop and target stay unchanged. **Time, stall and near-expiry exits continue,** and the gateway still requires a fresh quote to submit. **At the first accepted quote,** a full evaluation runs and `assumed_at` is written. **Other positions** are unaffected. | §7.5.1, §10 |

## The reviewer's decisions, as recorded

| Question | Decision (2026-11-07) | Where applied |
|---|---|---|
| Q7, per-machine registry | Accepted conditionally, as a **hard operational restriction**: one account, one machine. No manual trading of it during a run or between generations. Broker-fill corroboration is not a distributed lock. | §7.7 limits; §11, which puts it in the run plan and runbook as a precondition |
| Q8, per-position unverified mark | Accepted, with the REV151-03 boundary. | §7.5.1 |
| Q9, automatic reversal credits | Accepted with provenance conditions: the raw digest verifies; the credit is linked by broker `fill_id` or `order_id` to an original fee already attributed; it does not exceed the unreversed fees for that link; it is recorded once. Anything else is `predecessor_cash_reversal_unlinked` and stays unresolved. The alert is mandatory either way. | §7.6, §7.6.1, §10 |

## Code facts checked for rev. 6 (at `24291c40`)

- **REV151-02:**
  - **The sweep:** `_resolve_stranded` (`reconciliation/coordinator.py`) checks `STRANDED_INTENT_GRACE_SECONDS = 120` against `created_at`. With `order is None`, it writes `REJECTED` with `BROKER_NEVER_RECEIVED` for a `SUBMISSION_UNKNOWN` intent, otherwise `STRANDED_BEFORE_SUBMISSION`.
  - **The gateway path:** `_recover_unknown_submission` (`execution/gateway.py`) already keeps a 404 as `SUBMISSION_UNKNOWN` and alerts. Only the sweep makes it terminal.
  - **The lookup:** `get_order_by_client_order_id` returns `None` only for a 404.
  - **`submitted_at`:** `TradeIntent.submitted_at` exists, but the gateway's `SUBMITTED` write (`replace(approved, status=SUBMITTED)`) does not set it. The build sets it.
  - **No test asserts** either rejection string.
- **REV151-01:** the successor-start evidence and `monitoring_assumed_at` that Rev.147 writes live in the generation database. So check 3, no protection assumed, is a read-only query of G's database, which check 1 has already proven is G's. The Rev.149 F02 completeness-proven order listing (`get_open_orders`) covers open orders only. Check 4 needs a listing of orders of every status by date window, under the same rules, built in Rev.147.
- **REV151-03:** `monitor/coordinator.py` uses `positions.current_price` for the stop and target breach checks, `_ratchet_stop` and `_fold_lot_extrema` (MFE/MAE). Those rules are price-dependent, and rev. 6 suspends all of them, the extrema fold included. The stall exit reads the lots' stored MFE, which a stale price would otherwise have updated. Neither `_time_stopped` nor `_near_expiry` reads the price.

## For the operator

- **REV151-02 changes runtime behaviour on main, not only in the wind-down.** No run takes place before the Rev.147 build, so rev. 6 puts the fix in that build as its own commit. If a run were scheduled before then, the fix would have to go in a small runtime Rev first.
- **The fail-closed cost:** while an intent stays `SUBMISSION_UNKNOWN`, its asset is blocked for every order, protective exits included. That is the existing behaviour for every `SUBMISSION_UNKNOWN` intent. Rev. 6 only stops the sweep ending it early.

## Open for the reviewer

10. **`DISPATCH_FINALITY_MINUTES`:** proposed at 15, provisional. It only gates the operator's `resolve-dispatch` and never resolves anything on its own.

`FEE_WAIT_MINUTES` (open question 3) is still provisional until G1.

## Next, if approved

1. Rev.147 G1: the read-only CFEE history check, which needs the operator's go-ahead for the broker read.
2. The implementation plan.
3. The build, on its own branch and worktree. The stranded-sweep change (§8 step 7) is its own commit.
4. An independent audit.
5. A merge on the operator's approval.
