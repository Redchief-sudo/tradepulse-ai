# DBAE: Dynamic Balance-Aware Execution (Alpaca)

Status: design for review, rev. 1 (2026-10-30). No production code changes are made until this spec is approved.

## 1. Purpose and scope

TradePulse must be usable with as little as $20. DBAE decides, at every new entry, which asset classes the account's verified balance unlocks. It also decides whether the broker and the asset support the order and its protective exit, and how much the account can fund. It then shapes the order to fit, by rounding down only.

DBAE is **not** a risk profile. The profile the operator selects (`conservative`, `balanced`, `aggressive` or `micro`) keeps setting every risk limit. DBAE never raises a limit, never forces a minimum order and never weakens a stop.

In scope:
- Alpaca only, for paper and account-qualified use.
- New entries: equity, crypto and options.
- Scale-out feasibility.
- Protective-exit recovery rules.

Out of scope:
- IBKR. DBAE's rules on day trading are Alpaca-specific: IBKR may still apply legacy PDT rules until 2027-10-20 and needs its own capability evidence.
- Broker routing (DBAER).
- Scan cost (its own Rev).
- Cash-account settlement (refused; §5.3).
- Live trading.

## 2. Evidence

### 2.1 G0 probe, 2026-10-30 (read only)

Saved in `~/tradepulse-evidence/2026-10-30-dbae-g0/` (manifest sha256 prefix `5bbf17226e44f2ab`, no account identifiers).

**`GET /v2/account`** has:
- `equity`, `last_equity`, `cash`, `buying_power`, `non_marginable_buying_power`, `regt_buying_power`, `effective_buying_power`, `options_buying_power`;
- `multiplier` (`4`), `status` (`ACTIVE`);
- `trading_blocked`, `account_blocked`, `trade_suspended_by_user`, `transfers_blocked`, `shorting_enabled`;
- `crypto_status` (`ACTIVE`), `crypto_tier`;
- `options_approved_level` (`3`), `options_trading_level` (`3`);
- `sma`, `intraday_adjustments`, `pending_reg_taf_fees`.

It has **no** `pattern_day_trader` and **no** `daytrade_count`.

**`GET /v2/account/configurations`** has:
- `closing_transactions_only`, `suspend_trade`, `fractional_trading` (`true`);
- `max_margin_multiplier`, `no_shorting`, `disable_overnight_trading`, `ptp_no_exception_entry`, `trade_confirm_email`.

It has **no** `pdt_check` and **no** `dtbp_check`.

**`GET /v2/assets/{symbol}`:**
- AAPL is `fractionable: true`; BRK.A is `fractionable: false`.
- BTC/USD has `fractionable: true`, `min_order_size: 0.00001305`, `min_trade_increment: 0.000000001` and `marginable: false`.

### 2.2 Regulation and broker behaviour

- FINRA Regulatory Notice 26-10 replaces the pattern-day-trader framework (day-trade counting and the $25,000 minimum) with intraday margin standards. It took effect on 2026-06-04, with a transition through 2027-10-20 during which brokers may apply either regime.
- Alpaca runs the new framework for Trading API accounts. It removed the PDT designation, the day-trade count and their API fields (gone by 2026-07-06).
  - It applies pre-trade checks that reject orders that would create an intraday margin deficit.
  - Repeated unmet margin calls can restrict an account for up to 90 days.
  - 4x intraday buying power needs $2,000 equity.
- Alpaca fractional orders support market, limit, stop and stop-limit with `day` time in force.
  - Alpaca's page contradicts itself on fractional trading outside regular hours. DBAE treats fractional orders as regular-hours only.
  - The page does not state whether a whole fractional position worth less than $1 can be sold (§8.4).

**Consequence:** DBAE builds no day-trade counter for Alpaca. It relies on:
- the account's restriction flags;
- conservative funding (§7);
- Alpaca's own pre-trade margin rejection, kept as an independent safeguard.

Sources:
- [FINRA RN 26-10](https://www.finra.org/rules-guidance/notices/26-10)
- [Alpaca: FINRA retires the PDT rule](https://alpaca.markets/blog/finra-retires-the-pdt-rule-introducing-alpacas-new-intraday-margin-framework/)
- [Alpaca fractional trading](https://docs.alpaca.markets/docs/fractional-trading)
- [Investor.gov: margin rules for day trading](https://www.investor.gov/additional-resources/news-alerts/alerts-bulletins/margin-rules-day-trading)

### 2.3 What exists today

- **Sizing.** `evaluate_risk` (`tradepulse/risk/engine.py`) sizes from the risk budget, applies the position, sector, exposure and cash caps, rounds by asset class, and enforces the $1 `min_lot_notional` and the cost-to-risk rule.
- **Cross-symbol lock.** `_portfolio_risk_lock` (`tradepulse/execution/gateway.py`) serialises every buy decision across symbols, and the `RISK_APPROVED` write is fenced on its lease.
- **Pending buys** count toward the exposure and sector caps, through `build_portfolio_snapshot` (RISK-091-01 buckets).
- **The gap DBAE closes.** The cash cap uses the broker's raw `account.cash`, which Alpaca lowers only on a fill. A sent but unfilled buy is therefore not deducted from the next buy on a different symbol. On a margin account, Alpaca's 4x buying power can then accept both orders and borrow the difference.
- **Exits.**
  - Every order carries `client_order_id == trade_intent_id`.
  - An ambiguous submission is recovered by that id (`_recover_unknown_submission`). If it can't be found, the order becomes `SUBMISSION_UNKNOWN`, the asset stays blocked, and reconciliation re-checks it every pass.
  - Orders are `market`, with `day` time in force (`gtc` for crypto).
- **Option entries** are single-leg long buys only. No strategy sells to open or builds multi-leg orders.
- **The old ladder.** `TRADEPULSE_AUTO_RISK_PROFILE_BY_EQUITY` (`config/settings.py`) and `EQUITY_PROFILE_LADDER` (`config/risk_profiles.py`, used by `cli._resolve_risk_profile_id`) switch the risk profile by equity. DBAE retires them (§12).

## 3. Architecture

```
scanner (prefilter only) ──► ExecutionGateway.execute_intent (BUY, non-protective)
                                 │ inside _portfolio_risk_lock:
                                 ├─ AccountEvidence    ◄─ broker/capabilities.py (fresh read)
                                 ├─ AssetTradingCapabilities ◄─ broker/capabilities.py (fresh read)
                                 ├─ SessionBaseline    ◄─ dbae/baseline.py (stored, write-once per date)
                                 ├─ pending_buy_funding ◄─ risk/engine.py (shared RISK-091-01 buckets)
                                 ├─ dbae.policy.evaluate_entry_policy(...) ─► ExecutionPolicy (pure)
                                 ├─ evaluate_risk(..., available_cash=policy.funding_available)
                                 ├─ dbae.policy.shape_quantity(...) (round down, exit feasibility)
                                 └─ persist RISK_APPROVED (lease-fenced) with policy in risk_snapshot
```

### 3.1 Units

**`tradepulse/dbae/bands.py`:**
- `Band` enum: `BELOW_MINIMUM`, `MICRO`, `MICRO_PLUS`, `FRACTIONAL`, `STANDARD`, `FULL`;
- `band_for(equity: Decimal) -> Band`;
- `unlocks(band) -> BandUnlocks`.

**`tradepulse/dbae/policy.py`** is pure: no I/O and no clock reads.
- `evaluate_entry_policy(account, baseline, asset, proposal) -> ExecutionPolicy`
- `shape_quantity(policy, asset, approved_qty, price, stop_price) -> ShapedQuantity`
- `scale_out_feasible(asset, held_qty, part_qty, price) -> bool`

**`tradepulse/dbae/baseline.py`:**
- `SessionBaseline` dataclass;
- `load_or_capture_baseline(repositories, calendar, history, account, now) -> SessionBaseline | BaselineRefusal`.

**`tradepulse/broker/capabilities.py`:**
- `AccountEvidence`, `AssetTradingCapabilities`;
- the `AccountCapabilities` and `AssetCapabilities` protocols;
- the Alpaca implementations.

These are separate protocols, not new `BrokerPort` methods (DBAE-03). `BrokerPort` and its A0 conformance are unchanged. The simulated broker implements both protocols for tests.

### 3.2 Known and unknown values

Every capability field is typed `T | None`, and `None` means **unknown**. There are no defaults. An unknown field refuses exactly the asset classes that need it (§5.2) and grants nothing. Unparsable values, wrong types and non-finite decimals are parsed to `None` with a named reason, never to a permissive value.

### 3.3 Freshness and concurrency (DBAE-04)

- The scanner's DBAE result is only a prefilter.
- The gateway re-reads `AccountEvidence` and `AssetTradingCapabilities` inside the portfolio-risk lock, for every non-protective buy.
- Evidence older than `DBAE_EVIDENCE_MAX_AGE_SECONDS = 30`, measured from the response's `received_at`, is refused.
- The funding figure (§7) is computed inside the same lock, and the decision is committed by the lease-fenced `RISK_APPROVED` write.
- No second lock is introduced.

## 4. Bands and the session baseline

### 4.1 Bands

These are TradePulse's own policy thresholds, not broker or regulatory minimums. An unlocked class still needs broker permission, funding, a valid order and approval by the unchanged risk engine.

| Band | Band equity | Stocks and ETFs | Crypto | Options |
|---|---|---|---|---|
| `BELOW_MINIMUM` | < $20 | no new entries | no | no |
| `MICRO` | $20 – $49.99 | fractionable only | no | no |
| `MICRO_PLUS` | $50 – $99.99 | fractionable only | no | no |
| `FRACTIONAL` | $100 – $499.99 | fractionable; whole-share when affordable | yes | no |
| `STANDARD` | $500 – $1,999.99 | all tradable | yes | no |
| `FULL` | ≥ $2,000 | all tradable | yes | yes |

Boundaries are inclusive at the lower edge. `band_for(Decimal("19.99")) == BELOW_MINIMUM` and `band_for(Decimal("20")) == MICRO`.

`$2,000` for options proves nothing about contract affordability or maximum loss. Those remain the risk engine's checks, and the options permission check is in §6.

### 4.2 Band equity

`band_equity = min(current_equity, baseline.previous_close_equity)`, where `current_equity` comes from the fresh `AccountEvidence`.

- **Downgrades** take effect at once: any fresh equity reading in a lower band lowers the band for that entry.
- **Upgrades** happen only through a new session baseline. A gain, deposit or restart during a session never raises the band.

### 4.3 The session baseline (DBAE-S2-R2)

**Which session.** The session date is the current trading date from the Rev.144 calendar. On a non-trading day (weekend or holiday), it is the most recent trading date.

**The baseline.** The baseline for session date *D* records the account's closing equity on the trading date before *D* (`previous_close_equity`). It is read from `GET /v2/account/portfolio/history?timeframe=1D` (the daily bar for that date) and stored write-once in the `dbae_session_baselines` collection, keyed by *D*. It records:
- `previous_close_equity` and the date of the bar used;
- `last_equity` at capture;
- `source` (`portfolio_history`);
- `captured_at`.

**When a baseline is verified.** The baseline for *D* is verified only if all of these hold:
1. The calendar verifies *D* (Rev.144).
2. The portfolio-history bar exists for exactly the previous trading date, and its equity is finite and at least zero.
3. That equity agrees with the account's `last_equity` within $0.01. G1 (§13) confirms that `last_equity` means the previous close; if G1 shows otherwise, this cross-check is removed from the spec before the build.

**Capture and restart.** The baseline is captured at the first non-protective buy decision of session *D* and reused afterwards, including after a restart. Because it describes the *previous* close, the time of capture is irrelevant: starting at noon after a morning deposit still yields yesterday's close.

**When verification fails.** No baseline row is written for *D*.
- If an earlier baseline exists, the band for that entry is `min(band_for(band_equity_now), band of the latest stored baseline)`, where `band_equity_now` uses the latest stored `previous_close_equity`. The reason is `DBAE_SESSION_BASELINE_UNVERIFIED_CAPPED`.
- If no earlier baseline exists, there are no new entries: `DBAE_SESSION_BASELINE_UNVERIFIED`.

**Crypto.** Crypto has no session boundary of its own. On weekends and holidays, crypto entries use the baseline of the most recent equity trading date. Upgrades therefore happen only at an equity trading-day boundary, while downgrades still apply at once (§4.2).

### 4.4 Band-change audit

When an entry's band differs from the band of the last `dbae_band_changed` event, `dbae_band_changed` is written once, with:
- the old band and the new band;
- `band_equity`, `current_equity` and `previous_close_equity`;
- the session date;
- the unlocked classes.

## 5. Account evidence (DBAE-S2-05)

### 5.1 Fields

`AccountEvidence` is parsed from `/v2/account` and `/v2/account/configurations`, both fetched fresh:
- `status`, `trading_blocked`, `account_blocked`, `trade_suspended_by_user`;
- `equity`, `cash`, `buying_power`, `non_marginable_buying_power`, `multiplier`;
- `crypto_status`, `options_trading_level`;
- `suspend_trade`, `closing_transactions_only`, `fractional_trading`;
- `received_at`.

### 5.2 Requirements per entry

Every new entry needs all of the following. Otherwise the entry is refused with `DBAE_ACCOUNT_STATE_UNVERIFIED`, or with the named flag reason if a flag is present but blocking:
- `status == "ACTIVE"`;
- `trading_blocked`, `account_blocked`, `trade_suspended_by_user`, `suspend_trade` and `closing_transactions_only` all `False`;
- `equity`, `cash`, `buying_power` and `multiplier` present.

Requirements that depend on the asset class:

| Entry | Also needs | Reason if missing or not met |
|---|---|---|
| fractional equity quantity | `fractional_trading is True` | `DBAE_FRACTIONAL_TRADING_UNVERIFIED` |
| crypto | `crypto_status == "ACTIVE"`, `non_marginable_buying_power` present | `DBAE_CRYPTO_PERMISSION_UNVERIFIED` |
| option | `options_trading_level >= 2` | `DBAE_OPTIONS_PERMISSION_UNVERIFIED` |

Level 2 is Alpaca's level for buying calls and puts. TradePulse opens only single-leg long options (§2.3). A missing options or crypto field never blocks an otherwise verified equity order.

### 5.3 Cash accounts

`multiplier == 1` is refused with `DBAE_CASH_ACCOUNT_SETTLEMENT_UNVERIFIED`. Buying with unsettled sale proceeds can cause a good-faith violation, and the paper account (a margin account) cannot show which field reports settled cash. This is a named limit, to be lifted by its own evidence later.

## 6. Asset eligibility

`AssetTradingCapabilities` is parsed from a fresh `GET /v2/assets/{symbol}` read (options: the contract endpoint). Its fields are `active`, `tradable`, `fractionable`, `min_order_size`, `min_trade_increment`, `price_increment` and `received_at`, each `T | None`.

An entry needs:
1. The asset class unlocked by the band (§4.1), otherwise `DBAE_ASSET_CLASS_LOCKED`.
2. `active is True` and `tradable is True`, otherwise `DBAE_ASSET_NOT_TRADABLE`.
3. For an equity in the `MICRO` and `MICRO_PLUS` bands: `fractionable is True`, otherwise `DBAE_WHOLE_SHARES_NOT_ALLOWED_IN_BAND`.
4. For a fractional equity quantity in any band: `fractionable is True`, otherwise the quantity is floored to whole shares (§8.2).
5. For crypto: `min_order_size` and `min_trade_increment` present and positive, otherwise `DBAE_CRYPTO_INCREMENT_UNVERIFIED`.

## 7. Funding (DBAE-S2-01, DBAE-S2-R1)

### 7.1 The commitment record

A buy's funding commitment is its trade intent, persisted as `RISK_APPROVED` before submission, under the portfolio-risk lock, by the lease-fenced write. Its `risk_snapshot.dbae.max_funding` records:

`requested_quantity × reference_price × multiplier × (1 + slippage_allowance) + projected_fees`

where `slippage_allowance` is the profile's `slippage_limit_for(asset_class)` as a fraction.

### 7.2 What is still reserved

`pending_buy_funding(repositories) -> Decimal` sums, over every buy intent in `RISK_APPROVED`, `SUBMITTED`, `ACCEPTED`, `PARTIALLY_FILLED` or `SUBMISSION_UNKNOWN`, across all symbols:

`(requested_quantity − attributed_quantity) × reference_price × multiplier × (1 + slippage_allowance) + unpaid projected fees`

- `attributed_quantity` comes from the intent's SettlementEvents, the same ground truth and the same shared bucket helper as RISK-091-01. It is never taken from `filled_quantity`.
- A `CANCELED` or `EXPIRED` intent leaves the set only once the broker has confirmed that terminal state. Its attributed part is then already in cash and holdings.
- A `SUBMISSION_UNKNOWN` intent stays fully reserved until it is resolved.
- The figure is derived from durable records on every evaluation. There is no separate running total to drift or restore after a crash.

### 7.3 Funding available

```
equity / option:  funding_available = min(cash − P, buying_power − P)
crypto:           funding_available = min(cash − P, non_marginable_buying_power − P)
```

where `P = pending_buy_funding`. It is passed to `evaluate_risk` as `available_cash`, replacing raw `account.cash`. `check_cash_sufficiency` then applies its existing 1% buffer and the new order's fees.

- A negative or missing input refuses entries: `DBAE_FUNDING_UNVERIFIED`. A result at or below zero gives the existing `INSUFFICIENT_CASH`.
- **Double counting is one-directional.** Alpaca lowers `buying_power` for open orders and lowers `cash` on fills. Between a broker fill and its attribution, the filled part is counted in both `P` and the broker figure. This only makes the check stricter, it ends at attribution, and it never charges a fill twice once it is attributed. For example, a $10 order with $6 attributed reserves $4, and cash already shows the $6.
- **Not a guarantee.** This bounds TradePulse's own commitments to cash. It does not claim a margin deficit is impossible: maintenance changes, fees and broker-side adjustments remain. Alpaca's pre-trade margin rejection and the existing reconciliation and margin monitoring stay as independent safeguards.

## 8. Sizing, minimums and exits (DBAE-S2-03, DBAE-S2-R3)

### 8.0 The asset minimum

Wherever this spec writes `max($1, min_order_size × price)`, the second term applies only when the asset has a `min_order_size`, which in practice means crypto. Alpaca reports none for equities, and for them the term is $1. A crypto asset whose minimum is unknown is already refused by §6.

### 8.1 Order of checks for a new entry

1. Account evidence (§5).
2. The session baseline and band (§4).
3. Asset eligibility (§6).
4. Funding (§7).
5. `evaluate_risk`, unchanged, with `available_cash = funding_available`.
6. Shaping the quantity (§8.2).
7. Exit feasibility (§8.3).

The first failure refuses the entry. Every reason is recorded (§10).

### 8.2 Shaping the quantity: round down only

- **Equity, fractionable** (and `fractional_trading` true): the engine's 0.001 precision.
- **Equity, not fractionable:** floored to whole shares. If that leaves 0, the entry is refused with `WHOLE_SHARE_UNAFFORDABLE`.
- **Crypto:** floored to a multiple of `min_trade_increment`. If the result is below `min_order_size`, the entry is refused with `DBAE_BELOW_ASSET_MINIMUM`.
- After shaping, the entry must still pass the $1 `min_lot_notional` and the cost-to-risk rule, re-evaluated on the shaped quantity.
- The quantity is never rounded up.

### 8.3 Exit feasibility at entry (DBAE-02)

An entry is allowed only if its protective exit is a supported order:
- **Equity:** a `market` sell of the full held quantity, during regular hours. Fractional exits need `fractionable is True`.
- **Crypto:** a `market` `gtc` sell of the full quantity. The quantity must be at least `min_order_size` and a multiple of `min_trade_increment`, which holds by construction after §8.2.
- **Option:** a whole-contract `market` sell.

**Sub-minimum liquidation.** If the position's value at its stop (`shaped_qty × stop_price × multiplier`) is below `max($1, min_order_size × stop_price)`, the entry depends on the broker accepting a sell below the entry minimum.

`SUB_MINIMUM_FULL_LIQUIDATION` is a recorded broker-evidence constant in `tradepulse/dbae/evidence.py`, with values `"unknown"`, `"verified"` or `"refused"`, following the same pattern as Rev.143's `DIVIDEND_REPRESENTATION`:
- While it is `"unknown"` or `"refused"`, that entry is refused with `DBAE_SUB_MINIMUM_LIQUIDATION_UNVERIFIED`.
- Once G2 (§13) sets it to `"verified"`, the entry proceeds with no extra buffer.

DBAE never promises a position's value at its stop. Gaps can invalidate any such prediction, which is why the evidence is about the broker accepting the sell, not about price.

### 8.4 Scale-outs

The monitor's scale-out is allowed only if `scale_out_feasible` holds:
- the part sold and the part kept are each at least `max($1, min_order_size × price)`;
- for crypto, both parts are multiples of `min_trade_increment`.

Otherwise the scale-out is skipped, and the position stays whole under its stop. This replaces the monitor's current `min_lot_notional`-only test (`monitor/coordinator.py`) with the same rule plus the asset's own minimum and increment.

### 8.5 Small residuals

A held position whose market value falls below $1 raises `DBAE_RESIDUAL_BELOW_ENTRY_MINIMUM` (warning, once per asset per session). Its protective exit is still a full-quantity sell, and no entry-only minimum ever blocks it.

### 8.6 What the $20 cases look like, until G2 is verified

- **`micro`:** risk $0.20 at a 6% stop gives about $3.33 (the 20% cap is $4). That is about $3.13 at the stop, so the entry can proceed.
- **`conservative`:** risk $0.05 at a 5% stop gives $1.00 (the cap is also $1.00). That is $0.95 at the stop, so the entry is refused with `DBAE_SUB_MINIMUM_LIQUIDATION_UNVERIFIED`.
- **`balanced`:** risk $0.06 at an 8% stop gives $0.75. It is refused by the existing `INSUFFICIENT_CAPACITY_FOR_MINIMUM_LOT`, before DBAE's own check.

TradePulse does not claim that every profile can trade at $20. It claims that every $20 case either places a fully valid trade or records a named refusal.

## 9. Protective exits (DBAE-02, DBAE-S2-04)

- Exits skip only DBAE's band and entry rules (§4–§8.3). They still pass the session check, the Rev.140 sellable bound, the canonical order controls and broker authorization.
- A protective exit takes no portfolio-risk lock and no funding check, as today.

**Rejection.** On a definitive broker rejection (`is_definitive_rejection`):
- the holding and its stop obligation remain;
- `PROTECTIVE_EXIT_REJECTED` alerts at critical severity, with the broker's reason;
- the next monitor cycle may submit a new exit, after re-evaluating the evidence.

**Ambiguity.** On an ambiguous outcome, the existing `_recover_unknown_submission` path applies: lookup by client order id, then `SUBMISSION_UNKNOWN`, with the asset blocked. A new exit is submitted only once the original order is proven one of:
- rejected;
- never received;
- terminal, with its fills attributed.

There is never a second sell while the first is unresolved.

**Never assumed closed.** The position is never assumed closed. Only attributed sell fills reduce it.

## 10. Refusal reasons and failure handling

Every refusal:
- is stored as the trade intent's `rejection_reason` (several reasons joined in check order);
- writes `dbae_entry_refused` with the reason, band, band equity, asset class and evidence ages.

Refusals never block exits, cancel orders or change holdings.

| Reason | Cause |
|---|---|
| `DBAE_BELOW_MINIMUM_BALANCE` | band `BELOW_MINIMUM` |
| `DBAE_ACCOUNT_STATE_UNVERIFIED` | a core field missing or unparsable, or evidence stale |
| `DBAE_ACCOUNT_TRADING_BLOCKED` | `trading_blocked`, `account_blocked`, `trade_suspended_by_user` or `suspend_trade` |
| `DBAE_ACCOUNT_CLOSING_ONLY` | `closing_transactions_only` |
| `DBAE_ACCOUNT_NOT_ACTIVE` | `status != ACTIVE` |
| `DBAE_CASH_ACCOUNT_SETTLEMENT_UNVERIFIED` | `multiplier == 1` |
| `DBAE_SESSION_BASELINE_UNVERIFIED` | no verified baseline and none stored |
| `DBAE_SESSION_BASELINE_UNVERIFIED_CAPPED` | an informational reason when the band is capped at the last stored baseline (the entry may proceed) |
| `DBAE_ASSET_CLASS_LOCKED` | class not unlocked by the band |
| `DBAE_ASSET_NOT_TRADABLE` | `active` or `tradable` not `True` |
| `DBAE_WHOLE_SHARES_NOT_ALLOWED_IN_BAND` | non-fractionable equity in `MICRO` or `MICRO_PLUS` |
| `DBAE_FRACTIONAL_TRADING_UNVERIFIED` | fractional quantity without verified account fractional trading |
| `DBAE_CRYPTO_PERMISSION_UNVERIFIED` | §5.2 |
| `DBAE_CRYPTO_INCREMENT_UNVERIFIED` | §6 |
| `DBAE_OPTIONS_PERMISSION_UNVERIFIED` | §5.2 |
| `DBAE_FUNDING_UNVERIFIED` | a funding input missing or negative |
| `WHOLE_SHARE_UNAFFORDABLE` | floor to whole shares leaves 0 |
| `DBAE_BELOW_ASSET_MINIMUM` | crypto below `min_order_size` after flooring |
| `DBAE_SUB_MINIMUM_LIQUIDATION_UNVERIFIED` | §8.3 |

A broker read that fails (timeout, 5xx or malformed data) is treated as unknown evidence, never as permission, and the entry is skipped for that cycle.

## 11. Audit

- **`dbae_band_changed`** (§4.4).
- **`dbae_entry_refused`** (§10).
- **`dbae_session_baseline_captured`**, with the session date, `previous_close_equity`, bar date, `last_equity` and the agreement result. **`dbae_session_baseline_unverified`**, with the failing check.
- **`risk_snapshot.dbae`** on every approved buy, holding:
  - band, band equity, session date, baseline id;
  - `funding_available`, `pending_buy_funding`, `max_funding`;
  - shaped quantity and shaping rule;
  - exit-feasibility result;
  - evidence `received_at` times.

  Every decision can be replayed from this record.
- **Alerts:** `PROTECTIVE_EXIT_REJECTED` (critical) and `DBAE_RESIDUAL_BELOW_ENTRY_MINIMUM` (warning), each through `_alert_once`.

## 12. Retiring the equity ladder

- `TRADEPULSE_AUTO_RISK_PROFILE_BY_EQUITY` is removed.
- If it is set in the environment (any value), settings parsing raises a migration error: "TRADEPULSE_AUTO_RISK_PROFILE_BY_EQUITY is retired; DBAE adapts order mechanics to balance and the risk profile is always TRADEPULSE_RISK_PROFILE. Remove the variable."
- `EQUITY_PROFILE_LADDER`, `EQUITY_PROFILE_LADDER_TOP_PROFILE_ID` and `cli._resolve_risk_profile_id` are deleted. Callers use `risk_limits_for_profile(settings.risk_profile)`.
- `.env.example` and README are updated.

DBAE has no on/off flag. It replaces the ladder, and every entry passes through it.

## 13. Activation gates

| Gate | What | Authorization |
|---|---|---|
| G1 | Read-only probes: `/v2/account/portfolio/history?timeframe=1D` shape and bar dates. Confirm `last_equity` equals the previous trading date's closing bar. Evidence saved with no identifiers. | Read only. Runs before the build. |
| G2 | Paper sub-$1 liquidation: buy ≈ $1.10 of one fractionable stock (notional market, regular hours). Sell ≈ $0.50 of it by quantity. Sell the remainder by quantity. All through the gateway or an approved direct order, with responses and fills saved. The account ends flat. | **The operator approves each exact order beforehand.** Never during a verification run. Paper behaviour may differ from live; the record says so. |
| G3 | Full test suite green; mypy zero over the package; Ruff with no new findings. | — |

G2's outcome sets `SUB_MINIMUM_FULL_LIQUIDATION` (`"verified"` if both sub-$1 sells fill, `"refused"` if Alpaca rejects either). G2 is not required for the build; until it passes, §8.3 refuses the entries that depend on it.

## 14. Tests

All tests are deterministic: simulated broker data, a fixed clock and the Rev.144 test calendar.

1. **Balances.** $20, $25, $50, $99.99, $100, $500, $1,999.99 and $2,000, each under all four profiles and for each asset class. Each asserts the band, the unlocked classes, and either the approved quantity or the exact refusal. Also checked: `band_for` at $19.99 and $20, and no case approved past an `evaluate_risk` refusal.
2. **Funding.**
   - Two concurrent buys on different symbols against $20 cash: the sum of `max_funding` never exceeds the funding available.
   - A $10 order with $6 attributed reserves exactly $4 (plus allowance), and cash already shows the $6.
   - An ambiguous buy stays fully reserved until it is resolved.
   - A cancel after a partial fill releases only the unfilled part, after the terminal state is confirmed.
   - A fee correction changes the reservation exactly once.
   - A crash between `RISK_APPROVED` and submission: the restart derives the same reservation.
   - Crypto uses `non_marginable_buying_power`.
   - A missing or negative input gives `DBAE_FUNDING_UNVERIFIED`.
3. **Baseline.**
   - A gain during the session gives no upgrade.
   - A deposit then a restart gives no upgrade.
   - A $50 close with a +$500 deposit and a first start at noon stays `MICRO_PLUS`.
   - A loss during the session downgrades at once.
   - Portfolio history disagreeing with `last_equity` gives the capped result, or a refusal when no baseline is stored.
   - The first session after a holiday uses the close of the last trading date.
   - A crypto entry on Saturday uses Friday's session baseline.
   - Write-once under concurrent capture.
4. **Account restrictions.**
   - Each blocking flag on its own refuses entries and leaves a protective exit able to proceed.
   - Missing options fields block only options, and missing crypto fields block only crypto.
   - A cash account is refused.
   - Stale evidence is refused.
5. **Minimums and exits.**
   - A non-fractionable equity is floored; a result of 0 gives `WHOLE_SHARE_UNAFFORDABLE`.
   - Crypto is floored to the increment; below the minimum gives `DBAE_BELOW_ASSET_MINIMUM`.
   - `MICRO` refuses a whole-share-only stock.
   - A stop value below $1 is refused while the constant is `"unknown"`, and allowed when it is `"verified"`.
   - A scale-out leaving a part too small is skipped; a crypto scale-out off the increment is skipped.
   - A residual below $1 alerts once, and its full exit is still submitted.
6. **Exit recovery.**
   - A definitive rejection keeps the obligation and alerts critically; the next cycle retries.
   - A timeout where the order is later found continues with no second order.
   - A timeout where the order is never found gives `SUBMISSION_UNKNOWN` and no second sell across cycles and a restart.
   - Reconciliation proves the order terminal, after which a new exit is allowed.
7. **Retired setting.** Setting `TRADEPULSE_AUTO_RISK_PROFILE_BY_EQUITY` (any value) fails settings parsing with the migration error. The ladder symbols no longer exist.
8. **Property test.** Over random balances, prices, minimums and pending sets: shaped quantity ≤ approved quantity; total reserved ≤ funding; no approval when any required field is `None`.

## 15. Verification-run compatibility

- The paper account (about $100k) is in `FULL`. For the 24 h run, DBAE changes only:
  - the funding check (§7), which is stricter;
  - the new audit events and `risk_snapshot.dbae`.
- The run's opening position (the 1-share buy) is taken before the cursor bootstrap and is unaffected.
- DBAE is built in a worktree and never edited during a run. G2 never runs during a run.

## 16. Review trail

- **Section 1 (approach):** a pure policy module plus separate capability protocols, approved with DBAE-01 to DBAE-04.
- **Section 2:** approved with DBAE-S2-01 to S2-05, then DBAE-S2-R1 to R3 and the funding-concurrency finding (§2.3, §7).
- **Operator decisions:**
  - retire the ladder;
  - gradual unlocks;
  - scan cost not in DBAE;
  - "keep one day trade in reserve" superseded by the PDT retirement evidence (§2.2) and approved by the reviewer.
