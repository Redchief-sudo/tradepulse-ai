# Intelligence roadmap

Status: proposal, 2026-10-20. Items 1–3 run outside the frozen trading code and can be built while the Alpaca soak and prove-edge run. Items 4–7 change trading behaviour, and each reaches live trading only through evidence, then a soak, then a proof.

## Principle

Intelligence here means **learning from evidence faster than other systems without fooling yourself**. Every improvement follows one loop:

1. **Collect** decision-time data (stored since Rev.128 and Rev.129).
2. **Label** it with what happened afterwards (item 1).
3. **Measure** which inputs actually predicted outcomes (item 2).
4. **Challenge** the live strategy with alternatives in shadow mode (item 3).
5. **Promote** a challenger only after it beats the champion on live data, then soak and prove it.

Nothing learns or changes behaviour inside a proof run. A frozen strategy is what prove-edge proves.

## 1. Outcome labelling (specified below)

A nightly offline job scores every AI-scanned candidate, traded or not, by what its price did next. The trading database's growing decision log becomes a training set.

## 2. Calibration and factor reports

Built on the labels:

- **AI calibration:** for each confidence band (70–75, 75–80, 80–85, and so on), the realised hit rate and average forward return, in total and by lane and regime. Does "80%" mean 80%?
- **Factor value:** for each factor and the composite, the rank correlation with forward return (information coefficient) by lane, regime and horizon, with confidence intervals. Is the 62.5 threshold well placed? Is the crypto composite structurally low?
- **Gate audit:** forward returns of candidates each gate rejected (confidence, composite, cost rule, spread, sizing). A gate that rejects candidates that then outperform is costing money.
- **Exit audit:** with the recorded exit reasons (Rev.129), outcomes by exit type, and what happened after each exit (selling too early or too late).

Output: a dated report file plus a dashboard page. The numbers need about 2–3 months of labels before they mean much. The report shows sample sizes and intervals on every figure, so small samples cannot pass as findings.

## 3. Champion and challenger in shadow mode

Alternatives (thresholds, weights, exit-ladder parameters, universes) run beside the live strategy on the **same** scans and quotes, recording the orders they *would* place and replaying their exits on real prices. They never trade and never touch the trading database: their own store, their own ledger.

A challenger becomes a candidate for promotion only when:
- its shadow results beat the champion over a pre-registered window and sample size;
- its 95% lower bound on net expectancy, after modelled costs, beats the champion's.

Promotion is then an ordinary revision: freeze, soak, prove.

## 4. Ensemble judgement

Several AI models (for example Claude, GPT and an open model) score the same candidates, alongside the quant composite. Agreement becomes an input: high agreement sizes normally, disagreement sizes down or skips. Introduced first as a challenger (item 3), so it must earn its place.

## 5. Execution intelligence

From TradePulse's own fills: realised slippage by symbol, time of day and order type, so the system chooses market or limit orders and avoids expensive windows. The cost rule then uses measured rather than assumed costs.

## 6. Risk intelligence

- Volatility-targeted sizing, so portfolio volatility stays near a target.
- Correlation-aware open-risk accounting, where correlated positions count as more risk.
- Automatic de-risking in drawdowns.

Each starts as a challenger.

## 7. Memory as statistics

Setup-level priors, such as "this signal pattern in this regime on this instrument has historically returned X with N samples", feeding the next decision as an input. Updated between runs from item 1 labels; never self-modifying during a run.

---

# Item 1 specification: outcome labelling

## Inputs (read-only)

The job reads a **copy** of each trading or soak database, made with SQLite's `.backup` from a `mode=ro` connection. It never opens a trading database for writing and never writes beside it. Holding a long read transaction on a live database slows its writers (Rev.124), so it works from a copy.

| Source | Use |
|---|---|
| `ai_responses` | **Master list:** every candidate the AI returned (symbol, recommendation, confidence, summary, model), linked to its scan by `request_id` |
| `scan_runs` | Lane (`asset_class`), decision time (`started_at` / `completed_at`), regime fields, weights profile, market-data feeds |
| `rejected_candidates` | Which gate stopped the candidate (`reason`), plus its context (confidence; full `factor_scores` since Rev.128 on composite rejections) |
| `opportunities` | Accepted candidates: quote, stop, composite rank, full `factor_scores` |
| `trade_intents`, `fills`, `trade_attributions` | What was traded and how it ended, including the Rev.129 `exit_reason` on closing intents |

## Sample unit and deduplication

One **candidate-day** = (lane, symbol, New York trading date). The same symbol is re-scanned about every 15–20 minutes: in the Rev.127 soak, 1,971 rejection rows covered only 79 symbol-days. Keep the **first** scan of the day as the decision record, and also keep the number of scans that day and whether any of them was accepted. This keeps samples nearly independent: roughly 130 a week at the current universe size.

## Features (decision-time only)

- AI: recommendation, confidence, model.
- Factors: the stored `factor_scores` when present; otherwise **recomputed** from daily bars ending the trading day before the decision, using the code version recorded with the scan. Mark `factors_source = stored | recomputed`.
- Composite and weights profile, regime fields, lane, sector, the gate outcome (`accepted` or the rejection reason), and spread or cost at decision time where recorded.
- Never any information timestamped after the decision.

## Labels

Daily bars from Alpaca (`get_bars`, `1Day`), equities split-adjusted. The decision price `p0` is the stored `factor_scores.price` when present, otherwise the first bar close at or after the decision time.

| Label | Definition |
|---|---|
| `ret_1d`, `ret_5d`, `ret_10d` | close at decision date + h trading days ÷ p0 − 1 (crypto: calendar days) |
| `mfe_10d`, `mae_10d` | best high and worst low within 10 days, as returns from p0 |
| `r_outcome` | The trade **replayed under the exit ladder**: entry p0; stop = p0 − 2 × ATR(14) at the decision date (the scanner's rule, clamped as it is); exits at the stop, half at +1R with the stop then at break-even, the rest at +3R or on the trailing rule; stall exit at 7 days; time stop at 15 days. Result in R, net of the lane's modelled costs. **When a day's range touches both stop and target, assume the stop came first** (conservative). |
| `benchmark_ret_5d` | Same-window return of the lane benchmark (SPY for equities and options underlyings, BTC/USD for crypto), so results can be judged relative to the market |

A label is written only once its horizon has fully passed. Rows not yet mature stay pending and are filled on a later night.

## Output

`data/research/labels.sqlite`, outside the repository's protected source and outside every trading-database directory:
- `candidates`: one row per candidate-day, with features (JSON), labels, `label_version`, source database and `labelled_at`;
- `runs`: one row per job run, recording inputs, counts and code revision.

The job is idempotent: rerunning adds newly mature labels and never rewrites an existing label for the same `label_version`. A change to the label definition bumps `label_version` and writes alongside.

## Rules against fooling ourselves

1. Features only from decision time. Labels only after the horizon. Both are enforced in code and covered by tests.
2. Evaluation is **walk-forward by time** (train on earlier weeks, test on later weeks), never random splits.
3. Every reported figure carries its sample size and a confidence interval.
4. Hypotheses to test are written down before looking at the test period.
5. Survivorship: the universe is what was actually scanned at the time, never today's list.

## Schedule and operation

A user systemd timer runs it nightly after the US close and fee batch (for example 02:30 UTC), at low priority (`nice 10`), outside soak runs' critical windows. On the Oracle server it runs beside TradePulse.

## Tests

- Deduplication (many scans → one candidate-day).
- Each label against hand-computed bar series, including the both-touched-same-day rule and immature horizons left pending.
- Feature leakage (a bar after the decision time must not change features).
- Idempotent reruns.
- Never opening a source database for writing.

## Effort

About 1–2 weeks including the replay of the ladder. Item 2's first report follows once a few weeks of labels exist.
