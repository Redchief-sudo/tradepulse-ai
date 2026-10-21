# Design: Interactive Brokers, futures and forex lanes

Status: proposal for review, 2026-10-20. No code yet. Built in parallel with, and never inside, the frozen Rev.129 Alpaca soak and the 60-day Alpaca prove-edge run.

## 1. Goal and approach

More trading opportunities: US stocks and options through a second broker, plus futures and forex. The operator has a live Interactive Brokers (IBKR) account, and an IBKR paper account is being requested.

Approach:
- Prove the current strategy on Alpaca first.
- Meanwhile, build the IBKR work in its own worktree.
- Soak each new venue and lane separately: a soak can only prove what it actually trades, and a combined failure would not say whether the strategy or the new plumbing failed.

## 2. Where the code stands (measured 2026-10-20, Rev.129)

| Area | Today | Consequence |
|---|---|---|
| Broker access | `AlpacaClient` is imported directly by 13 modules (gateway, monitor, scanner, reconciliation, valuation, session, web, CLI and others). | About 11 calls are used outside `broker/`: `get_positions` (11 call sites), `get_account` (9), `get_clock` (6), `get_order` (4), `get_activities` (4), `get_order_by_client_order_id` (2), `get_latest_quote` (2), `place_order`, `get_options_chain`, `set_market_data_feeds` and `rate_limit_snapshot`. That is a narrow, extractable seam. |
| Asset identity | `AssetIdentity` already has a `venue` field, and `asset_identity_key` is `asset_class:venue:native_asset_id`. | Venue-qualified identity exists; only `asset_key_from_broker_symbol` hard-codes `alpaca:` and needs a venue parameter. |
| Asset classes | `AssetClass` already defines `FOREX` and `COMMODITY`. | Futures need a `FUTURE` value (or `COMMODITY` repurposed, not recommended). |
| Market hours | One US-equity clock from Alpaca's `/v2/clock`, plus `is_continuous_market()` (crypto only). | Futures (CME Globex, nearly 24/5 with a daily break) and forex (24/5) need per-venue session calendars. |
| Accounting evidence | Reconciliation, fee populations, generation membership and the soak/prove-edge evidence are built on Alpaca's account-activity feed (`FILL`, `FEE`, `CFEE`, `JNLC`, `CSD`; date-prefixed IDs; the overnight REG/TAF/CAT batch). About 5,100 lines across `reconciliation/` and `verification/`. | **The largest piece of work.** IBKR reports fees differently (below), so each evidence rule needs an IBKR counterpart, not a renamed field. |
| Sizing | Risk-based sizing already applies `contract_multiplier`. Caps work in USD notional. | Works for futures as-is, but needs a margin cap. Forex needs a USD conversion of notional and risk. |

## 3. Architecture

### 3.1 A broker port

A `BrokerPort` protocol in `tradepulse/broker/port.py` takes the calls in section 2, typed with broker-neutral models: `Account`, `Position`, `OrderRequest`, `OrderState`, `Execution`, `FeeRecord`, `Quote`, `SessionCalendar`. `AlpacaClient` is wrapped by `AlpacaBroker(BrokerPort)` without behaviour change. That wrap is its own revision, gated by the existing test suite: Alpaca behaviour must be byte-identical. Every module then takes a `BrokerPort`, or a `BrokerRegistry` keyed by venue.

### 3.2 Routing and identity

- Each lane is bound to one venue in config, for example `equity: alpaca` and `future: ibkr`. A position's venue is fixed at entry and travels in `AssetIdentity.venue`, so exits, reconciliation and fees always use the broker that holds it.
- `asset_key_from_broker_symbol(asset_class, symbol, venue)`: IBKR native IDs use the contract id (`ibkr:conId`), which is stable across symbol changes and unambiguous for futures expiries.
- Risk and exposure stay account-wide in USD across venues: one equity curve and one daily-loss limit.

### 3.3 Per-venue accounting evidence

| Evidence | Alpaca | IBKR |
|---|---|---|
| Fills | Activity `FILL` records | `execDetails` (`execId`, `permId`, `orderRef`) |
| Fees | Overnight REG/TAF/CAT batch, CFEE for crypto | **Per fill:** a `commissionReport` for each `execId` (commission plus regulatory and exchange fees). Simpler: no batch, no fee-day waits. |
| History beyond the session | Paginated activity feed | The API returns executions for the current session only (up to 7 days in TWS). Full history and cash comes from the **Flex Web Service** (statement queries). Reconciliation must use Flex for anything older than today. |
| Idempotency and recovery | `client_order_id` lookup | `orderRef` = `trade_intent_id`, recovered with `reqOpenOrders` / `reqCompletedOrders` / `reqExecutions` filtered by `orderRef`; `permId` is the broker order id. The Rev.120–123 identity checks (`order_matches_intent`) need an IBKR version. |
| Cash | One USD balance | Per-currency balances (forex creates them), plus futures variation margin moving cash daily. |

## 4. IBKR adapter (Phase A: stocks and options)

**Runtime:**
- IB Gateway runs headless, with **IBC** handling login and IBKR's daily restart (around 11:45 pm ET); Python uses `ib_async`.
- Reconnect and backoff: a gateway restart pauses execution. The monitor and reconciliation must treat it like the existing "broker unavailable" paths, which fail closed for entries and alert.
- The Oracle server hosts the gateway beside TradePulse. Only `127.0.0.1` exposes the API port.

**Pacing and market data:**
- IBKR limits API messages (about 50 a second) and historical-data requests. The adapter needs a pacing queue.
- The paper account must share the live account's market-data subscriptions (a Client Portal setting).

**Orders and positions:**
- Market and limit orders, with time in force `DAY`, or `GTC` where needed.
- Options by OCC-style contract with multiplier 100, the same model as the existing options lane.
- Positions and account summary come from `reqPositions` / `reqAccountSummary`.

**Soak:** IBKR paper, stocks and options only. The same invariants as Alpaca, with IBKR evidence sources.

## 5. Futures (Phase B)

**Universe:** micro contracts first, so risk per contract fits small accounts:
- equity index: MES, MNQ, M2K, MYM;
- commodities: MGC, MCL;
- FX futures: M6E, M6B, M6A (see section 6).

**Contract model:**
- root, expiry, multiplier, tick size and tick value, first notice and last trade dates;
- `native_asset_id = ibkr:conId`, so each expiry is a distinct instrument.

**Rolls:** hold only the front contract. Roll N trading days before last trade (configurable, for example 5): close the old contract, then open the new one through the gateway, as one recorded roll decision. Never hold into first notice for physically delivered contracts.

**Sessions:** a CME Globex calendar (Sunday 6 pm to Friday 5 pm ET, with a daily one-hour break), replacing the single equity clock for this lane. Exits follow the futures session, not stock hours.

**Sizing:**
- Risk-based sizing already multiplies by the contract multiplier, and quantity is whole contracts.
- New: an initial-margin cap, so combined margin stays at or below X% of equity.
- The cost rule uses IBKR commission plus exchange fees per contract, for example about $0.60–0.90 a side for micros.

**Accounting:** daily mark-to-market moves cash (variation margin) while the position is open. Lots stay the source of truth for realized P&L on close. Cash reconciliation must accept variation-margin flows as explained movements, with Flex as the authority.

**Strategy:** the factors and the AI prompt need futures-appropriate inputs (trend and volatility of the contract's own continuous series, roll-adjusted), and a benchmark per sector rather than SPY.

## 6. Forex (Phase C)

**Recommendation: start forex through CME micro FX futures (M6E, M6B, M6A, and similar) in the futures lane, not spot.** It reuses the futures model completely: USD-settled, no foreign-currency cash balances, no swap interest, one contract model. It avoids spot FX's minimum sizes on IDEALPRO (about $25k base for best pricing; smaller trades are odd lots with wider pricing).

Spot forex (later, optional) needs multi-currency cash in the ledger, P&L in the quote currency converted to USD at an evidenced rate, rollover and interest accrual, and a 24/5 session calendar. That is significant accounting work: plan it only if micro FX futures prove too limiting.

## 7. Phases, effort and gates

| Phase | Content | Rough effort | Gate |
|---|---|---|---|
| A0 | `BrokerPort` extraction, Alpaca wrapped unchanged | 1 week | Full suite and an Alpaca soak, byte-identical behaviour |
| A | IBKR adapter, stocks and options, IBKR evidence (executions, commission reports, Flex), IB Gateway and IBC on the server | 3–5 weeks | IBKR paper soak |
| B | Futures lane (micros), contract model, rolls, Globex calendar, margin cap, variation-margin reconciliation, futures factors | 3–4 weeks | Futures paper soak, including a roll |
| C | Forex through micro FX futures (universe and factors) | about 1 week on top of B | FX futures paper soak |
| C2 (optional) | Spot forex, multi-currency ledger | 3–4 weeks | Spot FX paper soak |

The estimates are for working sessions like the ones so far, including tests and review rounds. Prove-edge for each new lane follows its soak.

## 8. Risks

- **Gateway fragility.** IB Gateway sessions drop, restart daily and need 2FA handling. IBC mitigates this; the system must fail closed for entries while disconnected and keep alerting.
- **History gap.** Without Flex, reconciliation cannot see fills older than the session. Flex setup (a token and query IDs) is a prerequisite for Phase A, not optional.
- **Futures leverage.** Notional is large relative to margin. The risk-based sizing and open-risk caps stay in force; add the margin cap and a hard per-contract stop.
- **Data costs.** Futures and some exchange data need paid subscriptions. Choose only what the lanes trade.
- **Strategy fit.** The current edge, if proven, is a stock and options edge. Futures and forex need their own evidence; they do not inherit it.

## 9. Operator prerequisites

1. An IBKR **paper account**: Client Portal → Settings → Account Settings → Paper Trading Account.
2. **Trading permissions** on the live account for futures (and options); the paper account mirrors them.
3. **Market data:** share live subscriptions with the paper account, and confirm which subscriptions to buy for the chosen micros.
4. **API:** in IB Gateway, enable socket clients and allow `127.0.0.1` only.
5. **Flex Web Service:** create a token and Activity and Trade Confirmation Flex queries for the paper account.

## 10. Open questions for review

1. A broker port with a venue registry (proposed), or a separate runtime per broker?
2. Forex through micro FX futures first (proposed), or spot from the start?
3. Initial futures universe: index micros only, or index plus metals and energy micros?
4. Margin cap value (proposed: initial margin at or below 25% of equity across futures).
5. Does stock and options trading move from Alpaca to IBKR after Phase A, or run on both (venue chosen per lane)?
