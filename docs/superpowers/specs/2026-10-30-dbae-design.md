# DBAE: Dynamic Balance-Aware Execution (Alpaca)

Status: design for review, rev. 4 (2026-10-30). No production code changes are made until this spec is approved. Changes from rev. 1 are listed in §17.

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
3. That equity agrees with the account's `last_equity` within $0.01.

**Mapping a bar to its trading date (G1, §13.1).** A daily bar's `timestamp` is 00:00 UTC on the day after the trading date. Its trading date is the New York calendar date of that timestamp. For example, the bar at `2026-10-30T00:00:00Z` is New York 2026-10-29 20:00, so it is trading date 2026-10-29.
- The bar used must be the one whose mapped date equals the previous trading date from the Rev.144 calendar.
- A bar for the current session, if one appears, is never used.

**Before Alpaca updates `last_equity`.** If a capture happens before Alpaca has rolled `last_equity` over (for example, pre-market), the check in 3 fails. The entry is refused and capture is retried at the next buy decision, as in "When verification fails" below. It never falls back to an older baseline.

**Capture and restart.** The baseline is captured at the first non-protective buy decision of session *D* and reused afterwards, including after a restart. Because it describes the *previous* close, the time of capture is irrelevant: starting at noon after a morning deposit still yields yesterday's close.

**When verification fails (DBAE-R1-02).**
- No baseline row is written for *D*, and there are no new entries in session *D*: `DBAE_SESSION_BASELINE_UNVERIFIED`.
- An older stored baseline never authorises an entry in a later session. There is no fallback path.
- Capture is retried at the next non-protective buy decision. A baseline verified later in the session takes effect from then on.
- **New accounts.** An account with no closing bar for the previous trading date (for example, funded today) cannot open entries until the next session. This is deliberate: there is no evidence from before the session.
- Protective exits never depend on a baseline (§9).

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
- `equity`, `cash`, `buying_power`, `non_marginable_buying_power`;
- `account_multiplier`, parsed from Alpaca's account `multiplier`. This is the account's buying-power classification and is never used in order notional (§7.2);
- `crypto_status`, `options_trading_level`;
- `suspend_trade`, `closing_transactions_only`, `fractional_trading`;
- `received_at`.

### 5.2 Requirements per entry

Every new entry needs all of the following. Otherwise the entry is refused with `DBAE_ACCOUNT_STATE_UNVERIFIED`, or with the named flag reason if a flag is present but blocking:
- `status == "ACTIVE"`;
- `trading_blocked`, `account_blocked`, `trade_suspended_by_user`, `suspend_trade` and `closing_transactions_only` all `False`;
- `equity`, `cash`, `buying_power` and `account_multiplier` present.

Requirements that depend on the asset class:

| Entry | Also needs | Reason if missing or not met |
|---|---|---|
| fractional equity quantity | `fractional_trading is True` | `DBAE_FRACTIONAL_TRADING_UNVERIFIED` |
| crypto | `crypto_status == "ACTIVE"`, `non_marginable_buying_power` present | `DBAE_CRYPTO_PERMISSION_UNVERIFIED` |
| option | `options_trading_level >= 2` | `DBAE_OPTIONS_PERMISSION_UNVERIFIED` |

Level 2 is Alpaca's level for buying calls and puts. TradePulse opens only single-leg long options (§2.3). A missing options or crypto field never blocks an otherwise verified equity order.

### 5.3 Account type

**Accepted values.** Only `account_multiplier` values of 2 or 4, which Alpaca uses for margin accounts, are accepted.

**`account_multiplier == 1` is refused** with `DBAE_ACCOUNT_TYPE_UNVERIFIED`.
- Alpaca uses 1 for limited-margin accounts as well as cash accounts, so on its own it does not prove an account is cash-only. DBAE does not try to classify it.
- The refusal stands until the account type and its settled-funds rules are verified by their own evidence. Buying with unsettled sale proceeds in a cash account can cause a good-faith violation, and the paper account (multiplier 4) cannot show which field reports settled cash.
- This is a named limitation of the initial rollout. It may exclude some genuine $20 accounts.

**Any other value** (missing, 0, 3, or unparsable) gives `DBAE_ACCOUNT_STATE_UNVERIFIED`.

## 6. Asset eligibility

`AssetTradingCapabilities` is parsed from a fresh `GET /v2/assets/{symbol}` read (options: the contract endpoint). Its fields are `active`, `tradable`, `fractionable`, `min_order_size`, `min_trade_increment`, `price_increment` and `received_at`, each `T | None`.

An entry needs:
1. The asset class unlocked by the band (§4.1), otherwise `DBAE_ASSET_CLASS_LOCKED`.
2. `active is True` and `tradable is True`, otherwise `DBAE_ASSET_NOT_TRADABLE`.
3. For an equity in the `MICRO` and `MICRO_PLUS` bands: `fractionable is True`, otherwise `DBAE_WHOLE_SHARES_NOT_ALLOWED_IN_BAND`.
4. For a fractional equity quantity in any band: `fractionable is True`, otherwise the quantity is floored to whole shares (§8.2).
5. For crypto: `min_order_size` and `min_trade_increment` present and positive, otherwise `DBAE_CRYPTO_INCREMENT_UNVERIFIED`.

## 7. Funding (DBAE-S2-01, DBAE-S2-R1)

### 7.1 Quantities (DBAE-R1-01)

Every approved buy records four distinct quantities, each under its own name, in `risk_snapshot.dbae` (DBAE-R2-01):

| Field | Meaning |
|---|---|
| `original_requested_quantity` | the strategy's original request |
| `risk_approved_quantity` | what the first `evaluate_risk` run approved |
| `shaped_quantity` | after §8.2's round-down |
| `submitted_quantity` | the quantity durably authorised for the broker order |

**Written once.** All four are written together, by the lease-fenced `RISK_APPROVED` write, and are never rewritten. A later status update copies `risk_snapshot` unchanged, as today.

**The intent's own `requested_quantity` field.**
- The gateway already overwrites this field with the risk-approved quantity at `RISK_APPROVED` (`gateway.py`, the `approved = replace(...)` write), and submits it. DBAE keeps that field rather than disturbing the gateway, but defines it: from `RISK_APPROVED` on, the intent's `requested_quantity` **is the executable submitted quantity**, and it equals `dbae.submitted_quantity`.
- Before `RISK_APPROVED`, it holds the original request.
- The existing snapshot keys `requested_quantity` and `approved_quantity` keep their current values and meaning, for the audit records already written. DBAE code never reads them; it reads only the four named fields.

**Who reads which field.**
- Broker submission, `order_matches_intent`, `_recover_unknown_submission`, `_poll_and_settle` and fill attribution read the intent's `requested_quantity`, as today. That is the submitted quantity.
- `pending_buy_funding` (§7.4) and `max_funding` (§7.2) use `dbae.submitted_quantity`, and check that it equals the intent's field.
- Audit and replay use the four named fields.

**A mismatch** between the intent field and `dbae.submitted_quantity` is an integrity fault. Funding reserves the larger of the two, a critical `DBAE_QUANTITY_IDENTITY_MISMATCH` alert fires once per intent, and new entries are refused until the operator resolves it.

**Older intents.** An intent from before DBAE (no `dbae` block) is reserved on its `requested_quantity`, which is what was submitted.

The shaped and submitted quantities are equal by construction. The original request never reserves funding.

**Re-approval after shaping.** When shaping changes the quantity, `evaluate_risk` runs again on the same snapshot, inside the same lock, with `requested_quantity = shaped_quantity`.
- The result must approve exactly the shaped quantity, otherwise the entry is refused with `DBAE_SHAPED_QUANTITY_NOT_REAPPROVED`.
- Rounding down cannot break the caps, but it can break the $1 minimum lot and the cost-to-risk rule, which the second run catches.

### 7.2 The commitment record

A buy's funding commitment is its trade intent, persisted as `RISK_APPROVED` before submission, under the portfolio-risk lock, by the lease-fenced write. Its `risk_snapshot.dbae.max_funding` records:

`submitted_quantity × reference_price × contract_multiplier × (1 + slippage_allowance) + projected_fees`

where `slippage_allowance` is the profile's `slippage_limit_for(asset_class)` as a fraction.

**`contract_multiplier` (DBAE-R3-01)** is the instrument's units per quantity. It always comes from `contract_multiplier_of(asset)` (`models/market.py`), the system's single source for converting quantity × price to dollars. That gives:
- 1 for equities, whole or fractional, and for crypto;
- for an option, the multiplier on Alpaca's contract record (`providers/alpaca_market_data.py` stores `contract.multiplier`), never an assumed 100. An option without one raises and is refused, as today.

The account's `account_multiplier` (Alpaca's `multiplier`, for example 4) is a buying-power classification. It is **never** used in any order-notional, funding, reservation or exit-value calculation. A test pins this: with `account_multiplier = 4`, a $10 equity order reserves about $10 plus its allowance and fees, not $40.

For example, a request for $20 that is approved and shaped to $3.50 reserves about $3.50 plus its allowance and fees, never $20.

### 7.3 Funding components and their sources (DBAE-R1-03)

| Component | Source | Changes when |
|---|---|---|
| `cash` | broker, `/v2/account` | Alpaca lowers it on a fill; see G2 below |
| `buying_power` (crypto: `non_marginable_buying_power`) | broker, `/v2/account` | Alpaca lowers it for open orders and fills; see G2 below |
| attributed quantity | TradePulse SettlementEvents | a validated fill is attributed |
| outstanding commitment `P` | TradePulse intents (§7.4) | an intent is approved, gets a fill attributed, or reaches a broker-confirmed terminal state |
| projected fees | the existing fee estimator (`_FEE_PCT_PER_SIDE` for crypto; zero commission and the regulatory sell-side fees for equities) | an actual fee is attributed, which replaces the estimate |

**What DBAE never does.** It never releases any part of `P` because a broker figure changed. The only things that reduce `P` are an attributed fill, or a terminal state confirmed by the broker.

**Broker behaviour to confirm.** The "changes when" column for `cash` and `buying_power` is Alpaca's documented behaviour, not yet observed on this account. G2 (§13) records the account before submission, after acceptance and after each fill, and the record states the observed behaviour.
- If G2 shows `cash` drops on acceptance rather than on fill, the subtraction stays as it is. It becomes stricter, never looser, and the spec records the observation.
- No relaxation of §7.5 is made without that evidence and a new Rev.

### 7.4 What is still reserved

`pending_buy_funding(repositories) -> Decimal` sums, over every buy intent in `RISK_APPROVED`, `SUBMITTED`, `ACCEPTED`, `PARTIALLY_FILLED` or `SUBMISSION_UNKNOWN`, across all symbols:

`(submitted_quantity − attributed_quantity) × reference_price × contract_multiplier × (1 + slippage_allowance) + unattributed projected fees`

- `attributed_quantity` comes from the intent's SettlementEvents, the same ground truth and the same shared bucket helper as RISK-091-01. It is never taken from `filled_quantity`.
- A `CANCELED` or `EXPIRED` intent leaves the set only once the broker has confirmed that terminal state. Its attributed part is then already in cash and holdings.
- A `SUBMISSION_UNKNOWN` intent stays fully reserved until it is resolved.
- The figure is derived from durable records on every evaluation. There is no separate running total to drift or restore after a crash.

### 7.5 Funding available

```
equity / option:  funding_available = min(cash − P, buying_power − P)
crypto:           funding_available = min(cash − P, non_marginable_buying_power − P)
```

where `P = pending_buy_funding`. It is passed to `evaluate_risk` as `available_cash`, replacing raw `account.cash`. `check_cash_sufficiency` then applies its existing 1% buffer and the new order's fees.

- A negative or missing input refuses entries: `DBAE_FUNDING_UNVERIFIED`. A result at or below zero gives the existing `INSUFFICIENT_CASH`.
- **Where overlap occurs, it only ever makes the check stricter.**
  - **`buying_power` and accepted orders:** Alpaca's figure already excludes an accepted, unfilled order, and `P` excludes it again. So that order counts twice in the `buying_power` term until it fills and is attributed. For a market order in regular hours, that is seconds.
  - **Between a fill and its attribution:** the filled part is in both `P` and the broker's lowered `cash`. This ends at attribution.
  - **After attribution:** a fill is never charged twice. For example, a $10 order with $6 attributed reserves $4, and `cash` already shows the $6.
  - DBAE deliberately accepts this transient over-count instead of releasing reservations based on unverified broker semantics (§7.3).
- **Small accounts.** Below $2,000 equity, Reg T allows no margin, so Alpaca's so `buying_power` is close to `cash`. While an order is open, the `buying_power` term can bind at about `cash − 2 × order`. This delays a second entry until the first fills; it never approves an unfunded one.
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
7. Re-approval of the shaped quantity (§7.1).
8. Exit feasibility (§8.3), on the shaped quantity.
9. The lease-fenced `RISK_APPROVED` write, which stores the four quantities (§7.1) and `max_funding` (§7.2).

The first failure refuses the entry. Every reason is recorded (§10).

### 8.2 Shaping the quantity: round down only

- **Equity, fractionable** (and `fractional_trading` true): the engine's 0.001 precision.
- **Equity, not fractionable:** floored to whole shares. If that leaves 0, the entry is refused with `WHOLE_SHARE_UNAFFORDABLE`.
- **Crypto:** floored to a multiple of `min_trade_increment`. If the result is below `min_order_size`, the entry is refused with `DBAE_BELOW_ASSET_MINIMUM`.
- After shaping, every risk limit is re-checked by the second `evaluate_risk` run (§7.1).
- The quantity is never rounded up.

### 8.3 Exit feasibility at entry (DBAE-02)

An entry is allowed only if its protective exit is a supported order:
- **Equity:** a `market` sell of the full held quantity, during regular hours. Fractional exits need `fractionable is True`.
- **Crypto:** a `market` `gtc` sell of the full quantity. The quantity must be at least `min_order_size` and a multiple of `min_trade_increment`, which holds by construction after §8.2.
- **Option:** a whole-contract `market` sell.

**Sub-minimum liquidation.** If the position's value at its stop (`shaped_qty × stop_price × contract_multiplier`) is below `max($1, min_order_size × stop_price)`, the entry depends on the broker accepting a sell below the entry minimum.

**Scoped evidence (DBAE-R2-02).** Sub-minimum liquidation evidence is a recorded table in `tradepulse/dbae/evidence.py`, `SUB_MINIMUM_LIQUIDATION_EVIDENCE: Mapping[LiquidationScope, LiquidationVerdict]`, following the pattern of Rev.143's `DIVIDEND_REPRESENTATION`.

`LiquidationScope` is a frozen dataclass with these fields:

| Field | Values |
|---|---|
| `broker_environment` | `"alpaca-paper"` or `"alpaca-live"` |
| `account_type` | `"margin"` (cash accounts are refused by §5.3) |
| `asset_class` | `equity`, `crypto` or `option` |
| `quantity_kind` | `"fractional"` or `"whole"` |
| `order_type` | `"market"` |
| `time_in_force` | `"day"` or `"gtc"` |
| `session` | `"regular"` |
| `minimum_kind` | `"notional_below_1_usd"` (equity) or `"notional_below_1_usd_qty_at_or_above_min_order_size"` (crypto) |

`LiquidationVerdict` is `"verified"` or `"refused"`, with the evidence path and manifest digest.

How it is used:
- An entry whose stop value falls below the threshold looks up the exact scope its protective exit would use. If there is no entry for that scope, or the entry is `"refused"`, it is refused with `DBAE_SUB_MINIMUM_LIQUIDATION_UNVERIFIED`. Only an exact `"verified"` match lets it proceed, with no extra buffer.
- There is no wildcard, no inheritance between scopes, and no paper-to-live inheritance.
- The table starts empty.
- G2 can add at most one row: `alpaca-paper / margin / equity / fractional / market / day / regular / notional_below_1_usd`. It never covers crypto, options, whole-share quantities, other order types, outside regular hours, or a live account.

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

**Below the entry minimum.** When the account falls below $20 (`BELOW_MINIMUM`), or into any band that would not unlock the held asset class, every existing position keeps full protective management:
- stops, the ratchet, the time stop and full exits are unaffected;
- band membership is read only by new entries.

A scale-out is still governed only by §8.4.

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
| `DBAE_ACCOUNT_TYPE_UNVERIFIED` | `account_multiplier == 1` (limited-margin or cash; §5.3) |
| `DBAE_SESSION_BASELINE_UNVERIFIED` | no verified baseline for the current session (§4.3) |
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
| `DBAE_SHAPED_QUANTITY_NOT_REAPPROVED` | the second `evaluate_risk` run did not approve exactly the shaped quantity (§7.1) |

A broker read that fails (timeout, 5xx or malformed data) is treated as unknown evidence, never as permission, and the entry is skipped for that cycle.

## 11. Audit

- **`dbae_band_changed`** (§4.4).
- **`dbae_entry_refused`** (§10).
- **`dbae_session_baseline_captured`**, with the session date, `previous_close_equity`, bar date, `last_equity` and the agreement result. **`dbae_session_baseline_unverified`**, with the failing check.
- **`risk_snapshot.dbae`** on every approved buy, holding:
  - band, band equity, session date, baseline id;
  - `funding_available`, `pending_buy_funding`, `max_funding`;
  - the broker figures used (`cash`, `buying_power` or `non_marginable_buying_power`) with their `received_at`;
  - `shaped_quantity`, the shaping rule, and the re-approval result (§7.1), alongside the existing `requested_quantity` and `approved_quantity`;
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
| G1 | Read-only probes of `/v2/account/portfolio/history?timeframe=1D` and `last_equity`. **Done 2026-10-30; see §13.1.** A repeat (G1b) is taken read-only on the first session day after the build, on an account that is not flat. | Read only. |
| G2 | Paper sub-$1 liquidation and funding behaviour. Buy ≈ $1.10 of one fractionable stock (notional market, regular hours). Sell ≈ $0.50 of it by quantity. Sell the remainder by quantity. All through the gateway or an approved direct order, with order responses and fills saved. A read-only account snapshot (`cash`, `buying_power`, `non_marginable_buying_power`) is saved before each submission, after acceptance and after each fill, to establish §7.3's broker behaviour. The account ends flat. | **The operator approves each exact order beforehand.** Never during a verification run. Paper behaviour may differ from live; the record says so. |
| G3 | Full test suite green; mypy zero over the package; Ruff with no new findings. | — |

G2's outcome adds its single scope row to `SUB_MINIMUM_LIQUIDATION_EVIDENCE` (§8.3): `"verified"` if both sub-$1 sells fill, `"refused"` if Alpaca rejects either. G2 is not required for the build; until it passes, §8.3 refuses the entries that depend on it.

### 13.1 G1 result (2026-10-30, 10:49 ET, regular session)

Evidence: `~/tradepulse-evidence/2026-10-30-dbae-g1/` (manifest sha256 prefix `8c687d283405b346`, no identifiers).

**Response shape.** `GET /v2/account/portfolio/history?period=1M&timeframe=1D` returns `timestamp`, `equity`, `profit_loss`, `profit_loss_pct`, `base_value`, `base_value_asof` and `timeframe`. The default, `intraday_reporting=market_hours` and `intraday_reporting=extended_hours` return identical daily bars.

**Bar dates.** Daily bars are stamped 00:00 UTC on the day after each trading date:
- the bars present are UTC 10-21, 22, 23, 24, 27, 28, 29 and 30;
- this maps to trading dates 10-20, 21, 22, 23, 26, 27, 28 and 29;
- there are no bars for weekends;
- there is no bar for the current session (10-30) during it.

**Agreement.** `last_equity` = 101480.03, exactly the bar for trading date 10-29. The 10-28 bar is 101422.20, which shows `last_equity` had rolled over to the previous close, not to an earlier one.

**Limitation.** The account was flat (it was closed 10-29), so current equity also equalled `last_equity`. This observation cannot tell "previous close" apart from "current equity". The 10-28/10-29 difference shows the rollover, but only one instant was observed, and the pre-market rollover time was not. Hence:
- G1b, on a day the account holds positions;
- the fail-closed retry in §4.3.

**Decision.** The §4.3 contract stands as written: the bar mapped by New York date is primary, and `last_equity` is the cross-check within $0.01. If G1b shows `last_equity` disagreeing with the mapped bar during a session, the spec is revised before DBAE is activated (§15).

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
   - **Contract multiplier vs account multiplier:** with `account_multiplier = 4`, a $10 equity order's `max_funding` is $10 × (1 + allowance) + fees, not $40. The same holds for a crypto order.
   - An option with contract multiplier 100 reserves 100 × premium × quantity. An option whose contract record has a non-standard multiplier (for example 10) uses that value. An option with no contract multiplier is refused.
   - A static check (an AST test, as in Rev.142's T26) asserts that no `dbae/` or funding code reads `account_multiplier` except §5.3's account-type check.
   - A missing or negative input gives `DBAE_FUNDING_UNVERIFIED`.
   - **Request vs authorised quantity:** a $20 request risk-approved at $3.50 reserves the $3.50 order's obligation only. `risk_snapshot` holds all four quantities, and the submitted quantity equals the shaped one.
   - **Shaping:** a quantity floored by the increment has `max_funding` computed on the floored quantity. A floor that breaks the $1 minimum lot or the cost rule gives `DBAE_SHAPED_QUANTITY_NOT_REAPPROVED`.
   - **Partial fill with a lowered broker figure:** a partly filled order with `buying_power` already reduced releases nothing that isn't attributed, and the attributed part never stays in `P`.
   - **Restart after an ambiguous buy:** the same `P`, and no second buy for that intent.
   - **Quantity identity:** a buy with request 10, risk-approved 7.3 and shaped and submitted 7 is persisted, the process restarts, and the intent is reloaded through `hydrate`. All four `dbae` fields are distinct and unchanged. The intent's `requested_quantity` is 7. Recovery and `order_matches_intent` use 7. `pending_buy_funding` uses 7. A status update (accepted, then partly filled) leaves the four fields byte-identical.
   - **Identity mismatch:** an intent whose field disagrees with `dbae.submitted_quantity` reserves the larger, alerts `DBAE_QUANTITY_IDENTITY_MISMATCH` once, and refuses new entries.
3. **Baseline.**
   - A gain during the session gives no upgrade.
   - A deposit then a restart gives no upgrade.
   - A $50 close with a +$500 deposit and a first start at noon stays `MICRO_PLUS`.
   - A loss during the session downgrades at once.
   - Bar mapping: a bar at `2026-10-30T00:00:00Z` is trading date 10-29. A Monday session uses the bar stamped Saturday 00:00 UTC (Friday's trading date). A bar for the current session is ignored.
   - A pre-market capture before `last_equity` has rolled over is refused, and the same session's retry succeeds once they agree.
   - Portfolio history disagreeing with `last_equity`, or a missing previous-date bar, refuses entries for the session, even with an older stored baseline.
   - A baseline verified later in the same session allows entries from then on.
   - A new account with no previous close is refused until the next session.
   - The first session after a holiday uses the close of the last trading date.
   - A crypto entry on Saturday uses Friday's session baseline.
   - Write-once under concurrent capture.
4. **Account restrictions.**
   - Each blocking flag on its own refuses entries and leaves a protective exit able to proceed.
   - Missing options fields block only options, and missing crypto fields block only crypto.
   - `account_multiplier == 1` gives `DBAE_ACCOUNT_TYPE_UNVERIFIED`; 3, 0 or missing gives `DBAE_ACCOUNT_STATE_UNVERIFIED`; 2 and 4 pass.
   - Stale evidence is refused.
5. **Minimums and exits.**
   - A non-fractionable equity is floored; a result of 0 gives `WHOLE_SHARE_UNAFFORDABLE`.
   - Crypto is floored to the increment; below the minimum gives `DBAE_BELOW_ASSET_MINIMUM`.
   - `MICRO` refuses a whole-share-only stock.
   - A stop value below $1 is refused with an empty evidence table.
   - It is allowed for an equity fractional market day order on paper once that exact scope is `"verified"`.
   - With only the G2 equity row present, it is still refused for: crypto; a whole-share equity; an option; the same equity scope on `alpaca-live`.
   - A `"refused"` row refuses.
   - A scale-out leaving a part too small is skipped; a crypto scale-out off the increment is skipped.
   - A residual below $1 alerts once, and its full exit is still submitted.
6. **Exit recovery.**
   - A definitive rejection keeps the obligation and alerts critically; the next cycle retries.
   - A timeout where the order is later found continues with no second order.
   - A timeout where the order is never found gives `SUBMISSION_UNKNOWN` and no second sell across cycles and a restart.
   - Reconciliation proves the order terminal, after which a new exit is allowed.
   - With equity dropped below $20, the stop exit of an existing position is submitted, and no DBAE reason appears.
7. **Retired setting.** Setting `TRADEPULSE_AUTO_RISK_PROFILE_BY_EQUITY` (any value) fails settings parsing with the migration error. The ladder symbols no longer exist.
8. **Activation.**
   - Binding or resuming a generation frozen without `dbae_policy_version` gives `generation_predates_dbae`.
   - Changing a band threshold after the freeze gives `frozen_configuration_mismatch`.
9. **Small accounts with G2 unverified.** At $20 under `conservative`, the entry is refused with `DBAE_SUB_MINIMUM_LIQUIDATION_UNVERIFIED`, nothing is submitted, and no limit is changed to force it.
10. **Property test.** Over random balances, prices, minimums and pending sets: shaped quantity ≤ approved quantity; total reserved ≤ funding; no approval when any required field is `None`.

## 15. Verification activation (DBAE-R1-04)

DBAE changes the executable decision path: new account fields, session baselines, fresh asset reads, shaping with re-approval, and stricter funding. It is therefore treated as a new release, never as a change inside a running generation.

**What already exists.** A verification generation freezes:
- the protected source files (`freeze_source`, an aggregate sha256);
- the settings context (`context_for`).

A later mismatch fails with `frozen_configuration_mismatch`. A runtime with DBAE source cannot continue a generation frozen without it.

**What DBAE adds.**
- The frozen `policy` records `dbae_policy_version = "dbae-1"`, the band thresholds, `DBAE_EVIDENCE_MAX_AGE_SECONDS` and the full `SUB_MINIMUM_LIQUIDATION_EVIDENCE` table. A change to any of them is a configuration mismatch.
- A runtime with DBAE refuses to bind or resume a generation whose frozen policy has no `dbae_policy_version`: `generation_predates_dbae`. That prevents DBAE from entering an existing generation, even one that is reopened.
- No migration converts an existing generation.

**Release order:**
1. DBAE merged.
2. G3 green.
3. The `dbae_session_baselines` collection created by the normal schema bootstrap.
4. A fresh generation frozen and bound on the final configuration and schema.
5. The 24 h run.

G1 runs before the build. G2, if the operator authorises it, runs before the fresh generation is frozen, never inside it.

**Effect on the run.** The paper account (about $100k) is in `FULL`, so DBAE refuses no asset class there. It changes the funding check (§7), which is stricter, and adds the new audit events and `risk_snapshot.dbae`. The run's opening 1-share buy goes through the gateway before the cursor bootstrap, as planned.

DBAE is built in a worktree and never edited during a run.

## 16. Review trail

- **Section 1 (approach):** a pure policy module plus separate capability protocols, approved with DBAE-01 to DBAE-04.
- **Section 2:** approved with DBAE-S2-01 to S2-05, then DBAE-S2-R1 to R3 and the funding-concurrency finding (§2.3, §7).
- **Operator decisions:**
  - retire the ladder;
  - gradual unlocks;
  - scan cost not in DBAE;
  - "keep one day trade in reserve" superseded by the PDT retirement evidence (§2.2) and approved by the reviewer.
- **Rev. 1 forensic audit:** DBAE-R1-01 to R1-04 (§17).
- **Rev. 2 forensic audit:** DBAE-R2-01 and R2-02, plus G1 completed (§13.1, §17).
- **Rev. 3 forensic audit:** DBAE-R3-01 and the multiplier-1 clarification (§17).

## 17. Change log

**Rev. 4 (2026-10-30)** answers the rev. 3 audit.

| Finding | Change | Where |
|---|---|---|
| DBAE-R3-01, funding multiplier | Every notional formula now names `contract_multiplier`, sourced only from `contract_multiplier_of(asset)`: 1 for equities and crypto, and Alpaca's contract record for options, never an assumed 100. The account's buying-power multiplier is renamed `account_multiplier` and banned from every notional calculation, with a static test. Tests: a $10 order with `account_multiplier` 4 reserves about $10; options with standard, non-standard and missing multipliers. | §5.1, §7.2, §7.4, §8.3, §14.2 |
| Multiplier-1 clarification | `account_multiplier == 1` is no longer called a cash account. It is refused as `DBAE_ACCOUNT_TYPE_UNVERIFIED`, because Alpaca uses 1 for limited-margin accounts too, until the account type and settled-funds rules are verified. Only 2 and 4 are accepted. | §5.3, §10, §14.4 |

**Rev. 3 (2026-10-30)** answers the rev. 2 audit.

| Finding | Change | Where |
|---|---|---|
| DBAE-R2-01, quantity identity | Four distinct fields, written once: `original_requested_quantity`, `risk_approved_quantity`, `shaped_quantity`, `submitted_quantity`. The intent's `requested_quantity` is defined as the executable submitted quantity from `RISK_APPROVED` on. Readers are listed. A mismatch is an integrity fault (`DBAE_QUANTITY_IDENTITY_MISMATCH`). Restart and serialization tests added. | §7.1, §14.2 |
| DBAE-R2-02, liquidation evidence scope | The global `SUB_MINIMUM_FULL_LIQUIDATION` constant is replaced by `SUB_MINIMUM_LIQUIDATION_EVIDENCE`, keyed by an exact `LiquidationScope` (environment, account type, asset class, quantity kind, order type, time in force, session, minimum kind). There is no wildcard and no inheritance. G2 can verify only the paper fractional-equity market-day scope. | §8.3, §13, §15, §14.5 |
| G1 dependency | G1 was run read-only and recorded. It establishes bar-to-trading-date mapping by New York date and `last_equity` agreement, and states its limitation (flat account, single instant). A read-only G1b is added on a non-flat day, with a fail-closed pre-market retry. | §4.3, §13, §13.1, §14.3 |

**Rev. 2 (2026-10-30)** answers the rev. 1 audit.

| Finding | Change | Where |
|---|---|---|
| DBAE-R1-01, authorised funding quantity | The four quantities (request, risk-approved, shaped, submitted) are recorded separately. Funding is reserved on the submitted (shaped and re-approved) quantity, never the request. A second `evaluate_risk` run on the shaped quantity must approve it exactly (`DBAE_SHAPED_QUANTITY_NOT_REAPPROVED`). The existing gateway overwrite of the intent's quantity is now documented. | §7.1, §7.2, §8.1, §8.2, §10, §11, §14.2 |
| DBAE-R1-02, session baseline | The stale-baseline fallback and `DBAE_SESSION_BASELINE_UNVERIFIED_CAPPED` are removed. An unverified current-session baseline refuses new entries for that session. A new account waits one session. | §4.3, §10, §14.3 |
| DBAE-R1-03, funding conservation | A table of components with sources and when each changes. `P` is released only by attribution or a broker-confirmed terminal state, never because a broker figure moved. The over-count from `buying_power` and accepted orders is stated, with its small-account effect. G2 now records account snapshots around each order to establish broker behaviour. No relaxation without that evidence and a new Rev. | §7.3, §7.5, §13, §14.2 |
| DBAE-R1-04, verification activation | DBAE is a new release. The frozen generation policy carries `dbae_policy_version` and DBAE's constants. A pre-DBAE generation cannot be bound or resumed (`generation_predates_dbae`). The release order ends in a fresh frozen generation. | §15, §14.8 |
| Added tests | A $20 request approved at $3.50; increment shaping; a partial fill with a lowered broker figure; a missing current baseline; G2 unverified at $20 conservative; activation into an old generation; restart after an ambiguous buy; stop exits below $20. | §14 |
