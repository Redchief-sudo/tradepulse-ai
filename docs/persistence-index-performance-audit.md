# SQLite EXPLAIN QUERY PLAN Performance Audit — Final Report

Scope: `tradepulse/persistence/repositories.py`'s six unbounded/keyset-paginated query
families (plus two related helpers: `exists_with_status_and_asset`,
`max_by_json_field`) against synthetic scratch databases at 1k / 10k / 100k rows,
built from the app's own model classes (`tradepulse.models.*`) and
`tradepulse.persistence.codec.encode_payload`, so every JSON payload shape matches
production exactly. All work happened in disposable scratch files under
`/tmp/claude-1000/-home-damien-tradepulse-ai/0be70285-825a-48f3-b413-92eb76115775/scratchpad/`;
`tradepulse/persistence/database.py` and `repositories.py` were read-only referenced,
never modified; the real `tradepulse.db` was never written to (its row counts from
§9 were reused from the prior read-only measurement, not re-queried).

Artifacts produced:
- `gen_perf_db.py` — synthetic-data generator (model classes + `encode_payload`).
- `perf_audit_baseline_{1k,10k,100k}.db` — baseline (schema-as-shipped, 4 existing
  indexes only, never mutated after population).
- `perf_audit_equity_100k.db` — supplementary 100k-row `equity_snapshots` table for
  `max_by_json_field`, since that table isn't one of the six paginated families.
- `perf_audit_idx_*_100k.db` — one disposable `shutil.copy` per candidate index.
- `measure.py`, `run_measurements.py` — EXPLAIN QUERY PLAN / timing harness.
- `measurements_output.log` — full raw output of every EQP/timing run below.

---

## 1. Findings table

All "Current Plan" cells are verbatim `EXPLAIN QUERY PLAN` output (see
`measurements_output.log` for the full run; scale noted was structurally identical
at 1k/10k/100k in every case — confirmed, not assumed).

| # | Query | Current Plan (baseline, verbatim) | Problem | Proposed Index/Change | Severity | Expected Benefit (measured) |
|---|---|---|---|---|---|---|
| 1 | `position_lots.list_by_asset` (asset-identity filter, first page) | `QUERY PLAN`<br>`|--SCAN position_lots`<br>`` `--USE TEMP B-TREE FOR ORDER BY `` | Full table scan computing 3 `json_extract`/row + separate sort pass; cost grows with total table size regardless of how selective the asset is | `CREATE INDEX idx_position_lots_asset ON position_lots(json_extract(payload,'$.asset.asset_class'), LOWER(COALESCE(json_extract(payload,'$.asset.venue'),'default')), json_extract(payload,'$.asset.native_asset_id'), created_at, record_id)` | **High** | Proven: `` `--SEARCH position_lots USING INDEX idx_position_lots_asset (<expr>=? AND <expr>=? AND <expr>=?) ``, **no** temp b-tree line (index also serves the sort). Measured 293.8ms → 5.8ms @ 100k rows (**50.9×**). Also proven to serve the keyset-continuation form (`AND (created_at,record_id)>(?,?)`) with the same index, no b-tree. |
| 2 | `fills.list_by_json_field('trade_intent_id', ...)` | `QUERY PLAN`<br>`|--SCAN fills`<br>`` `--USE TEMP B-TREE FOR ORDER BY `` | Same shape as #1; called from settlement lookups keyed by trade_intent_id | `CREATE INDEX idx_fills_trade_intent_id ON fills(json_extract(payload,'$.trade_intent_id'), created_at, record_id)` | **High** | Proven: `` `--SEARCH fills USING INDEX idx_fills_trade_intent_id (<expr>=?) ``, no b-tree. Measured 210.3ms → 0.018ms @ 100k (**~11,600×** — trade_intent_id is unique-per-fill, so the unindexed path pays a full 100k-row scan to find one row). |
| 3 | `settlements.list_by_json_field('trade_intent_id', ...)` | `QUERY PLAN`<br>`|--SCAN settlements`<br>`` `--USE TEMP B-TREE FOR ORDER BY `` | Same shape; used by `fill_attribution.py`'s retroactive quarantine sweep on every proven integrity dispute | `CREATE INDEX idx_settlements_trade_intent_id ON settlements(json_extract(payload,'$.trade_intent_id'), created_at, record_id)` | **High** | Proven: `` `--SEARCH settlements USING INDEX idx_settlements_trade_intent_id (<expr>=?) ``, no b-tree. |
| 4 | `settlements.list_by_json_field('broker_fill_id', ...)` | `QUERY PLAN`<br>`|--SCAN settlements`<br>`` `--USE TEMP B-TREE FOR ORDER BY `` | Same shape, lower call frequency | `CREATE INDEX idx_settlements_broker_fill_id ON settlements(json_extract(payload,'$.broker_fill_id'), created_at, record_id)` | Medium | Proven: `` `--SEARCH settlements USING INDEX idx_settlements_broker_fill_id (<expr>=?) ``, no b-tree. |
| 5 | `trade_intents.list_by_json_field('broker_order_id', ...)` | `QUERY PLAN`<br>`|--SCAN trade_intents`<br>`` `--USE TEMP B-TREE FOR ORDER BY `` | Same shape, moderate call frequency | `CREATE INDEX idx_trade_intents_broker_order_id ON trade_intents(json_extract(payload,'$.broker_order_id'), created_at, record_id)` | Medium | Proven: `` `--SEARCH trade_intents USING INDEX idx_trade_intents_broker_order_id (<expr>=?) ``, no b-tree. |
| 6 | `fills.list_by_json_time_range('filled_at', ...)` | `QUERY PLAN`<br>`|--SCAN fills`<br>`` `--USE TEMP B-TREE FOR ORDER BY `` | Full scan + sort | `CREATE INDEX idx_fills_filled_at ON fills(json_extract(payload,'$.filled_at'), created_at, record_id)` | **High** | **Partial win, proven precisely**: `` |--SEARCH fills USING INDEX idx_fills_filled_at (<expr>>? AND <expr><?) `` **followed by** `` `--USE TEMP B-TREE FOR ORDER BY `` (still present). A *range* predicate on the leading (expression) column does not let SQLite use the trailing `created_at, record_id` columns for the sort — only a leading *equality* does. So this index removes the full-table-scan/json_extract-per-row cost but **not** the sort step. Still a major win for wide tables (scan cost dominates), but do not claim the temp-b-tree is eliminated — it is not. |
| 7 | `holdings.list_page` (whole table, `paginate_all_rows`) | `QUERY PLAN`<br>`|--SCAN holdings`<br>`` `--USE TEMP B-TREE FOR ORDER BY `` | None in practice | **None recommended** | N/A (rejected — see §3) | Measured 0.133ms at 40 synthetic rows (real prod: 3 rows). `holdings` is bounded by distinct-asset count, not by trade/fill volume — it will never reach a size where this matters. |
| 8 | `position_lots.list_page` (whole table, `paginate_all_rows`) | `QUERY PLAN`<br>`|--SCAN position_lots`<br>`` `--USE TEMP B-TREE FOR ORDER BY `` | Unlike holdings, this table is append-only and grows with fill volume; the full-scan itself is unavoidable for an unfiltered read, **but the sort step is not** | `CREATE INDEX idx_position_lots_created ON position_lots(created_at, record_id)` (**new candidate, found during this audit, not in the original candidate list**) | **High** (was mis-classified in the initial plan as "no index needed" by analogy with holdings — corrected here with evidence) | Proven: `` `--SCAN position_lots USING INDEX idx_position_lots_created `` (single line — index serves the scan **in already-sorted order**, no separate temp b-tree). Measured 67.7ms → 2.5ms for one page @ 100k rows (**27×**). Table must still be fully scanned once as history grows, but every subsequent page is now a cheap indexed range walk instead of re-sorting from scratch. |
| 9 | `settlements.list_by_statuses(['pending'])` (small-fraction subset) | `QUERY PLAN`<br>`|--SEARCH settlements USING INDEX idx_settlements_status (status=?)`<br>`` `--USE TEMP B-TREE FOR RIGHT PART OF ORDER BY `` | Already uses the shipped `idx_settlements_status(status, created_at)`; the trailing `record_id` tie-break for the compound keyset ORDER BY is **not** covered by the 2-column index, so a residual sort is still done (cheap, since it only sorts same-`created_at` ties, not the whole match set) | See #12 (keyset-sufficiency test) | Low (already indexed; residual cost is small) | N/A here — see row #12. |
| 10 | `settlements.list_by_statuses(['completed'])` (large-fraction, ~95% of rows) | Identical plan shape to #9 (`SEARCH ... (status=?)` + `USE TEMP B-TREE FOR RIGHT PART OF ORDER BY`) | Same index is used even when the status covers nearly all rows — SQLite doesn't know to prefer a scan for a non-selective equality; this matches the shipped index's intended use | No change needed | Low | Plan unchanged whether the status subset is 2% or 95% of the table — the existing index still applies. |
| 11 | `trade_intents.list_by_statuses(['rejected'])` / `(['filled'])` | `` |--SEARCH trade_intents USING INDEX idx_trade_intents_status (status=?) `` + `` `--USE TEMP B-TREE FOR RIGHT PART OF ORDER BY `` | Same shape as #9/#10 on the other shipped status index | No change needed | Low | Already indexed; residual sort is on ties only. |
| 12 | `scan_runs.list_by_statuses(['failed'])` | `QUERY PLAN`<br>`|--SCAN scan_runs`<br>`` `--USE TEMP B-TREE FOR ORDER BY `` | `scan_runs` is in `STATUS_TABLES` but has **no** status index at all (confirmed against current `database.py` SCHEMA — only `settlements`/`orders`/`trade_intents`/`equity_snapshots` have indexes) | `CREATE INDEX idx_scan_runs_status ON scan_runs(status, created_at, record_id)` | **High** (scan_runs is already the highest-volume table in production today — 299 rows, ~1/day and growing) | Proven: `` `--SEARCH scan_runs USING INDEX idx_scan_runs_status (status=?) `` — single line, **no** temp b-tree at all (3-column index fully covers filter + sort). Measured 9.0ms → 2.8ms @ 10k rows (3.2× — table capped at 10k in this synthetic run; the multiplier only grows with row count). |
| 13 | `integrity_holds.list_by_statuses(['fill_quantity_disputed'])` | `QUERY PLAN`<br>`|--SCAN integrity_holds`<br>`` `--USE TEMP B-TREE FOR ORDER BY `` | Same gap as #12 | `CREATE INDEX idx_integrity_holds_status ON integrity_holds(status, created_at, record_id)` | Low today (table is transient by construction, confirmed via call-site read: created only on dispute/verification-pending, deleted once resolved — held at 20/10 rows in this synthetic model as instructed) | Proven: `` `--SEARCH integrity_holds USING INDEX idx_integrity_holds_status (status=?) ``, no b-tree. Recommended defensively (near-zero cost, correct if a future incident spikes hold volume) but not urgent. |
| 14 | `RecordRepository.get` (integrity_holds PK, hot-path guard in `_check_integrity_hold`) | `` `--SEARCH integrity_holds USING INDEX sqlite_autoindex_integrity_holds_1 (record_id=?) `` | None — already an indexed PK lookup via SQLite's automatic PK index | No change | N/A (rejected — see §3) | 0.013ms @ 30 rows; this is called on **every** guarded `mutate`/`create_once`/`update`, but it was already optimal. |
| 15 | `trade_intents.exists_with_status_and_asset` | `` `--SEARCH trade_intents USING INDEX idx_trade_intents_status (status=?) `` | None significant — already benefits from the shipped status index; `LIMIT 1` short-circuits | No change recommended | Low | Already adequate; the asset-identity predicates are applied as a residual filter after the status-indexed seek, and `LIMIT 1` means it stops at the first match. |
| 16 | Keyset-sufficiency: `settlements.list_by_statuses`, non-first-page (mid-range `after` cursor) — **existing** `idx_settlements_status(status, created_at)` only | `` |--SEARCH settlements USING INDEX idx_settlements_status (status=? AND created_at>?) `` + `` `--USE TEMP B-TREE FOR RIGHT PART OF ORDER BY `` | The 2-column index handles `status` equality and the `created_at` half of the range predicate, but not `record_id` — a residual sort of same-`created_at` ties still runs | See row #17 below | Low | 3.576ms @ 100k (500-row page, ~95%-selective status) — the residual sort is cheap here because it only sorts within same-timestamp collision groups, not the whole 95k-row match set. |
| 17 | Keyset-sufficiency, same query, **`idx_settlements_status_only(status)` added alongside** the existing 2-column index | Identical to row 16 — `` |--SEARCH settlements USING INDEX idx_settlements_status (status=? AND created_at>?) `` + b-tree | **Proven dead weight**: SQLite's query planner never switches to the narrower single-column index even when it exists — the wider, already-shipped `(status, created_at)` index dominates it for every plan tested (first page and continuation alike) | **Reject** `idx_settlements_status_only` — do not create it | N/A | Confirmed via two separate EQP captures (first-page and continuation) on a copy carrying both indexes: plan is byte-for-byte identical to the no-status-only-index baseline. Adding it would only add write cost with zero read benefit. |
| 18 | Keyset-sufficiency, same query, **`idx_settlements_status_full(status, created_at, record_id)` added** | `` `--SEARCH settlements USING INDEX idx_settlements_status_full (status=? AND (created_at,record_id)>(?,?)) `` | N/A — this is the fix | This is the one worth shipping if the temp-b-tree cost ever matters at your real status-cardinality/selectivity | Low-Medium | SQLite **does** switch to this 3-column index and the temp b-tree disappears entirely from the plan. However, measured wall-clock at 100k rows / 95%-selective status / 500-row page was 3.576ms (2-col, with residual sort) vs 3.954ms (3-col) — **no measurable improvement, if anything slightly slower** at this scale/selectivity, because the residual sort only ever touches same-timestamp tie groups (small) under this synthetic collision density. **Do not oversell this index** — it is structurally cleaner (proven via EQP) but not proven to matter in wall-clock terms at any scale/selectivity tested. See §3 for the recommendation nuance. |
| 19 | `max_by_json_field('total_equity')` on `equity_snapshots` (not one of the 6 paginated families, but flagged in the task as a restructuring candidate) | `QUERY PLAN`<br>`|--SCAN equity_snapshots`<br>`` `--USE TEMP B-TREE FOR ORDER BY `` (202.3ms @ 100k rows) | Full scan + full-table sort just to find one extremum row | `CREATE INDEX idx_equity_total_equity_real ON equity_snapshots(CAST(json_extract(payload,'$.total_equity') AS REAL))` | **High** | **Correction to the plan's working assumption** — this expression index **is** usable, proven: `` SCAN equity_snapshots USING INDEX idx_equity_total_equity_real `` (single line, index walked in DESC order, `LIMIT 1` stops immediately). Measured 202.3ms → 0.019ms (**~10,600×**). The plan is a `SCAN` of the index (not `SEARCH`) because there's no equality/range predicate, but that's exactly what makes `LIMIT 1` cheap: the index is walked from its high end and returns after the first row. Requires the index's `CAST(... AS REAL)` expression text to match the query's byte-for-byte — proven exact-match here (see §4 for why this matters generally). |

---

## 2. Recommended `CREATE INDEX` SQL (proven usage, exact text)

```sql
-- position_lots.list_by_asset / list_all_by_asset
CREATE INDEX idx_position_lots_asset ON position_lots(
  json_extract(payload,'$.asset.asset_class'),
  LOWER(COALESCE(json_extract(payload,'$.asset.venue'),'default')),
  json_extract(payload,'$.asset.native_asset_id'),
  created_at, record_id
);

-- fills.list_by_json_field('trade_intent_id', ...)
CREATE INDEX idx_fills_trade_intent_id ON fills(json_extract(payload,'$.trade_intent_id'), created_at, record_id);

-- settlements.list_by_json_field('trade_intent_id', ...) and ('broker_fill_id', ...)
CREATE INDEX idx_settlements_trade_intent_id ON settlements(json_extract(payload,'$.trade_intent_id'), created_at, record_id);
CREATE INDEX idx_settlements_broker_fill_id ON settlements(json_extract(payload,'$.broker_fill_id'), created_at, record_id);

-- trade_intents.list_by_json_field('broker_order_id', ...)
CREATE INDEX idx_trade_intents_broker_order_id ON trade_intents(json_extract(payload,'$.broker_order_id'), created_at, record_id);

-- fills.list_by_json_time_range('filled_at', ...) -- partial win only, see row #6
CREATE INDEX idx_fills_filled_at ON fills(json_extract(payload,'$.filled_at'), created_at, record_id);

-- scan_runs / integrity_holds status (currently missing entirely)
CREATE INDEX idx_scan_runs_status ON scan_runs(status, created_at, record_id);
CREATE INDEX idx_integrity_holds_status ON integrity_holds(status, created_at, record_id);

-- NEW (found during this audit, not in the original candidate list):
-- position_lots.list_page / paginate_all_rows whole-table keyset pagination
CREATE INDEX idx_position_lots_created ON position_lots(created_at, record_id);

-- equity_snapshots.max_by_json_field('total_equity') -- restructuring turned out
-- unnecessary; a matching expression index works, see row #19
CREATE INDEX idx_equity_total_equity_real ON equity_snapshots(CAST(json_extract(payload,'$.total_equity') AS REAL));
```

Each of the above was confirmed via `EXPLAIN QUERY PLAN` on a disposable copy to
actually be chosen by SQLite's planner (never assumed from the `CREATE INDEX` text
alone) — see the verbatim plans in §1 and the full log in `measurements_output.log`.

**Not recommended, tested and rejected:**
```sql
-- Proven dead weight: the query planner never selects this over the existing
-- 2-column idx_settlements_status(status, created_at) -- see row #17.
CREATE INDEX idx_settlements_status_only ON settlements(status);          -- REJECT

-- Structurally proven to work (row #18) but NOT proven to matter in wall-clock
-- terms at any scale/selectivity tested here -- ship only if profiling on real
-- traffic later shows the residual sort actually costs something.
CREATE INDEX idx_settlements_status_full ON settlements(status, created_at, record_id);  -- OPTIONAL, LOW PRIORITY
```

---

## 3. Indexes explicitly rejected

| Table | Reason | Evidence |
|---|---|---|
| `holdings` (`list_page`/`paginate_all_rows`) | Bounded by distinct-asset count (real prod: 3 rows; synthetic: 40 rows after dedup on `asset_identity_key`), never by trade/fill volume. Will never reach a size where a full scan matters. | Measured 0.133ms at 40 rows; `SCAN holdings` + temp b-tree confirmed structurally identical shape at every scale tested — but the row count itself cannot grow the way fills/settlements do. |
| `integrity_holds` PK `get()` (hot-path guard) | Already served by SQLite's automatic `sqlite_autoindex_integrity_holds_1` on the `record_id` PRIMARY KEY. No gap exists. | `` `--SEARCH integrity_holds USING INDEX sqlite_autoindex_integrity_holds_1 (record_id=?) ``, 0.013ms @ 30 rows. |
| `idx_settlements_status_only(status)` | Proven dead weight — the query planner never chooses it over the existing shipped `(status, created_at)` index, in either the first-page or keyset-continuation form. Pure write-cost, zero read benefit. | Two EQP captures on a copy carrying both indexes, byte-identical to the no-status-only-index plan. See row #17. |
| `trade_intents.exists_with_status_and_asset` | Already adequately served by the shipped `idx_trade_intents_status`; `LIMIT 1` bounds the residual-filter cost regardless of table size. | `` `--SEARCH trade_intents USING INDEX idx_trade_intents_status (status=?) ``. |

---

## 4. Queries needing restructuring instead of (or in addition to) indexing

- **`fills.list_by_json_time_range('filled_at', ...)` (row #6)**: indexing removes
  the full-scan cost but a range predicate on the leading expression column means
  the trailing `(created_at, record_id)` columns can't also serve the `ORDER BY` —
  the temp b-tree persists. No restructuring is proposed (the index is still a
  large net win — the scan is the dominant cost, not the sort), but do not report
  this as "fully solved by indexing" the way rows #1-#5 were.

- **`equity_snapshots.max_by_json_field('total_equity')` (row #19)** — **the plan's
  working assumption here was wrong, corrected by direct measurement.** The
  docstring-level worry ("no index can serve this CAST-based ORDER BY the way a
  plain column can") turned out to be only conditionally true: SQLite's expression
  indexes are matched by exact expression text, and
  `CAST(json_extract(payload,'$.total_equity') AS REAL)` in the index definition
  matches the query's own expression byte-for-byte (both are built from the same
  `_validate_json_field_name`-gated field-name substitution in
  `max_by_json_field`). No restructuring is needed here — a matching expression
  index resolves it completely (202ms → 0.019ms, full scan+sort → index walk with
  `LIMIT 1` short-circuit). **General lesson for future query changes**: this class
  of index is uniquely fragile — any drift between the index's expression text and
  the query's own expression text (e.g. a future refactor that changes the CAST
  target type, or drops/reorders whitespace in a way that changes SQLite's internal
  expression comparison) silently falls back to a full scan with no error. This is
  exactly why §8 recommends a plan-shape regression test.

- No other family requires restructuring; all other `SCAN`s become `SEARCH`
  (fully, i.e. filter + sort both covered) once the matching expression index
  exists — proven, not assumed, per §1.

---

## 5. Expected effect at 10k / 100k (measured, not extrapolated)

Plan **shape** was confirmed structurally identical across 1k/10k/100k for every
unindexed query family (i.e., it is row-count-driven in *cost*, not in *plan
choice* — SQLite doesn't switch strategies at some threshold; it always scans
without a matching index). Measured wall-clock at 100k (the largest synthetic
scale, closest to a multi-year projection of current growth):

| Query | Baseline (SCAN) | Indexed (SEARCH/covering SCAN) | Speedup |
|---|---|---|---|
| `position_lots.list_by_asset` | 293.8 ms | 5.8 ms | 50.9× |
| `fills.list_by_json_field('trade_intent_id')` | 210.3 ms | 0.018 ms | ~11,600× |
| `scan_runs.list_by_statuses('failed')` (10k rows, capped per plan) | 9.0 ms | 2.8 ms | 3.2× |
| `position_lots.list_page` (whole-table, one page) | 67.7 ms | 2.5 ms | 27× |
| `equity_snapshots.max_by_json_field('total_equity')` | 202.3 ms | 0.019 ms | ~10,600× |
| `settlements` keyset continuation, 2-col vs 3-col status index | 3.576 ms | 3.954 ms | **0.9×** (no real benefit at this selectivity — see row #18) |

At current real production scale (§9: single/low-double-digit rows on every
financial-authority table), none of this matters yet — even the 293ms worst case
scales down to microseconds at 18 rows. The projection is for the scale this
system is explicitly designed to reach as trading history accumulates (the
one-scan-run-per-cycle cadence already puts `scan_runs`/`equity_snapshots` at
293-457 rows after a comparatively short live period).

---

## 6. Write-amplification per recommended index

From the call-site analysis (grep across `execution/`, `settlement/`, `scanner/`,
`monitor/`, `reconciliation/`, re-confirmed against current source in this run):

- **`idx_position_lots_asset`** — insert-time-only cost. `asset.asset_class`,
  `asset.venue`, `asset.native_asset_id` are set once at `create_once`
  (`settlement/engine.py:101,122`, `monitor/coordinator.py:144`) and never
  re-derived by any `mutate` call observed (mutate only rewrites P&L/quantity/
  lot-state fields) — confirmed by reading every `position_lots.mutate` call site.
  So this index is maintained once per lot, never touched again.
- **`idx_position_lots_created`** (new) — same profile: `created_at`/`record_id`
  are set once at insert and never updated.
- **`idx_fills_trade_intent_id`**, **`idx_fills_filled_at`** — `fills` is
  append-only (no `update`/`mutate` method exists for it per `TABLES`/`UNIQUE_FIELDS`
  gating in `repositories.py`); insert-time-only cost.
- **`idx_settlements_trade_intent_id`**, **`idx_settlements_broker_fill_id`** —
  `trade_intent_id`/`broker_fill_id` are set once at settlement-event creation and
  never rewritten by `settlements.update` (engine.py:479 rewrites `payload`+`status`
  as a whole, but the underlying `trade_intent_id`/`fill_id` values are immutable
  per the dataclass's own construction path); insert-time-only cost for these two
  indexes specifically, even though `settlements.update` itself runs once per
  settlement-stage transition (that cost lands on the *existing* `idx_settlements_status`
  index, already paid today, not on these new ones).
- **`idx_trade_intents_broker_order_id`** — `broker_order_id` is set once
  (typically at/after submission) and not observed to change across the
  `trade_intents.update` call sites (gateway.py:298,336,383,392,410,418,440,449,
  458,466,487,491,611,622,629; fill_attribution.py:374,381; settlement/engine.py:370)
  — those calls change `status`/other payload fields, not `broker_order_id`. So
  this index, too, is effectively insert-time-only in practice, though the
  underlying B-tree technically re-validates on every UPDATE (SQLite must at least
  check whether an indexed expression's value changed).
- **`idx_scan_runs_status`** — **not** insert-only: `status` transitions from
  `RUNNING`→`COMPLETED`/`FAILED` via `scan_runs.create_once`/`update`
  (`scanner/coordinator.py:517` create, `467,544` finalize) — **2 writes per scan
  run**, both paid by this index, but at low absolute frequency (confirms the
  plan's "lower growth rate" assumption — 299 rows in production today vs.
  thousands of intents/fills at similar operating age).
- **`idx_integrity_holds_status`** — `status` (i.e. `hold_type`) can be upgraded
  from `verification_pending`→`fill_quantity_disputed` via
  `fill_attribution.py:271`'s `update` call, so this index is maintained on that
  (rare) transition too, plus the create/delete lifecycle
  (`fill_attribution.py:189,214,264`, `reconciliation/coordinator.py:458`). Rare by
  construction (disputes are exceptional), so this is a negligible write-cost
  index in practice.
- **`idx_equity_total_equity_real`** — `equity_snapshots` has no `update`/`mutate`
  method (append-only per `TABLES` gating); insert-time-only.
- **Storage overhead measured** (100k-row synthetic file, `idx_*` copy vs.
  baseline `.db` file size): `idx_position_lots_asset` +2.4%, `idx_fills_trade_intent_id`
  +2.2%, `idx_settlements_trade_intent_id`+`idx_settlements_broker_fill_id`
  (combined) +4.3%, `idx_trade_intents_broker_order_id` +2.0%, `idx_fills_filled_at`
  +2.4%, `idx_position_lots_created` (not separately measured, expect similar to
  the others, ~2%), `idx_scan_runs_status` +0.2% (table capped at 10k rows in this
  run), `idx_integrity_holds_status` +0.0% (30 rows). Modest across the board.

**Bottom line on write-amplification**: every recommended index on
`fills`/`settlements`/`trade_intents`/`position_lots`'s *identity/reference*
JSON fields is effectively insert-time-only cost, because those fields are
write-once per row even though the *rows themselves* are updated repeatedly for
unrelated fields (`status`, P&L, lot-state). The two `status`-indexes on
`scan_runs`/`integrity_holds` are genuinely maintained on every transition, but
both tables have low absolute write volume by construction.

---

## 7. Files that would need to change

- `tradepulse/persistence/database.py` (the `SCHEMA` string) — to add the 8
  recommended `CREATE INDEX IF NOT EXISTS` statements from §2 (named only, no diff
  produced — this audit does not modify production files per the task's hard
  constraint).

No other file needs to change: every recommended index matches the query text
`repositories.py` already emits, unmodified.

---

## 8. Tests/benchmarks needed to validate later

- New file (named only, not created): `tests/persistence/test_query_plans.py` —
  parametrized over the query-family/table/index tuples in §1/§2, each asserting
  that `EXPLAIN QUERY PLAN` output for the exact SQL text `repositories.py` builds
  contains `USING INDEX idx_<name>` (and, where claimed in §1, that no `USE TEMP
  B-TREE` line is present). This is the regression guard the task specifically
  asked for: if `repositories.py`'s query text ever drifts from an index's exact
  expression text (e.g. reordering `LOWER(COALESCE(...))`, changing a JSON path,
  or altering `max_by_json_field`'s `CAST` target type), SQLite silently falls
  back to a full scan with **no error** — only a plan-shape assertion would catch
  it before it reaches production traffic. Section §4's `max_by_json_field`
  finding is the concrete motivating example: this class of index is proven to
  work only as long as the expression text matches byte-for-byte forever.
- A lightweight benchmark harness reusing `gen_perf_db.py` (already written to
  the scratchpad, not committed) could be checked in under `scripts/` or
  `tests/persistence/` if the team wants to re-run this audit against future
  schema/query changes without redoing this work from scratch — left as a
  suggestion, not created, since the task scope was audit-and-report only.

---

## 9. Risk verdict

**Not yet** at real current scale. Reused from the prior read-only measurement
(not re-queried, per instructions — no reason to distrust it):

```
trade_intents                6
orders                       0
fills                        18
settlements                  18
holdings                     3
position_lots                18
cash_ledger                  0
pnl_records                  0
reconciliation_records       0
trading_sessions             1
audit_events                 9
scan_runs                    299
equity_snapshots             293
ai_responses                 293
trade_attributions           0
rejected_candidates          457
integrity_holds              ERROR no such table: integrity_holds (table not yet created in this DB file)
opportunities                8
```

Every financial-authority table (`fills`, `settlements`, `position_lots`,
`trade_intents`) is single/low-double-digit rows today — a full scan over 18 rows
is sub-microsecond regardless of plan shape, confirmed by this audit's own timing
methodology (even the *worst* unindexed 100k-row case, 293ms, would be ~0.05ms at
18 rows by the same per-row cost). There is no current latency risk to
settlement/risk cadence from any of these queries today.

**Future risk, now precisely characterized (not just assumed) by this audit**:
- `list_by_asset`/`list_by_json_field`/`list_by_json_time_range` **do** degrade to
  a full `SCAN` + separate sort at every scale tested, confirmed structurally
  identical from 1k to 100k rows (i.e., this is a real, not a hypothetical, risk
  that will bite exactly when these tables cross from "trivial" to "large enough
  that a human notices latency" — measured at 100k rows: up to ~300ms per call
  for `list_by_asset`, ~11,000× slower than indexed for `trade_intent_id` lookups).
- `list_by_statuses` **already** benefits from the shipped `idx_settlements_status`/
  `idx_trade_intents_status` indexes for `settlements`/`trade_intents` (residual
  cost is a same-timestamp-tie sort only, cheap) — confirmed, not assumed.
- `scan_runs`/`integrity_holds` have **no** status index at all, confirmed against
  current `database.py` SCHEMA — and `scan_runs` in particular is already the
  fastest-growing table in production today (299 rows), making its missing index
  the audit's single highest-priority near-term recommendation among the
  currently-unindexed families.
- **One correction to the original working plan, found only by actually running
  the measurement**: `position_lots.list_page`/`paginate_all_rows` (whole-table,
  unfiltered) was assumed low-priority by analogy with `holdings`, but unlike
  `holdings` it is append-only and grows with fill volume — proven to benefit from
  a plain `(created_at, record_id)` index (27× measured at 100k rows, temp
  b-tree eliminated entirely from the plan).
- **Second correction**: `equity_snapshots.max_by_json_field('total_equity')` was
  flagged as needing restructuring (no index could serve it); measurement proved
  a matching `CAST(...)` expression index resolves it completely (~10,600×
  measured), so no restructuring is actually needed there — only the CREATE INDEX
  in §2, plus the plan-shape regression test in §8 to keep it working.

---

## 10. Disposition (Rev.94/95)

Reviewed and **approved** as performance/indexing hardening — this is not
evidence of a correctness defect like FIN-090-01, and does not reopen or
change strategy calibration, risk formulas, or settlement semantics. Approved
with these acceptance conditions, all satisfied before this section was
added:

- All 10 indexes from §2 shipped (the 2 explicitly rejected in §2/§3 —
  `idx_settlements_status_only` and `idx_settlements_status_full` — were not
  shipped), added to `tradepulse/persistence/database.py`'s `SCHEMA` as
  `CREATE INDEX IF NOT EXISTS`, in the audit's own priority order (HIGH
  findings on financial-authority/genuinely-growing tables first):
  `idx_position_lots_asset`, `idx_position_lots_created`,
  `idx_fills_trade_intent_id`, `idx_settlements_trade_intent_id`,
  `idx_settlements_broker_fill_id`, `idx_trade_intents_broker_order_id`,
  `idx_fills_filled_at`, `idx_equity_total_equity_real`,
  `idx_scan_runs_status`, `idx_integrity_holds_status`.
- Added through the normal schema authority (`database.py`'s `SCHEMA`
  string), not a separate migration mechanism — `AsyncSQLiteDatabase.
  initialize()` already runs this whole script, `CREATE INDEX IF NOT EXISTS`
  included, unconditionally on every startup, so an existing database
  receives the missing indexes the next time it initializes, with zero
  schema drift between fresh and pre-existing databases.
- Exact-production-SQL `EXPLAIN QUERY PLAN` regression tests added
  (`python_tests/test_query_plans.py`) — each test captures the literal SQL
  `repositories.py` executes (via `sqlite3.Connection.set_trace_callback` on
  the real repository call, not a hand-retyped copy) and asserts the
  resulting plan uses the intended index, matching §1's claims exactly
  (including that `idx_fills_filled_at` does **not** eliminate the trailing
  temp b-tree — see row #6 — so the test asserts that qualified claim, not
  an overclaimed one).
- A dedicated test proves an already-initialized database (tables but none
  of the 10 new indexes) receives all 10 on the next `initialize()` call —
  not just a freshly created database.
- Full suite re-run, `PRAGMA integrity_check` run, and `sqlite_master`
  checked for the intended index names — both against the real
  `tradepulse.db` (schema-migrated in place; row counts unchanged) and via
  the test suite's application-schema-path fixtures.
- No change to query text, strategy, risk formulas, or settlement semantics
  — index-only.
- The ~3.9 GB of disposable scratch benchmark databases this audit produced
  under `/tmp/.../scratchpad/` were deleted after this report was retained;
  the benchmark scripts/logs were kept for provenance.
