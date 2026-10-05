# Rev.113: option execution limits and liquidity-aware contract choice

Rev.113 follows Rev.112 (`266d0d4e`).

## Finding

Three accounting soaks attempted about ten option buys. Only one passed the shared spread limit (1.5% on `balanced`). The rejected quoted spreads were 1.87, 1.93, 2.58, 2.94, 3.79, 3.79 and 5.80%. Spread is measured relative to the premium, so a 5-cent market on a $3 contract is already 1.7%; a limit calibrated for shares blocks nearly every option. Contract selection also ignored liquidity entirely: it took the single strike nearest the OTM target in the midpoint expiry, even when a neighbouring strike was far tighter.

## Changes

- `RiskLimits.options_spread_limit_pct` / `options_slippage_limit_pct`, resolved through `spread_limit_for` / `slippage_limit_for` and applied by the risk engine to option buys only. Equities and crypto keep their existing limits. Each option slippage limit is half its spread limit, matching the half-spread slippage estimate.

  | Profile | Shares spread / slippage | Options spread / slippage |
  |---|---|---|
  | aggressive | 2 / 1.5 | 3.5 / 1.75 |
  | balanced | 1.5 / 1 | 2.5 / 1.25 |
  | conservative | 1 / 0.5 | 2 / 1 |
  | micro | 2 / 1 | 3 / 1.5 |

- `option_candidates` returns the five strikes nearest the OTM target within the same midpoint expiry. Its first element is exactly the previous `select_contract` choice, which is kept unchanged.
- The scanner quotes those strikes. `choose_liquid_contract` trades the nearest one whose spread is within the option limit. If none qualifies, it chooses the tightest, so the risk engine evaluates a real contract and records the rejection. An `option_contract_selected` log line lists every considered contract's spread.

With `balanced`, 2 of the 7 observed rejections (1.87% and 1.93%) would have passed on the new limit alone, before any gain from choosing a tighter neighbouring strike.

Stop placement, sizing, the expiry window, the OTM target and every equity/crypto rule are unchanged. Because protected source changed, both soaks must be run again from Rev.113.

## Validation

Full Python suite passed. New coverage:
- candidate ordering, and its equivalence with `select_contract`
- the spread formula matches the risk engine
- nearest-within-limit choice, with fallback to the tightest
- option limits apply to options only, and still reject excessive spreads
- every profile defines wider, consistent option limits
- an end-to-end options scan trades the liquid neighbouring strike when the target strike is too wide

Two existing concurrency tests (`test_concurrent_buys_for_different_symbols_serialize_through_portfolio_risk_lock`, `test_in_flight_detection_is_correct_behind_a_large_non_blocking_backlog`) failed once under heavy machine load during a live soak. They pass in isolation on both this revision and Rev.112.
