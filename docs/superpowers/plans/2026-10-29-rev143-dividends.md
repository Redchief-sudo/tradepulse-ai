# Rev.143 Dividend Recognition Implementation Plan

> Executed natively (operator decision 2026-10-28: build all, one review).

**Goal:** record every in-run `DIV*` activity's cash exactly once, so the generation cash check balances, and attribute it to entitled holders only with verified evidence. Strategy dividends go into run equity; inherited ones go into `D_excl`; unproven ones go into `D_unresolved`.

**Spec:** `docs/superpowers/specs/2026-10-27-dividend-recognition-design.md` (rev. 5, plus the G0 results in §3.8). **Depends on:** Rev.144's calendar (`session_deadline`) and Rev.140's quarantine reads.

## Global constraints
- `DIVIDEND_REPRESENTATION = "unknown"` (G0): cash is posted, attribution is refused.
- Escalation is at the close of the 3rd NYSE session after first observation (Rev.144 `session_deadline`).
- The announcement window is [credit date − 90 days, credit date].
- No tolerance on any amount or quantity check.

## Review Focus
1. A receipt with no `created_at`: its cash time is `date` at 00:00 New York, recorded as such.
2. A dividend activity first seen in a late-arriving population: `first_observed_at` is the cycle that first saw it, never `date`.
3. An announcement amended between fetches: the version used is the one stored with its SHA-256.
4. A crash between recording and attribution: attribution is retried once and written once (T22).
5. With representation `unknown`, no code path ever writes an attribution row (T31).

### Task 1: Receipts, recording, schema, valuation (§3.1–3.3, §3.10; T1–T5, T20, T23)
### Task 2: Announcements on the broker port (§3.4 step 1)
### Task 3: Attribution (§3.4 steps 1–6; T6–T12, T16, T27–T31, T33, T34)
### Task 4: Reversals, escalation, quarantine, run equity, recovery (§3.4 step 7, §3.5–3.7; T13–T15, T17–T19, T21, T22, T24–T26, T32)
### Task 5: Rev record
