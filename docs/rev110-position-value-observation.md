# Rev.110: position-value observation rounding and coordinated broker reads

Rev.110 follows Rev.109 (`a79ee2f`). It corrects the remaining defect that made every generation equity reconciliation `incomplete_evidence` while a position was open. The defect was found in the interrupted accounting soak 1 rerun (`data/soaks/20260928T142358Z/`, preserved, not modified).

## Findings

The rerun started 2026-09-28 14:24 UTC. Every check matched while the account was flat. After the soak's first equity fill (9.977 AAPL at 340.15, 17:40 UTC), all 12 subsequent checks were `incomplete_or_mismatched`. In every one, cash, position quantities, holdings-versus-lots, fee finality and broker equity arithmetic all **matched**. The only failing input was `position_value_observation_status = different_uncoordinated_observations`, which `mandatory_invariants` required to be exactly zero.

That check compares the sum of exact position marks (`/v2/positions`) against the account's `long_market_value + short_market_value` (`/v2/account`). It had two independent causes:

1. **Broker cent rounding (10 of 12).** Alpaca reports account-level market value rounded to the cent while each position carries its exact mark: `9.977 x 339.675 = 3388.937475` posts as `3388.94`. Every such difference was under half a cent.
2. **Quote moves between two HTTP responses (2 of 12).** Differences of `0.10138` and `0.05368` came from the mark moving between the separate account and positions requests, which share no valuation timestamp.

The rerun then ended at 17:56 UTC when the host was powered off (`Power key pressed short`), so it cannot count toward the uninterrupted minimum regardless of this fix.

## Corrections

- `tradepulse/valuation.py` `position_value_observation`: when the broker's account-level value is itself a whole-cent amount, reproducing the broker's cent rounding (of the total, or per position then summed) is treated as equal. This models broker rounding the same way `broker_settled_cash` does; it is not a tolerance. Any quote move surviving the rounding stays `different_uncoordinated_observations`. A broker value that is not whole-cent is compared exactly. The exact difference is always preserved in the snapshot and reconciliation record. New status: `equal_after_broker_cent_rounding`.
- `tradepulse/valuation.py` `observe_broker_valuation`: reads positions, then the account, then positions again, up to three times, until identical position marks bracket the account read. If marks keep moving, the last observation is returned unchanged, so the difference stays visible and the check fails closed as before. Used by generation reconciliation (`cli.py`), `reset-integrity`, the scanner's per-cycle equity snapshot, the dashboard risk-exposure endpoint, and the accounting repair tool.

No thresholds, fee/slippage overlay rates, risk parameters or trading logic changed.

Evidence could not distinguish total-level from per-position rounding: no stored snapshot has more than one open position. Both exact models are accepted; the next soak's multi-position snapshots will show which Alpaca uses.

## Operational notes

- Because the protected source changed, both soaks must be run again from Rev.110 with new database/report paths before an official freeze.
- The interrupted rerun left 9.977 AAPL open on the paper account with no runtime managing it. Resolve it before starting a new soak; the verification doc warns that mixed opening and new inventory in one symbol is unproven.
- Soaks run for 12.5-24.5 hours on this host. Prevent sleep for the run's duration (for example `systemd-inhibit --what=sleep:idle:handle-lid-switch:handle-power-key`), keep the host on mains power, and do not use the power button.

## Validation

Complete Python suite passed. New regression coverage (`python_tests/test_valuation_observation.py`): the real soak value `3388.937475` vs `3388.94` is equal after broker rounding with the exact difference preserved; total and per-position rounding; the real `0.10138`/`0.05368` quote moves and a one-cent move stay different; an unrounded broker value is compared exactly; rounded and exact snapshots yield the same mandatory invariants; coordinated reads stop after one account read on stable marks, re-read on moving marks, and return the last observation unhidden when marks never settle. Replaying the rerun's 12 failed snapshots through the new comparison yields 10 `equal_after_broker_cent_rounding` and 2 `different_uncoordinated_observations`, as expected; the coordinated re-read addresses the latter at observation time.
