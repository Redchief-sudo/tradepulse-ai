# Exit Intelligence / Portfolio Optimization — empirical calibration

Ground rules, matching `docs/regime-classifier-phase1-calibration.md`'s own
discipline: real historical data only (live-fetched from Alpaca, this
account's actual IEX/crypto feeds, the same production `AlpacaClient.get_bars`
code path), no synthetic series, no tuning thresholds merely to get a
desired-looking number. This is **empirical grounding, not backtesting** —
it derives thresholds from what the market actually does (real ATR%, real
pairwise correlation), not from simulated trade P&L. See the session
decision: full backtesting was explicitly deferred as a separate, larger
project.

## Data pulled

Full default universe, live from Alpaca:
- 35 equity symbols (`strategy/universe.py::DEFAULT_EQUITY_UNIVERSE`), daily
  bars, 2020-09-08 → 2026-09-02 (~1,500 trading days each — IEX history
  starts reliably around 2020-07-27, confirmed in Regime Phase 1).
- 5 crypto pairs (`DEFAULT_CRYPTO_UNIVERSE`), daily bars, 2021-01-01 →
  2026-09-03 (~1,650–2,072 days each — SOL/USD's Alpaca history starts
  later than the other four, confirmed by a shorter series length).

## Finding 1: `trailing_atr_multiplier` — the existing guess was already in a reasonable range, now backed by real data instead of a guess

Computed a genuinely **rolling** ATR(14)-as-%-of-price (60-bar trailing
window, resampled every 5 bars, using the actual production `atr()`
function) across the full history of every universe symbol — 10,115 equity
observations, 1,931 crypto observations, spanning calm and volatile
conditions alike, not just today's snapshot.

| | p10 | p25 | median | p75 | p90 | p95 | mean |
|---|---|---|---|---|---|---|---|
| Equity ATR% | 0.80 | 1.21 | 1.69 | 2.32 | 3.17 | 4.03 | 2.05 |
| Crypto ATR% | 3.61 | 4.62 | 6.06 | 8.34 | 10.98 | 13.26 | 7.29 |

Crypto's ATR% runs ~3.6x equity's median — consistent with Regime Phase 1's
own finding that crypto's calibrated volatility thresholds are roughly
4-5x equity's.

**`trailing_atr_multiplier` is deliberately calendar-agnostic** (one value
per profile, not split by equity/crypto like `_CALENDAR_THRESHOLDS` in
`regime.py`) — and that turns out to be correct, not an oversight: ATR
itself already scales with the asset's own volatility, so the same
multiplier produces a proportionally appropriate trail distance on both
calendars. At the balanced profile's 2.5x: equity's median ATR% (1.69%)
gives a ~4.2% trail; crypto's median (6.06%) gives a ~15.2% trail — both
reasonable relative to each asset class's own typical daily movement, with
no separate crypto multiplier needed.

**Verdict: kept unchanged.** The existing per-profile values (aggressive
3.0 / balanced 2.5 / conservative 2.0 / micro 2.5) land in a defensible
range against the real distribution — conservative's 2.0x on median equity
ATR% (3.4% trail) is tight without being a hair-trigger; aggressive's 3.0x
(5.1% trail) gives real room to run. No change made; the confidence in
these numbers is now empirical rather than a guess.

## Finding 2: `max_correlation_threshold` — the existing guesses were too permissive relative to the real distribution

Computed pairwise |Pearson correlation| of daily returns (the actual
production `pearson_correlation()` function) across every equity pair
(595 = C(35,2)) and every crypto pair (10 = C(5,2)) in the universe.

| | p10 | p25 | median | p75 | p90 | p95 | mean |
|---|---|---|---|---|---|---|---|
| Equity \|corr\| (595 pairs) | 0.044 | 0.087 | 0.195 | 0.384 | 0.568 | 0.766 | 0.266 |
| Crypto \|corr\| (10 pairs) | 0.050 | 0.054 | 0.668 | 0.717 | 0.815 | 0.815 | 0.454 |

Concrete named pairs, for interpretability:

| Pair | Correlation | Read |
|---|---|---|
| SPY vs VOO | 0.996 | near-duplicate — must always demote |
| SPY vs VTI | 0.992 | near-duplicate |
| SPY vs QQQ | 0.936 | same broad-market bet |
| TLT vs IEF | 0.908 | same duration-risk bet |
| JPM vs BAC | 0.822 | same-sector banks |
| GLD vs SLV | 0.787 | same precious-metals bet |
| XLK vs AAPL | 0.552 | related, not identical |
| AAPL vs MSFT | 0.549 | both mega-cap tech, only moderately correlated day-to-day |
| KO vs PG | 0.484 | loosely related consumer staples |
| SPY vs XLU | 0.283 | weakly related |
| SPY vs GLD | 0.159 | genuinely diversifying |
| BTC/USD vs ETH/USD | 0.815 | crypto majors move together |
| BTC/USD vs SOL/USD | 0.054 | SOL's shorter/idiosyncratic history diverged |

**Equity finding**: the original guessed thresholds (aggressive 0.85 /
balanced 0.75 / conservative 0.65 / micro 0.75) sit at roughly the p95-99
range of the real distribution — high enough that they would only ever
catch near-duplicate pairs (SPY/VOO, SPY/VTI) and miss meaningfully
concentrated bets like JPM/BAC (0.82, still slightly below the old
aggressive setting) or the AAPL/MSFT-level of relatedness (0.55) entirely.
**Adjusted downward**, grounded in both the percentile table and the named
pairs above:

| profile | old | new | rationale |
|---|---|---|---|
| aggressive | 0.85 | 0.75 | ~p95 — only clear same-bet pairs (SPY/QQQ-level and tighter) |
| balanced | 0.75 | 0.65 | ~p90 — catches JPM/BAC-level sector concentration |
| conservative | 0.65 | 0.55 | ~p85 — catches AAPL/MSFT-level relatedness, most cautious |
| micro | 0.75 | 0.65 | same posture as balanced |

**Crypto finding, flagged but NOT acted on**: crypto's own typical
pairwise correlation (median 0.67) sits ABOVE every profile's threshold
except aggressive's new 0.75 — meaning ordinary crypto co-movement
(BTC/ETH-level) would trigger demotion under balanced/conservative/micro's
new thresholds almost by default, not just for genuinely unusual
concentration. This is a real, worth-fixing gap (the same calendar-split
principle `regime.py`'s own `_CALENDAR_THRESHOLDS` already established),
but **not fixed here**: crypto's sample is only 10 pairs (5 symbols), too
small to derive a reliable separate crypto threshold without just
inventing a number dressed up as calibration. Flagged as a named follow-up
once the crypto universe is larger or more history is available (SOL/USD
in particular).

## Finding 3: `max_hold_days` — no clean empirical proxy without crossing into backtesting; kept as a judgment call, now with weaker-confidence supporting context

Attempted to ground this in real regime-persistence (day-by-day
`classify_regime` run-lengths on SPY/BTC-USD, the same benchmark assets
Market Regime Phase 1 calibrated). Result: `low_vol_bull` — the regime a
fresh long entry typically occurs in — has a median run-length of only
4-4.5 days (mean ~6, p75 ~8, max 19-22) for both SPY and BTC/USD. This is
the classifier's *label* flipping on day-to-day noise, not a clean "how
long does a real trend last" signal, and is **too noisy to derive
`max_hold_days` from directly** — using it naively would suggest a
4-8-day time stop, which would fight the ATR trailing stop constantly on
ordinary short-term pullbacks within a still-intact uptrend.

The one steadier statistic found: `range_bound_choppy` (the "nothing much
is happening" steady state) has a much longer median persistence — 46.5
days (SPY) / 28 days (BTC/USD). Used only as a loose upper anchor: a
position open longer than a typical range-bound episode without reaching
target suggests the original bullish thesis has likely expired.

**Verdict: kept unchanged**, with explicitly weaker confidence than
Findings 1-2. The existing per-profile values (aggressive 10 / balanced 15
/ conservative 30 / micro 12 days) already sit at roughly 1/3-2/3 of the
range-bound-persistence anchor, scaled by each profile's stated risk
posture (aggressive recycles capital fastest, conservative gives the
thesis the most room) — a defensible judgment call, not a data-derived
optimum. A real calibration of this field needs either a proper backtest
(explicitly deferred) or real paper-trade outcome data from Outcome
Attribution, neither of which exists yet.

## `break_even_trigger_pct` — out of scope for this pass

Not attempted here (no market-statistic directly answers "how much
unrealized gain should be locked in before letting a trade run further" —
it's a risk-tolerance decision, not an empirical property of price
series), matching the original scoping discussion. Kept unchanged
(aggressive 6 / balanced 4 / conservative 2.5 / micro 3 pct).

## Summary of changes made

- `max_correlation_threshold`: adjusted per the table in Finding 2 (all
  four profiles lowered).
- `trailing_atr_multiplier`, `max_hold_days`, `break_even_trigger_pct`:
  unchanged — validated as reasonable (Findings 1, 3) or out of scope, not
  silently left unexamined.
- New flagged follow-up: crypto-specific `max_correlation_threshold`
  (needs a larger/longer crypto universe sample first).

## Verification

- `.venv/bin/python -m pytest python_tests -q` — full suite green (only
  `test_config_strategy_weights.py`-style profile-value assertions could
  be affected; confirmed none hardcode the old correlation thresholds).
