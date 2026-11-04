# Rev.147 rev. 3 — response to the rev. 2 forensic review

**Spec:** `docs/superpowers/specs/2026-11-01-run-wind-down-design.md` (rev. 3).
**Scope:** design only. No production code changed, no broker calls, no orders, no database writes.
**Method:** each finding was checked against the code on `main` before it was answered. The code references below are what the check found.

## Findings

| Finding | Verified against the code | Rev. 3 answer | Sections |
|---|---|---|---|
| **R2-01 stop preservation** | **Confirmed, and wider than stated.** `adoption/__init__.py:307` rebuilds `stop_loss` from Alpaca's average entry (`entry_stop_loss`), and the adopted `Holding` has `current_stop = None` (`:239-240`). A ratcheted stop is lost. So are `scaled_out` (a second scale-out becomes possible) and lot `opened_at` (time and stall exits restart). The ladder's R also uses `average_price − stop_loss` (`monitor/coordinator.py::plan_ladder_exit`). | The handover carries the full protective state: the holding (`stop_loss`, `current_stop`, `target_price`, `scaled_out`, `average_price`), the lots (`acquisition_price`, `opened_at`, MFE/MAE), the effective stop, the stop state, a digest and provenance. The successor restores it exactly and never applies a rebuilt stop (recorded as `reconstructed_stop` only). Freeze validates five checks, including equality with the predecessor database's final rows, and refuses on any failure. No amendment can change a stop. | §4.2, §7.1, §7.2.1, §10 |
| **R2-02 monitoring continuity** | **Confirmed gap.** The preferred overlap is **not possible**, for three reasons: the account lease (`runtime_lease.py`) allows one runtime per account; two monitors could double-exit; and a predecessor exit after its seal would fall outside its sealed evidence. Stock and option exits act on regular-session prices only (`monitor/coordinator.py`, ~:398 and :452), and the seal is outside a session (§3.1). | **Pushback on the mechanism, not the requirement.** A session-boundary responsibility rule: the predecessor records `monitoring_released_at` and `next_session_open`; the successor records `monitoring_assumed_at`. If the successor takes over before the next open, the result is `no_session_gap`, equivalent to a continuous run. Otherwise startup refuses (`handover_protection_gap_unaccepted`) until the operator accepts with `--accept-gap`, and a gap audit runs. `WIND_DOWN_COMPLETE` is explicitly *not* a protection claim. The reviewer is asked to accept the equivalence (open question 5). | §6.3, §7.5, §12 |
| **R2-03 inventory completion** | **Confirmed.** Rev. 2's completion rule named crypto and "local holdings" only. Open orders were read from the broker list, but an order that left the list had no tracked terminal state. | Four-class partition B = Carried ⊎ Residual ⊎ Unresolved, with `unproven` as the default class. A seven-part `COMPLETE` invariant, and a reduced one for `UNRESOLVED_ACCEPTED`. A new observed-order register means every observed order needs a broker terminal state (`wind_down_order_unaccounted`). `resolve` is refused while any order is non-terminal. | §4.1, §4.2, §4.4, §5, §5.1 |
| **R2-04 late fees** | **Confirmed, and rev. 2 had a wrong citation.** `verification/soak.py::_late_fee_reopenings` is intra-generation checkpoint supersession and does nothing after a seal. Today a predecessor fee arriving after the successor's opening would be `unresolved_generation_membership` in `reconciliation/membership.py`. Fee links exist only when the activity carries `fill_id` or `order_id` (`generation_fees.py::cash_fee_receipt`). Non-debits are already refused (`GENERATION_FEE_NOT_A_DEBIT`). | One new membership class, `predecessor_obligation`, outside `ELIGIBLE_MEMBERSHIPS`. Matching is by broker link only; an unlinked fee needs the operator's `--resolve-fee`, verified read-only. Activity ID is the key: an identical repeat is a no-op, and a conflicting repeat is refused. Split fees are allowed, and reversals become `fee_correction` (alert, no posting). System-written `fee_attribution` records go in the predecessor's amendment chain. Sealed files stay byte-identical, and reports show `post_seal_adjustments`. Pending fees chain forward. `FEE_WAIT_MINUTES` is provisional until G1, which now also records whether the CFEE carries a link. | §4.5, §7.1, §7.6, §9, §10 |

## Decisions applied

| Decision | Rev. 3 |
|---|---|
| Wind-down exits in reporting | Full financial accounting, with attribution kept. Excluded from edge statistics and reported as `wind_down_excluded`, built like Rev.142's `adopted_excluded` (`verification/evidence.py`). The totals reconcile across the sections. Only `exit_reason = "run_wind_down"` qualifies (§4.3). |
| 60-minute escalation | Alert only: an `escalated` event, and the state stays `BLOCKED`. In rev. 2, escalation moved the state to `UNRESOLVED`. That is removed, and `UNRESOLVED` now comes only from census facts. `resolve` no longer depends on the timer (§5). |

## Architecture preserved

- Sells, cancels and recovery go through `ExecutionGateway`, Rev.140's sellability, client-order-ID idempotency, `SUBMISSION_UNKNOWN` reconciliation, `SettlementProcessor` and the reconciliation coordinator.
- Late fees use the existing membership classifier and generation cash ledger. No second settlement or recovery path is added.
- The build is on its own branch and worktree after the Rev.146 merge, and no production code changes until rev. 3 is approved.

## Open for the reviewer

1. **§7.5:** accept the session-boundary equivalence in place of a monitor overlap.
2. **§4.2 (new):** a carried option expiring before the successor's first session. Should it be `unproven` at wind-down, or carried with an alert?
3. **§4.5:** `FEE_WAIT_MINUTES` stays provisional pending G1.
