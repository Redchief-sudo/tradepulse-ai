# Rev.125: entry composite threshold

Rev.125 follows Rev.124 (`295fee45`). It is a strategy setting change chosen by the operator before the soaks are re-run. Protected source changed, so soak 1, soak 2 and the official prove-edge run all use this setting.

## Finding

In soak 1 (2026-10-07 to 2026-10-11) the deterministic agreement gate rejected 549 candidates the AI had rated at or above the 80% confidence minimum. The gate required the composite score to fall in the BUY band (above 65). Rejected composites averaged:

| Lane | Average composite of rejected candidates |
|---|---|
| Stocks | 61.1 |
| Options | 60.9 |
| Crypto | 53.8 (maximum 57) |

## Change

`ENTRY_COMPOSITE_THRESHOLD = 62.5` in `tradepulse/scanner/coordinator.py`. An entry now needs:
- a composite score above 62.5, instead of the BUY band's above 65;
- AI confidence of at least the profile's `min_confidence`, which is unchanged at 80 for `balanced`.

The recorded `deterministic_signal` label and its bands are unchanged, so an entry at 63 is recorded as HOLD with its composite. No exit uses the composite.

Over soak 1's three days this would have admitted about 49 more stock candidates (5 symbols) and 39 more option candidates (3 underlyings), and no crypto. The 3-trades-per-day cap, the 5-position limit, the exposure and daily-loss limits, the spread and slippage limits, the stops and confidence-scaled sizing all still apply.

## Validation

`test_entry_composite_threshold_is_just_under_the_buy_band` covers 62.5 (rejected), 62.51 and 64 (admitted), and 57 (rejected). The existing deterministic-gate rejection test and the full suite pass.
