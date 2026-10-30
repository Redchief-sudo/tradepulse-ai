# Remediation of the Rev.145–142 batch review (2026-10-29)

This record answers the forensic audit of `vegabond-tradepulse(rev.145-142-batch).zip` (tip `c8f9be21`), one finding at a time. Each fix was checked by running its new test against the old code first; the test failed there and passes now.

Branch `rev142`; the remediation is one commit on top of `c8f9be21`.

## Findings

### TP145-03: an order-level closing fee counted once per fill (confirmed, fixed)

**Defect.** `closing_fees()` added every broker fee receipt that named a fill's order to *each* fill of that order. One sell order filled as 4 + 6 shares with one $2 order fee was charged $2 on each fill, $4 in all. Both the run exclusion `N(k)` and the adopted P&L projection were affected.

**Fix.** `closing_fee_allocation()` in `tradepulse/adoption/accounting.py` allocates each receipt exactly once:
- A receipt naming a known fill goes to that fill alone.
- A receipt naming only an order is split across all of that order's fills by quantity, with a largest-remainder split in the amount's smallest unit (at least a cent), so the parts sum exactly.
- Each fill's own fee is counted once.

**Tests** (`test_rev142_accounting.py`):
- `test_order_level_fee_is_allocated_once_across_partial_fills`: $0.80 + $1.20 = $2, and X = $148. The old code gave $2 + $2.
- `test_fee_naming_a_fill_goes_to_that_fill_only`: a fill-specific fee plus an uneven three-way order fee, summing exactly to $1.50.
- `test_late_fee_correction_and_replay_conserve_fees`: a delayed $2 receipt, a $0.50 refund, then a replay that changes nothing. No lot reopens (T18).

### New finding R-1: adopted closes entered `observed_generation_net` (found during remediation, fixed)

**Defect.** The review did not list this one.
- `observed_generation_result()` in `tradepulse/verification/evidence.py` summed `realized_pnl` over *every* trade attribution and deducted *every* fill's fee.
- An adopted lot's attribution is measured from the broker's average entry price. Its close therefore added inherited, pre-run gains or losses to `observed_generation_net`.
- `observed_generation_net` is a pass/fail criterion (`> minimum_net_pnl`). This contradicts Rev.142's rule that adopted inventory contributes $0 to run performance, which `X(k)` already enforces for equity.

**Fix.**
- Adopted attributions, the fees of adopted closing fills, and the broker fee receipts linked to those fills or orders are left out of the bridge.
- They are reported separately under `observed_generation_bridge.adopted_excluded`, which lists `realized_pnl`, `cash_fees` and `fill_fees`.

**Test.** `test_no_fill_lookup_and_evidence_through_a_real_exit` now also checks that gross realized P&L is $0 after a full adopted liquidation. The old code gave -$200.

### New finding R-2: a stale-view rebuild dropped an adopted holding's ratcheted stop (found while writing T13, fixed)

**Defect.**
- When reconciliation rebuilt a stale holding from its lots (view drift), `_rebuild_holding_from_lots` returned a holding with no `current_stop` and with `scaled_out=False`.
- For an adopted position, this lowered the protective stop back to `stop_loss` and allowed a second scale-out. That breaks ADOPT-07.

**Fix.**
- For adopted lots only, the rebuild carries `current_stop` and `scaled_out` over from the holding it replaces. An adopted asset takes no buys, since a buy latches `MIXED_LOT_ORIGIN`, so the earlier floor still belongs to the same position.
- Strategy lots are unchanged. A new buy can move their average price, so carrying a floor over there needs its own decision. It is listed under follow-ups.

**Test.** `test_ratchet_partial_sale_replay_and_restart_never_lower_the_stop` (T12, T13) runs these steps:
1. a scale-out at 1R;
2. the ATR trail;
3. a settlement replay;
4. a stale-view rebuild;
5. a fresh monitor cycle.

Through all of them `current_stop` never decreases, `stop_loss` is kept, and there is exactly one scale-out. On the old code, the check fails at step 4.

### R-3: a missing adopted holding was rebuilt with no protective state (second review, fixed)

**Defect.**
- When the holding record was missing, but its adopted lot was still open, the rebuild used `current_stop=None` and `scaled_out=False`. This was found in the second review.
- The position could then be scaled out a second time, and a raised stop fell back to `stop_loss`, with no alert.

**Fix.**
- The rebuild restores only what the lot itself proves. Any recorded sale of an adopted lot is either the scale-out or a partly filled exit. Either way it sets `scaled_out=True` and a floor of break-even (the average price).
- The ATR trail can't be recovered, because the ATR value is not stored. It is not guessed: a critical `ADOPTED_PROTECTIVE_STATE_UNVERIFIED` alert names the restored stop, so the operator can check it.

**Why not refuse to rebuild, as the review suggested.** With no holding, the monitor has nothing to protect: the position would be reported as unmanaged, with no stop at all. A provable floor plus an incident is strictly safer than no stop.

**Tests.**
- `test_missing_holding_restores_only_what_the_lot_proves`: break-even restored; no second scale-out; one critical alert. The old code failed on `scaled_out`.
- `test_missing_holding_before_any_sale`: only the adoption's stop is restored, and the alert still fires.

### TP145-01: unexpected adopted positions cannot complete a protective sale (confirmed; behaviour kept, documented)

**Status.** The behaviour is unchanged and deliberate. No broker fill is invented, no sellability check is bypassed, and no hold is cleared.
- A position bought outside TradePulse *after* the fill cursor is adopted and monitored.
- Its exit is refused with `UNRECONCILED_BROKER_ACTIVITY` until the operator resolves the external execution with `tradepulse quarantine resolve`.
- `ADOPTED_EXIT_REFUSED` fires once per asset per hour, and nothing is submitted.

**Wording.** Rev.142's record now describes `unexpected_external` adoption as *monitored and quarantined, not automatically exit-capable*.

**Test.** `test_unexpected_position_after_the_cursor_stays_protected_not_sellable` is T20 as Rev.140 allows today. It checks:
- the 12-share adopted lot stays open;
- the refusal reason is `UNRECONCILED_BROKER_ACTIVITY`;
- one alert fires;
- no order is submitted.

**What remains.** A future Rev must define a verified `adopted_position` outcome for Rev.140, so an exit can be authorised from real broker evidence. It is not in this batch, because it changes Rev.140's evidence rules and needs the operator's decision.

**Effect on the 24 h run.** None. The planned position is opening inventory: it is bought before the cursor bootstrap, so the bootstrap baseline explains it.

### TP145-02: adopted crypto has no fee-replay path (confirmed; adoption now refused)

**Fix.**
- Crypto is no longer adopted. `_eligible` returns `CRYPTO_ADOPTION_UNSUPPORTED`, and `ADOPTABLE_CLASSES` is equity and options.
- A crypto position TradePulse does not manage stays reported as unmanaged, exactly as before Rev.142.
- The fail-closed `ASSET_FEE_ADOPTED_LOT_UNSUPPORTED` checks in `historical_fees.py` and `fee_replay.py` stay as a second guard.
- The crypto adopted-epoch code also stays. It is unreachable from adoption until a future Rev defines coin-fee allocation for adopted lots.

**Test.** `test_crypto_is_not_adopted`.

### TP145-04: dividend accounting not yet verified against a real dividend (confirmed; procedure documented)

`DIVIDEND_REPRESENTATION` stays `"unknown"`. The first-dividend procedure is in `docs/rev143-dividend-recognition.md` § "First real dividend: evidence and acceptance". It also covers the case the reviewer raised: a receipt whose shape fails validation (`DIVIDEND_RECEIPT_INVALID`) records no cash, so the cash check fails under that named reason rather than silently.

## Incomplete Rev.142 items, after remediation

| Item | Status |
|---|---|
| T12, T13 (ratchet, replay, restart) | Built (above) |
| T18 (fee correction after full closure) | Built for cash-fee receipts. The fee-replay path is unreachable for adopted lots: crypto is refused, and equities have no asset-denominated fees. |
| T20 | Built as today's behaviour. A full liquidation awaits the Rev.140 outcome (TP145-01). |
| T21 (statistics exclude adopted closes) | Built: empty population, no win rate, an empty breakdown, and no `prove_edge` key that is eligible. |
| T11, one test per site | Still the T26 AST guard plus the runtime tests that exercise the sites. Not split per site. |
| Dashboard badge and P&L split (§3.12) | Not built |
| Unexpected-position automatic exit | Restricted by Rev.140 (TP145-01) |
| Adopted crypto | Refused (TP145-02) |
| First real dividend | Unverified (TP145-04) |

## Follow-ups (not in this batch)

1. A Rev.140 `adopted_position` outcome (TP145-01).
2. Coin-denominated fee allocation for adopted crypto (TP145-02).
3. Whether a view-drift rebuild should keep a strategy holding's ratcheted stop (R-2).
4. The dashboard badge and P&L split.
5. `parse_activity_row`'s OCC option symbols (carried over).

## Operator acceptance (2026-10-29)

- **TP145-01: accepted.** A position bought outside TradePulse after the fill cursor stays monitored, but cannot be sold automatically until the operator resolves its external execution. The operator's intent is that no trades happen outside the system.
- **TP145-02: accepted as is.** Crypto adoption stays refused. No adopted-crypto Rev is queued; unmanaged crypto is reported as before Rev.142.
