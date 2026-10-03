# Rev.116: whole-position cap and dashboard request guard

Rev.116 follows Rev.115 (`c41f7e3f`).

## Findings

**F4: `max_position_pct` capped each order, not the position.** The risk engine compared only the new order's notional with the cap, so repeated BUYs of the same asset could grow a position far beyond `max_position_pct` of equity, one compliant order at a time.

**F2: the dashboard accepted requests from any website.** The dashboard binds to 127.0.0.1 and has no authentication, but binding locally does not stop a page open in the operator's own browser. A hostile site could POST to the control endpoints (start, stop, reset), and a DNS-rebinding hostname could read state.

## Changes

- F4: `RiskEvalOptions.held_notional` is the absolute broker `market_value` of the position already held in the asset (option multiplier included). The gateway passes it for BUY only. The risk engine adds it to the order notional before applying `max_position_pct`, so the cap bounds the whole position. A position already at the cap cannot grow.
- F2: a `local_control_guard` middleware in `tradepulse/web/app.py`:
  - every request must carry a Host of `127.0.0.1` or `localhost`, otherwise 403 `HOST_NOT_ALLOWED` (defeats DNS rebinding, also for reads);
  - every non-GET/HEAD/OPTIONS request with an Origin must have a local Origin, otherwise 403 `ORIGIN_NOT_ALLOWED`;
  - every such mutation must send `X-TradePulse-Control: 1`, otherwise 403 `CONTROL_HEADER_REQUIRED`. A cross-site page cannot add a custom header without a CORS preflight, and the app never grants one.
- The frontend `post()` in `frontend/src/api.ts` sends the header. Local GET polling needs no header and is unchanged.
- The dashboard still always binds 127.0.0.1; there is no host option and no CORS middleware.

## Validation

- New risk tests: `test_held_notional_counts_toward_position_cap`, `test_position_already_at_cap_cannot_grow`, `test_held_option_notional_counts_toward_position_cap`.
- New web tests: mutation without header refused; cross-origin mutation refused even with header; rebound host refused for reads; local reads and local mutations work.
- The shared web test client now uses `http://127.0.0.1:8766` and sends the control header.
- Frontend tests pass; full Python suite result recorded in the Rev.116 task report.

Protected source changed, so both soaks must be run again from Rev.116.
