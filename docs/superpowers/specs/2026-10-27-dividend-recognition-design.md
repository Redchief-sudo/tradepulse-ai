# Rev.143: dividend cash recognition and verified attribution — design specification

**Status:** design, rev. 1, written from the operator-approved Sections 1–3 (2026-10-27).
**Order of work:** Rev.140 → Rev.141 → **Rev.143** → Rev.142. Rev.142 rev. 3 depends on this spec (section 3.9).
**Branch:** `rev143-dividends-design`. No production code, no migration, and no change to the active A0 verification run.

## 1. What happens today (traced in the code, main at `c10bb2fa`)

- **Activities are fetched, but dividends are never recorded.** The reconciliation lane fetches all activity types for the run's population (`reconciliation/activity_cursor.py`, `activity_type=None`). Membership (`reconciliation/membership.py::_classify`) classifies an in-run dividend activity as `in_generation`.
- **Cash recording covers fees, transfers and interest only:**
  - `persist_generation_fees` (`reconciliation/generation_fees.py:82`) records cash fees (`FEE`, `CFEE`);
  - `_persist_generation_adjustments` (line 196) records `CSD`, `CSW`, `INT`, `JNLC` and `JNLD` (`adjustment_receipt`, line 143).
  - There is no code for any `DIV*` type; `grep -rn "'DIV" tradepulse` finds nothing.
- **So the cash check fails.** `valuation.py` (around lines 124–137) computes `expected_cash = opening cash + Σ cash_ledger`. A dividend changes Alpaca's cash, but no ledger entry is written, so `generation_cash` is `mismatch`. The mandatory invariants fail, and every later snapshot is `INCOMPLETE_EVIDENCE` for the rest of the run.
- **Today's effect:** a dividend paid during a verification run on **any** held asset makes the run unable to show clean books. Nothing is recorded with a reason; it surfaces only as an unexplained cash gap.
- **Corporate actions:** Rev.140 §3.10 refuses resolution of quarantined inventory on an instrument with a corporate-action activity, and sets an evidence minimum (raw broker records with SHA-256). No announcement code exists.

## 2. Requirements and decisions

| ID | Decision |
|---|---|
| D-1 | Scope: **verified attribution**. Record the cash, match it to entitled shares using Alpaca's announcements, check it to the share and the cent, and split strategy from inherited. |
| D-2 | Approach A: **cash first, attribution after.** Recording depends only on a valid receipt. Attribution can be pending or fail without affecting the cash check. |
| D-3 | Order: Rev.140 → Rev.141 → Rev.143 → Rev.142. |
| D-4 | Entitlement uses **shares held at the start of the ex-date**, not the record date. US stocks settle T+1, so the ex-date normally equals the record date, and a share bought on it is not entitled. |
| D-5 | Inherited results are excluded from run performance and `prove_edge`. That covers adopted shares, unadopted opening inventory, and shares held before the run. All cash stays in broker cash, the ledger and reconciliation (Rev.142 Q1). |
| D-6 | Unproven attribution fails closed. It is never an invented allocation. |
| D-7 | No feature flag. Recording replaces a failure that already happens. |
| D-8 | Alpaca's real fields and rounding are measured before implementation (G0). |

## 3. Design

### 3.1 Recognized activity types

The starting list is confirmed at G0 (3.8).

| Group | Types | Sign | `economic_type` |
|---|---|---|---|
| Income | `DIV`, `DIVCGL`, `DIVCGS`, `DIVTXEX` | > 0; < 0 is a reversal (3.5) | `dividend_income` |
| Withholding | `DIVNRA`, `DIVTW`, `DIVFT` | < 0; > 0 is a withholding reversal | `dividend_withholding` |
| Unsupported | `DIVROC` (return of capital changes cost basis), and any other type starting with `DIV` | any | `dividend_unsupported` |

An unsupported activity has its **cash recorded**, so the cash check balances, and raises a critical `DIVIDEND_TYPE_UNSUPPORTED` alert once per activity. Its attribution is `unresolved` with no expiry.

### 3.2 Receipt validation

`dividend_receipt(raw) -> DividendReceipt | None` lives in a new module `reconciliation/dividends.py`, beside `adjustment_receipt`. It returns None for non-`DIV*` types. For a `DIV*` type it requires:
- `currency == "USD"` and `status == "executed"`;
- `net_amount` a string, an exact `Decimal`, not zero, with the sign the group allows;
- `symbol` present, and mapping through `asset_key_from_broker_symbol` to an equity asset key;
- `id` present.

`qty`, `per_share_amount` and `date` are parsed when present. Their absence doesn't block recording; it makes attribution `unresolved` (3.4).

Failure raises `DIVIDEND_RECEIPT_INVALID:{id}`. Nothing is recorded, the cash check stays `mismatch` exactly as today, and the error names the activity.

### 3.3 Recording (exactly once per Alpaca activity ID)

A new `_persist_generation_dividends(connection, activities, membership, *, now)` is called from `persist_generation_fees` right after `_persist_generation_adjustments` (line 122), inside the caller's atomic transaction. For each activity whose membership is in `ELIGIBLE_MEMBERSHIPS` and whose `dividend_receipt` is not None:

1. **Cash:** a `cash_ledger` entry `broker:dividend:{id}` for `net_amount` USD, at the activity's time, with the note `immutable broker dividend; dividend-accounting-v1`.
2. **Record:** a `reconciliation_records` row `generation_dividend:{id}`, of type `dividend`, outcome `matched`, with `actual` holding:
   - the raw activity and its SHA-256;
   - `economic_type` and `cash_amount`;
   - `cash_entry_id`;
   - the checkpoint identifiers, as `_adjustment_actual` has today.
3. **Status:** a `dividend_attribution_status` row, `pending`, with a deadline 3 trading days after the receipt (`unresolved` immediately for `dividend_unsupported`).

**On replay,** an existing record is re-validated against the receipt and its cash entry, as `validate_generation_adjustment` does. A difference raises `GENERATION_DIVIDEND_RECEIPT_OR_CASH_MISMATCH`. A changed receipt under the same ID is already refused by the activity inbox (`BROKER_ACTIVITY_RECEIPT_CHANGED`).

**Membership is unchanged:**
- a dividend from before the run is `pre_generation` and not recorded;
- `unresolved_generation_membership` raises `GENERATION_DIVIDEND_MEMBERSHIP_UNRESOLVED`, as the fee path does.

**Valuation re-validation:** `valuation.py`'s eligible-activity loop (lines 124–132) adds a dividend branch, `validate_generation_dividend`, beside the fee and adjustment branches.

**Totals:** `generation_adjustment_totals` gains a third key, `dividend_income` (the net of income and withholding). It is never added to `capital_flows`.

### 3.4 Attribution

`attribute_dividends(connection, *, now)` runs in the reconciliation cycle after recording, for every `pending` activity.

1. **Announcement.**
   - Fetch Alpaca corporate-action announcements for the symbol: `ca_types=dividend`, over the window `[activity date − 90 days, activity date]`. That's the measured maximum window (evidence `2026-10-26-r7-api-semantics`).
   - Store each announcement raw in `corporate_action_announcements` with its SHA-256.
   - The match must be **exactly one** announcement with all of: sub-type cash; the same symbol; `payable_date` = the activity's payment date (as G0 establishes which field that is); rate = `per_share_amount`.
   - Zero or several matches: retry next cycle until the deadline.
   - `ex_date > payable_date` (a special dividend paid before going ex, settled with due bills): `unresolved` (`DIVIDEND_SPECIAL_EX_DATE`).
2. **Entitlement by trade date.** A fill counts as before the ex-date when its **trade date** is earlier than `ex_date`. The time of day is not used: a pre-market buy on the ex-date is not entitled, and an after-hours buy on the evening before is.
   - The trade date is the broker-reported trading date of the execution when Alpaca supplies one (measured at G0).
   - Otherwise it is the America/New_York calendar date of `transaction_time`.
   - A fill executed in Alpaca's overnight session (20:00–04:00 ET) belongs to the next trading day's trade date. Without a broker-reported trade date, a fill between 20:00 and 24:00 ET on the day before `ex_date` is **ambiguous**, and the activity is `unresolved` (`DIVIDEND_TRADE_DATE_AMBIGUOUS`).
   - Below, "before `T_ex`" means "trade date earlier than `ex_date`".
3. **Eligible quantity by bucket:**
   - **`strategy`**, per lot with `origin="fill"`: `opened_quantity` if the lot's opening fill executed before `T_ex`, minus Σ closures whose closing fill executed before `T_ex`, but not below zero.
   - **`adopted`**, per lot with `origin="adopted"` (Rev.142): the same rule. Before Rev.142 exists, no such lots.
   - **`opening_unadopted`:** when the bound opening checkpoint opened before `T_ex`, its quantity for the asset, minus any quantity already represented by an adopted lot.
   - **`pre_generation`:** when `T_ex` is before the bound checkpoint's `opened_at`, the **whole** activity goes here and the lot buckets are not computed. The shares were held before the run.
   - Outside a verification run there are no `opening_unadopted` or `pre_generation` buckets. Any eligible broker quantity not in lots leaves the activity `unresolved` (`DIVIDEND_ELIGIBLE_QUANTITY_UNEXPLAINED`).
4. **Cross-checks against Alpaca.** Each must be exact; any failure leaves the activity `unresolved` with the named reason:
   - Σ bucket quantities = the activity's `qty` (`DIVIDEND_QUANTITY_MISMATCH`);
   - the announcement rate = `per_share_amount` (`DIVIDEND_RATE_MISMATCH`);
   - `net_amount` = round(`qty × rate`, cent) under the rounding rule measured at G0, with no tolerance (`DIVIDEND_AMOUNT_MISMATCH`).
   - If `qty` or `per_share_amount` is missing: `DIVIDEND_RECEIPT_INCOMPLETE`.
5. **Withholding.** Each withholding activity is paired with exactly one verified income activity: same symbol, same payment date, same announcement. It is attributed in the income activity's proportions. If the income activity isn't verified, or the pairing isn't unique, the withholding stays pending and is unresolved at its deadline.
6. **Exact split.** The activity's `net_amount` is split across lots and buckets in proportion to eligible quantity, using the largest-remainder method on cents, with ties broken by `lot_id`, so the parts sum **exactly** to `net_amount`. Each part is one immutable `dividend_attributions` row: `(activity_id, bucket, lot_id or bucket name, eligible_qty, amount, announcement_id, announcement_sha256)`. The status then becomes `verified`.
7. **Deadline.** An activity still `pending` at its deadline becomes `unresolved` with the last reason, and raises a critical `DIVIDEND_ATTRIBUTION_UNRESOLVED` alert once per activity.

**Quarantined instruments (Rev.140 §3.10):** if the instrument has open quarantined inventory, the activity is `unresolved` (`DIVIDEND_ON_QUARANTINED_INSTRUMENT`). The cash is still recorded, and the quarantine hold stays.

### 3.5 Reversals and corrections

- A reversal (income with `net_amount < 0`, or withholding with `> 0`) must match exactly one **verified** original: same announcement, same `qty`, and an amount equal and opposite.
- It is then attributed with the original's rows, sign flipped, row for row, and the status becomes `verified`.
- A partial or unmatched reversal is `unresolved` (`DIVIDEND_REVERSAL_UNMATCHED`).
- A reissue after a reversal is a new activity, attributed normally.
- No attribution row is ever updated or deleted. Corrections are additional rows.

### 3.6 Where the money goes

| Bucket | Run equity | Strategy P&L, trade statistics, `prove_edge` |
|---|---|---|
| `strategy` | included | included, as a separate "dividend income" component on the lot's trade attribution, never mixed into price P&L |
| `adopted` | excluded via `D_excl` | excluded |
| `opening_unadopted` | excluded via `D_excl` | excluded |
| `pre_generation` | excluded via `D_excl` | excluded |
| pending or unresolved | held in `D_unresolved`; equity status unresolved for the asset | excluded until verified |

The cash is recorded once, whatever the bucket (3.3).

### 3.7 Run equity

`generation_equity = account.equity − Σₖ X(k) − capital_flows − D_excl − D_unresolved`

- `X(k)` is today's opening-inventory exclusion. Under Rev.142 it is the adopted form, and it contains **no** dividends.
- `D_excl` = Σ `dividend_attributions.amount` for buckets `adopted`, `opening_unadopted` and `pre_generation`.
- `D_unresolved` = Σ `net_amount` of `pending` or `unresolved` activities.
- When `D_unresolved ≠ 0`, `generation_equity.status = "unresolved_dividend_attribution"` with the activity IDs, and the mandatory invariants are not `matched`, so the snapshot is `INCOMPLETE_EVIDENCE`.
- The broker equity check (cash + holdings = Alpaca equity) is unchanged. The dividend is in Alpaca's cash, and the ledger now matches it.

**Worked example (test T21).**
- The run opened holding 13.252 MSFT (unadopted). The strategy bought 10 AAPL before AAPL's ex-date.
- MSFT pays $0.91 a share, $12.06 (`qty` 13.252, `pre_generation` no: the ex-date was after the run opened). AAPL pays $0.26 a share, $2.60.
- Cash ledger +$14.66, so the cash check matches.
- `D_excl` = $12.06 (`opening_unadopted`), so run equity includes **$2.60**.

### 3.8 G0: measure before implementation (read-only)

These are recorded in an evidence directory with a manifest, as in `2026-10-26-r7-api-semantics`. Read-only Alpaca calls only, with no orders.

1. `GET /v2/account/activities?activity_types=DIV,DIVCGL,DIVCGS,DIVTXEX,DIVNRA,DIVTW,DIVFT,DIVROC` on the paper account: the field names and types, which date `date` is, and whether `qty` and `per_share_amount` are present.
2. `GET /v2/corporate_actions/announcements?ca_types=dividend` for the held symbols: the field names, the `cash` rate format, and the ex, record and payable dates.
3. The rounding rule, derived from (1) and (2) when samples exist.
4. Whether Alpaca's newer corporate-actions endpoint (market data) is the better source. Record it; don't switch without a review.
5. Whether fill activities carry a trade date distinct from `transaction_time`, and how overnight-session fills are dated (3.4 step 2).

**If the paper account has no dividend activities:**
- every fixture cites its source (Alpaca documentation URL and date, or a recorded sample);
- the first real dividend on the paper account is a **verification event**: its raw receipt is compared field by field with the fixtures before attribution is trusted;
- until then, a receipt whose fields differ from the fixtures is `unresolved` (`DIVIDEND_RECEIPT_SHAPE_UNVERIFIED`).

### 3.9 Effect on Rev.142 (rev. 3 edit, made on `rev142-adoption-design`)

- **3.8a part 1** (the fail-closed placeholder, `adopted_income_unresolved`) is retired. Adopted dividends are attributed by Rev.143's `adopted` bucket and excluded through `D_excl`.
- **3.8a part 2** (the attribution contract) is satisfied by this spec. It is replaced by a reference to it.
- **`X(k)` never contains dividends.** The rev. 2 wording "adding it to `X(k)`" is removed, so the exclusion is defined once.
- **T30** becomes: a dividend on an adopted asset is attributed to `adopted`, excluded through `D_excl`, and recorded once in the ledger.
- **AD-I12** becomes: Rev.142 never allocates dividend cash itself; Rev.143 does.
- **Q3 is answered:** dividend recognition is Rev.143, scheduled before Rev.142.

### 3.10 Schema

All additions use `CREATE TABLE IF NOT EXISTS`, with no rebuild of existing tables.

| Table | Unique | Mutability |
|---|---|---|
| `corporate_action_announcements` | `(announcement_id, sha256)` | immutable; `BEFORE UPDATE` / `BEFORE DELETE` triggers verified at startup with Rev.140's trigger check |
| `dividend_attributions` | `(activity_id, bucket, holder_id)`, where `holder_id` = `lot_id` or the bucket name | immutable, with triggers as above |
| `dividend_attribution_status` | `activity_id` PK | mutable: `pending`, `verified` or `unresolved`; `reason`; `deadline`; `updated_at` |

The cash entries reuse `cash_ledger`, and the records reuse `reconciliation_records`.

`TradeAttribution` gains `dividend_income: Decimal = 0`, a projection recomputed from `dividend_attributions` for the lot.

### 3.11 Operator surface

| Alert | Severity | Deduplication |
|---|---|---|
| `DIVIDEND_TYPE_UNSUPPORTED` | critical | once per activity |
| `DIVIDEND_ATTRIBUTION_UNRESOLVED` | critical | once per activity |
| `DIVIDEND_RECEIPT_INVALID` | critical | once per activity per day (the cash check also fails) |

- **Telegram on verification:** `Dividend MSFT $12.06 — inherited (excluded from run performance)`, or `AAPL $2.60 — strategy`. Info level.
- **Dashboard:** dividend income per position, labeled by bucket; strategy dividend income shown as its own line in performance.

## 4. Invariants

- **DV-I1 cash:** every eligible `DIV*` activity with a valid receipt has exactly one `cash_ledger` entry equal to its `net_amount`, whatever its attribution status.
- **DV-I2 conservation:** for a `verified` activity, Σ `dividend_attributions.amount` = `net_amount` exactly, and Σ eligible quantities = `qty` exactly.
- **DV-I3 entitlement:** no lot whose opening fill's trade date is on or after `ex_date` receives an attribution. A lot whose closing fill's trade date is on or after `ex_date` keeps its entitlement.
- **DV-I4 single exclusion:** inherited dividend amounts are excluded once, through `D_excl`, and never through `X(k)`.
- **DV-I5 no invention:** no attribution row exists for an activity that is not `verified`. Unresolved amounts are in `D_unresolved`.
- **DV-I6 immutability:** announcement and attribution rows are never updated or deleted. Reversals are new rows.
- **DV-I7 Rev.140:** a quarantined instrument's dividend is never attributed while the quarantine is open.

## 5. Recovery

| Crash point | Outcome |
|---|---|
| Before the recording commit | Nothing written; the next cycle records it. |
| After recording, before attribution | The status is `pending`; the next cycle attributes. The cash check already matches. |
| During attribution | The attribution rows and the `verified` status are written in one transaction; on retry, a `verified` activity is skipped. |
| Announcement fetch failing | Stays `pending` until the deadline, then `unresolved`. |

## 6. Behaviour changes

- In a verification run, a dividend on a held stock no longer breaks the cash check. Run equity includes strategy dividends and excludes inherited ones.
- An unproven dividend still blocks clean evidence, but with a named reason and a critical alert instead of an unexplained cash gap.
- Strategy trade P&L gains a separate dividend component.
- Before G0 confirms the receipt shape, the first real dividend may be `unresolved` (`DIVIDEND_RECEIPT_SHAPE_UNVERIFIED`) by design.

## 7. Tests (deterministic; `SimulatedBroker` with its clock; recorded announcement fixtures; no network)

| # | Test |
|---|---|
| T1 | A `DIV` activity in the run writes one ledger entry, one record and a `pending` status; the cash check matches. |
| T2 | Replaying the same activity changes nothing; a changed receipt under the same ID is refused. |
| T3 | Invalid receipt (each field rule) raises `DIVIDEND_RECEIPT_INVALID` and writes nothing. |
| T4 | `DIVROC` and an unknown `DIV*` type: cash recorded, `unresolved`, alert once. |
| T5 | Pre-run dividend activity is not recorded; unresolved membership raises. |
| T6 | Announcement matching: one match → verified; zero → pending then unresolved at the deadline; two → unresolved; amended announcement → the version used is recorded. |
| T7 | Special dividend (`ex_date > payable_date`) → `unresolved` (`DIVIDEND_SPECIAL_EX_DATE`). |
| T8 | Entitlement by trade date: bought the day before (including after hours) is entitled; bought pre-market on the ex-date is not; sold on or after the ex-date keeps it; sold before loses it; an overnight-session fill without a broker trade date → `DIVIDEND_TRADE_DATE_AMBIGUOUS`. |
| T9 | Quantity cross-check mismatch → `DIVIDEND_QUANTITY_MISMATCH`. |
| T10 | Amount off by one cent → `DIVIDEND_AMOUNT_MISMATCH`; no tolerance. |
| T11 | Withholding paired and split in the income's proportions; unpaired withholding unresolved at its deadline. |
| T12 | Largest-remainder split over three lots sums exactly; ties are broken by `lot_id`. |
| T13 | Bucket `opening_unadopted`: excluded through `D_excl`. |
| T14 | Bucket `pre_generation`: `T_ex` before the run opened → whole activity excluded. |
| T15 | Strategy bucket: appears in the trade attribution's `dividend_income`, not in price P&L; counted in run equity. |
| T16 | Outside a run, unexplained eligible quantity → `DIVIDEND_ELIGIBLE_QUANTITY_UNEXPLAINED`. |
| T17 | Reversal of a verified dividend: mirror rows; partial reversal unresolved; reissue attributed as new. |
| T18 | Pending and unresolved: `D_unresolved` set, equity status unresolved, snapshot `INCOMPLETE_EVIDENCE`; verification clears `D_unresolved`. |
| T19 | Quarantined instrument: cash recorded, `DIVIDEND_ON_QUARANTINED_INSTRUMENT`, hold kept. |
| T20 | Immutability: update and delete on announcements and attributions fail; a missing trigger refuses startup. |
| T21 | End to end: the worked example in 3.7; cash check matches; run equity includes $2.60 only. |
| T22 | Recovery: crash after recording, before attribution; restart attributes once. |
| T23 | Receipt shape differing from the fixtures → `DIVIDEND_RECEIPT_SHAPE_UNVERIFIED`. |

## 8. Out of scope

- Return of capital, stock dividends, reinvestment, spin-offs, splits and mergers. Return of capital fails closed; the others belong to Rev.140 §3.10's future procedure.
- Dividends on short positions (payments in lieu), which Rev.140 handles.
- IBKR dividends. A2 reuses the announcement store (D12), but IBKR's own activity types are A2's concern.
- Accrual accounting (a receivable on the ex-date). It would disagree with Alpaca's equity, which has no receivable.

## 9. Open questions

- **Q1.** If G0 finds that `date` on a `DIV` activity is not the payable date, the matching key changes. G0 decides, and the spec is revised before the plan.
- **Q2.** The 3-trading-day deadline is a judgement. Is it right for the operator, or should unresolved be raised sooner?

## 10. Change log

**rev. 1 (2026-10-27).** First written version, from approved Sections 1–3.
- **Section 2 correction:** entitlement uses the ex-date (D-4), not the record date. Under T+1, a share bought on the record date is not entitled.
- **Self-review correction:** the section 3 draft used an instant (09:30 ET on the ex-date). Entitlement follows the **trade date**, so pre-market and overnight-session fills are handled explicitly (3.4 step 2, T8).
- G0 also measures whether Alpaca reports a trade date on fills (3.8).
- **Single exclusion:** dividends are excluded through `D_excl`, never through Rev.142's `X(k)` (DV-I4), with Rev.142 rev. 3 edited to match.

## 11. Evidence required for implementation approval

- Rev.140 and Rev.141 merged. G0 measurements recorded with a manifest, and the spec revised if they differ.
- All T1–T23 pass; the full Python suite, mypy (project configuration) and Ruff (changed files) are reported exactly.
