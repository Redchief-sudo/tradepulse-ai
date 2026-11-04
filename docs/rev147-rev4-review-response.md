# Rev.147 rev. 4 — response to the rev. 3 assessment

**Spec:** `docs/superpowers/specs/2026-11-01-run-wind-down-design.md` (rev. 4, implementation-ready).
**Assessment answered:** the Rev.148 forensic build assessment (2026-11-04), §3, "Two Rev.147 questions still need resolution."
**Scope:** design only. No production code changed, no broker calls, no orders, no database writes.
**Base:** main `a4cf99e9` (Rev.146 DBAE merged).

## Conditions applied

| Assessment item | Reviewer position | Rev. 4 answer | Sections |
|---|---|---|---|
| **A. Monitoring continuity** (open question 5) | Accept the session-boundary handover only with strict successor-start verification and an explicit record of the exposure interval. The gap audit cannot recreate absent protection; it can only identify breaches and require intervention. | **Successor-start verification:** five ordered checks must all pass before `monitoring_assumed_at` is written: (1) the checkpoint comparison; (2) a fresh census at assumption time, covering account drift since freeze; (3) the successor's own restored rows re-digested against the handover; (4) fresh marks; (5) one full evaluation, with any exit it decides submitted first. A failure gives `handover_assumption_unverified` and the runtime does not start. No amendment waives a check, and `--accept-gap` accepts the gap only. **Exposure record:** `protection_exposure` is recorded in every case, `no_session_gap` included. It holds the interval, the unmonitored sessions, each carried position's stop at release, the unmanaged crypto residuals, the continuity class, the audit results and the check results. It is written in the successor's evidence and as a system record in the predecessor's amendment chain, so the sealed files stay byte-identical. A refused startup leaves an open exposure, which time never closes. **Gap audit:** stated as detection only. A breach or an unverified audit starts a gap-breach hold: protective exits run, non-protective BUYs are refused (`GAP_BREACH_UNACKNOWLEDGED`), and the hold ends only through an operator `--ack-gap-breach` for each position. **Limits stated:** the equivalence covers regular-session equity and option exits only, and not crypto residuals or out-of-session gaps. | §7.3, §7.5, §8, §10, §11 |
| **B. Options expiring between generations** (open question 6) | Classify as unproven and block completion until the operator resolves it. | **Adopted.** A long option expiring **on or before** the NY date of `next_session_open` is `unproven` (`carried_option_expires_unmonitored`). The expiry day counts because a late or refused successor start would leave it unmonitored. Missing or malformed expiry metadata, the monitor's existing `_option_expiry_metadata_invalid`, is `unproven` (`carried_option_expiry_unknown`). So is every carried option when the calendar cannot verify the next open. No option sell, exercise or close is sent. The wind-down becomes `UNRESOLVED` until a broker-verified `--resolve`. | §4.2, §10, §11 |
| **15-minute fee wait** | Remains provisional pending G1. | Unchanged. `FEE_WAIT_MINUTES` stays provisional until Rev.147 G1 records the CFEE delay and its link. | §4.5, §9, §12 (q. 3) |

## Code facts checked for rev. 4

- **Option expiry lives in local state only:** `holding.asset.metadata["expiry"]`, parsed by `monitor/coordinator.py::_near_expiry`, with invalid metadata detected by `_option_expiry_metadata_invalid`. The option rule reuses both, and adds no new source of expiry.
- **A dividend or fee on a carried asset between freeze and startup** is normal (Rev.143, §7.6). Successor check 2 therefore refuses only position-changing activity (fills and quantity-changing corporate actions), not cash-only activity.

## Still open

- **§4.5:** `FEE_WAIT_MINUTES = 15` is provisional until G1. This is the only open question left in §12.

## Implementation readiness

With open questions 5 and 6 decided, the spec lists every new state, refusal, reason, record and test (§10, §11). If the reviewer approves rev. 4, the next steps are:
1. Rev.147 G1 (read-only CFEE history);
2. the implementation plan (writing-plans);
3. the build on its own branch and worktree;
4. an independent audit;
5. a merge on the operator's approval.

DBAE G1b and G2 follow, per the assessment's sequence.
