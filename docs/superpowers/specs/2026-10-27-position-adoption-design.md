# Rev.142: adopting unmanaged long positions for protective management — design specification

**Status:** design, rev. 3. Approved for specification drafting: Sections 1–3, reviews ADOPT-01 to ADOPT-08 and REV142-01 to REV142-03, and operator decisions Q1 and Q2 (rev. 2).
**Implementation is blocked** until Rev.140 is implemented, validated and approved (section 3.1), and Rev.143 is merged (3.8a).
**Branch:** `rev142-adoption-design`. No production code, no migration, and no change to the active A0 verification run.

## 1. What happens today (traced in the code, main at `c10bb2fa`)

A broker position with no local holding gets no protection.

**The monitor** (`monitor/coordinator.py`):
- The detection pass (`run_position_monitor`, around lines 436–453) reports a position as `unmanaged_broker_position`, a critical alert sent once per asset per day.
- It **suppresses** that report when the asset and quantity equal the bound opening checkpoint (`opening.get(key) != position.qty`).
- The exit loop then skips any position without a holding (`if holding_row is None: continue`).
- Result: positions inherited by a verification run are **silently** unprotected. In the A0 run these are MSFT 13.252 and SPY 8.842. Other unknown positions are reported but still unprotected.

**The scanner** (`scanner/coordinator.py`, around lines 669–672) refuses new entries in opening-inventory instruments. Exits are sized from the whole broker position, so a new lot would sell the excluded opening quantity too.

**The run's checks** (`valuation.py`):
- The quantity identity subtracts the checkpoint quantity from the broker quantity (around lines 246–248).
- The generation equity excludes `qty × mark − checkpoint market value` for each opening position, and raises `opening_inventory_mark_missing` if the position is gone (around lines 139–149).
- So any sale of inherited inventory today would either latch a mismatch or crash the generation computation.

**Settlement** (`settlement/engine.py`):
- `_entry_protective_levels` (line 266) rebuilds a holding's `stop_loss` and `target_price` from the oldest lot's originating fill and its intent.
- `_project_holding` (lines 280–327) carries the monitor's ratcheted `current_stop` and `scaled_out` forward unchanged, using `mutate()`.

**Protective sells already pass the integrity block:** `risk/session.py::execution_session_decision` returns early for `protective_exit`.

## 2. Requirements and decisions

| ID | Decision |
|---|---|
| O1 | An adopted position hitting its stop is **sold automatically**, under the full exit rules: stop, break-even, trailing stop, R ladder, stall exit and time stop. |
| O2 | The stop and the R ladder are measured from **Alpaca's average entry price**. A stop already breached at adoption sells at the next open, after an alert. |
| O3 | **Every** unmanaged long position is adopted **automatically**, whether inherited or appearing mid-run. |
| O4 | Approach 1: an adopted lot, through the existing monitor, gateway and settlement. No parallel liquidation engine. |
| O5 | The **exact** existing entry stop policy is reused, including its documented percentage fallback. `stop_source` = `atr` or `pct_fallback`. |
| ADOPT-01 | Broker basis, adoption mark, provenance and time are recorded separately. No synthetic fill and no opening cash entry. |
| ADOPT-02 | Adoption does not authorize a sale. Every exit passes Rev.140's sellable-inventory gate. |
| ADOPT-03 | Two classifications: `opening_inventory` and `unexpected_external`. |
| ADOPT-04 | Stop inputs are validated. A breached stop is evaluated in the same monitor cycle. |
| ADOPT-05 | One economic closing event and one cash effect, with two separately labeled P&L measurements that are never summed. |
| ADOPT-06 | Generation equity and cash are conserved through partial and full exits, fees and price moves. |
| ADOPT-07 | The adoption stop is immutable. The holding's `current_stop` is authoritative and never lowered. |
| ADOPT-08 | Lot closure, cash, fee allocation and recovery happen exactly once. Identities are account-qualified, and lot origins are never mixed per account and asset. |
| REV142-01 | The generation-equity exclusion is proven against dividends, capital flows, fee corrections and unrelated trading. All inherited-position performance is excluded from `prove_edge`. |
| REV142-02 | Neither adoption nor full liquidation clears an integrity hold. |
| REV142-03 | A late fee correction after full closure updates both projections exactly once, without reopening inventory or duplicating cash. |
| Q1 | Dividends attributable to adopted inventory are excluded from generation performance and `prove_edge`, and kept in broker cash, ledgers and reconciliation. Attribution needs verified entitlement evidence; when it can't be proven, the measurement is unresolved (fail closed), never an invented allocation (3.8a). |
| Q2 | No strategy BUY of an account-qualified asset that had an adoption during the verification generation, even after full closure. The exclusion is durable across restarts and released only at an authorized new-generation boundary with closure obligations reconciled. No re-entry mechanism outside a generation is introduced by Rev.142 (3.9). |

## 3. Design

### 3.1 Activation and the Rev.140 dependency

- **Setting:** `TRADEPULSE_ADOPT_UNMANAGED_POSITIONS`, a boolean that defaults to `false`.
  - It joins the frozen verification configuration (`context_for(settings)`), so a change mid-run fails `frozen_configuration_mismatch`.
  - With it off, behaviour is exactly as today. That includes the opening-inventory suppression in the detection pass.
- **Startup gate with the setting on.** The runtime refuses to start (`ADOPTION_REQUIRES_REV140`) unless every check below passes in this process. Each one is an operational check, not a module import:
  1. **Rev.140 schema:** the startup trigger verification (Rev.140 §3.11, `SCHEMA_TRIGGER_MISSING`) completed successfully. The same verification covers this Rev's immutable table (3.11).
  2. **Cursor readiness:** a `fill_cursor_bootstrap` row exists for the leased `account_ref` (Rev.140 §3.1a), and the fill cursor has been read successfully once since start.
  3. **Sellable enforcement:** the gateway's SELL path requires `symbol_lock_owner_token` (Rev.140 §3.1b).
     - It is proven by a startup self-check: a SELL request without a token, sent to the gateway's authorization entry point, must return `EXECUTION_RESERVATION_REQUIRED` **before** any broker read.
     - The self-check runs against the real gateway object with a broker wrapper that fails the test if it's called.
- **Before any adoption** the monitor confirms the gate result recorded at startup. It never re-derives it per cycle.

### 3.2 Classification (ADOPT-03)

A broker position is **eligible** only when all of these hold:
- it is long (`qty > 0`);
- the account and asset have **no** holding and **no** open lot of any origin;
- the asset has no open Rev.140 quarantined record (liability, or `unmanaged_long_inventory`);
- its asset class is equity, option or crypto;
- its `asset_key_from_broker_symbol` mapping succeeds.

Everything else keeps today's behaviour, and the reason is recorded:
- **Short:** `UNSUPPORTED_SHORT_POSITION` under Rev.140.
- **Partially managed,** where lots exist but differ from the broker: the existing mismatch.
- **Quarantined:** Rev.140 §3.4.

| Classification | Condition | Source ID |
|---|---|---|
| `opening_inventory` | A **bound opening checkpoint** exists, and it lists this account and asset with exactly this quantity. | `checkpoint_id` |
| `unexpected_external` | Any other eligible position, including every eligible position when no checkpoint is bound. | `episode_id` (3.3.1) |

**Outside a verification run** there is no `opening_inventory`. A runtime-start snapshot only shows what was observed at startup. It isn't evidence of acquisition history, and restarting would launder a mid-run position into "opening" (change log, item 3).

### 3.3 The adoption transaction

#### 3.3.1 Stability episode (`unexpected_external` only)

- The first cycle that observes an eligible unexpected position writes an `unmanaged_observations` row: `episode_id` = `"{account_ref}:{asset_key}:{first_seen_at}"`, `first_qty`, `last_qty`, `last_seen_at`, and `status=observing`.
- A later cycle adopts only if `last_qty` equals the current broker quantity, and `first_qty` equals it too.
- A change in quantity resets the episode: the old row closes `superseded` and a new one opens.
- The episode closes `adopted` when the adoption commits, and `vanished` when the broker quantity reaches 0.
- This is a **stability precondition only**. It is never sale authorization (3.5).

#### 3.3.2 Stop computation (ADOPT-04, O5)

The scanner's inline selection (`scanner/coordinator.py` around lines 870–885) is extracted unchanged into one shared function, used by both the scanner and adoption:

```python
def entry_stop_loss(reference_price: Decimal, candles: list[Candle], asset_class: AssetClass,
                    risk_limits: RiskLimits) -> tuple[Decimal | None, Literal["atr", "pct_fallback", "option_premium_pct"] | None]
```

- **Options:** `options_premium_stop_pct` → `option_premium_pct`.
- **Equity and crypto:** `_atr_stop_loss_price` when `atr_stop_multiplier > 0` and the result is not None (`atr`). Otherwise `_stop_loss_price(stop_loss_pct)` when `stop_loss_pct > 0` (`pct_fallback`).
- Candles come from **the scanner's own candle request** (same provider call and lookback), not the monitor's 30-day `_fetch_atr`. ATR depends on its smoothing start, so a different lookback would be a different policy.
- **Reference price:** `broker_avg_entry_price` (O2).

**Adoption is refused** (`ADOPTION_STOP_INPUT_INVALID`, critical, once per asset per day; the position stays reported as unmanaged) when any of these hold:
- `broker_avg_entry_price ≤ 0`, or the current mark ≤ 0;
- the asset class is unknown;
- the profile fails validation;
- the result is `(None, None)` (both policies disabled).

A failed candle fetch is **not** a refusal. It is the documented fallback, recorded as `pct_fallback`.

**Scanner regression:** for the same inputs, the scanner must produce the same stop as before the extraction (test T1).

#### 3.3.3 What one transaction writes

The write runs in one SQLite write transaction (`database.run(..., write=True)`, `BEGIN IMMEDIATE`). It first re-checks eligibility: no holding and no open lot for the account and asset. If either exists, it does nothing.

1. **`position_adoptions` row (immutable):**
   - `adoption_id`, `account_ref`, `asset_key`, `asset`, `classification`, `source_id`;
   - `adopted_quantity` = broker qty;
   - `broker_avg_entry_price`, `adoption_mark_price` = the position's `current_price` in the same broker read;
   - `adopted_at`, `basis_provenance = "broker_avg_entry_unverified"`;
   - `stop_loss`, `stop_source`, `candles_used` (count, first and last date);
   - `broker_observation`: the raw position, with any account identifiers removed.
2. **`PositionLot`:**
   - `lot_id = "adopted:{adoption_id}"`, `origin = "adopted"`, `adoption_id`;
   - `originating_fill_id = "adoption:{adoption_id}"`. That column is `NOT NULL UNIQUE`; the `adoption:` prefix can never equal a broker fill ID, and every dereference branches on `origin` (3.6.1);
   - `position_side="long"`, `opened_quantity = remaining_quantity = adopted_quantity`;
   - `acquisition_price = broker_avg_entry_price`, `opened_at = adopted_at`.
3. **`Holding`:** quantity and average price from the lot; `stop_loss` from the adoption; `target_price=None`; `current_stop=None`; `scaled_out=False`.
4. **`AuditEvent`** `adopted_position` (warning), with a deterministic ID `adopted_position:{adoption_id}`, delivered through `alert_once`.

5. **Crypto only: the adopted accounting epoch.** An `accounting_epochs` row `crypto_epoch:adopted:{adoption_id}`, with `origin="adopted"`, `owned_quantity = adopted_quantity` and no trade intents (3.9). This is internal classification, like the lot.

**Neutrality (ADOPT-01):** the transaction writes nothing to `fills`, `cash_ledger`, `trade_intents` or `settlements`, and calls no broker method except the reads that came before it.

**Idempotency (ADOPT-08):**
- `UNIQUE(account_ref, asset_key, source_id)` on `position_adoptions` is a real table constraint (3.11).
- When a concurrent second attempt wins the race, the eligibility re-check inside `BEGIN IMMEDIATE` makes the losing attempt do nothing. When both attempts carry the same source ID, the constraint rejects the second, and that is also treated as doing nothing.

### 3.4 Monitor flow

With the setting on, `run_position_monitor` does the following in order:
1. Read the broker positions once, as today.
2. **Adoption pass:** for each eligible position, the steps in 3.3.
3. **Detection pass:** as today, except that the opening-checkpoint suppression is removed. Any position still without a holding is reported as unmanaged. `unexpected_external` positions are always reported, adopted or not; that is the incident record, and REV142-02 applies.
4. **Exit loop:** unchanged. It reads holdings after step 2, so a newly adopted holding is evaluated **in the same cycle**, and a breached stop sells now when the market is open. Outside market hours, the existing `_defer_exit_outside_market_hours` path applies and the alert says "stop already breached — selling when market opens".

The ratchet, extreme folding, `plan_ladder_exit`, stall and time stop all work unchanged. Their inputs are the holding, the lots (`opened_at = adopted_at`) and the price. Stall and time-stop ages therefore count from adoption, not from the unknown original purchase.

### 3.5 Exit authorization (ADOPT-02)

- An adopted exit is an ordinary monitor SELL. It carries the asset's execution reservation token and passes through the gateway's full Rev.140 path: the per-asset serialization of §3.1b and the two sellable bounds of §3.1.
- The adopted lot is counted in *L* (Σ open long lot quantity). That is what lets the exit pass Bound 1, so no special case exists.
- Bound 2 still covers external orders. A refusal keeps the adoption and the holding, records the gateway reason, and sends a critical `ADOPTED_EXIT_REFUSED` alert, deduplicated per asset per hour.
- For `unexpected_external`, the 3.3.1 stability episode must have closed `adopted`. The 2-cycle check is a precondition of adoption, never of the sale.

### 3.6 Settlement

#### 3.6.1 Every `originating_fill_id` dereference

The adopted lot has no fill row. Each site below gets an explicit `lot.origin == "adopted"` branch; any other origin keeps today's code. An unknown origin value raises (fail closed).

| Site | Today | Adopted branch |
|---|---|---|
| `settlement/engine.py:269` `_entry_protective_levels` | fill → intent → (stop, target) | `position_adoptions[adoption_id]` → (`stop_loss`, `None`) |
| `settlement/engine.py:202` attribution opening intent | fill → intent | no opening intent; the attribution carries `origin="adopted"` and `adoption_id` |
| `settlement/lots.py:65` replayed opening quantity | matches `event.fill_id` | adopted lots never match a fill event (prefix), so this is unchanged; test T11 proves it |
| `settlement/accounting.py:63,73` projection evidence | opening fill raw | the adoption row is the opening evidence; quantity and price are checked against it |
| `reconciliation/coordinator.py:152` holding rebuild | fill → intent | adoption row → stop |
| `reconciliation/historical_fees.py:39,47` | origins by fill | adopted lots have no opening fee; closing fees only |
| `reconciliation/fee_replay.py:158` | `asset_fills[...]` | opening side skipped for adopted lots; closing side unchanged |
| `reconciliation/epochs.py:258,276` (crypto epochs) | selected fill IDs | adopted crypto lots form their own epoch; owned quantity includes them (3.9) |
| `verification/evidence.py:151,224,416,432,457,459,463,465,492,522` | `fills[lot.originating_fill_id]` | asset from the adoption row; the opening fill is "none, adopted"; quantity allocation from `adopted_quantity`; the bundle lists adoptions as their own evidence class |

**No adopted lot may be treated as having an originating fill.** No code path may construct, look up or default a fill for an `adoption:` ID.

**Source-level regression guard (test T26).**
- An AST test walks `tradepulse/` and collects every attribute access or subscript of `originating_fill_id` as a `(module, enclosing function)` pair.
- It compares that set with an approved list held in the test. Each entry names the site and the test (T11 or T27) that exercises its adopted branch.
- A new or moved site fails the test until it is added with its adopted-lot test. The two sites that only **write** the field (`settlement/engine.py:100`, `persistence/hydration.py:260`) and the schema and repository registrations are listed as write-only.

The plan must also re-run this inventory with `grep -rn originating_fill_id tradepulse` at implementation time, and fail the task if a site is missing from the table.

#### 3.6.2 Closing an adopted lot (ADOPT-08)

- The closing fill is real: broker order, activity and `fills` row, posted to `cash_ledger` once by the existing fill path.
- Settlement closes lots oldest-first. An adopted asset has exactly one lot (3.6.3), so the closure goes to it.
- **Exactly once:**
  - `lot.closures[fill_id]` already makes replay idempotent;
  - cash is posted once, keyed by the fill;
  - fees use the canonical allocation for ordinary fills (`reconciliation/cash_fees.py` / `reconciliation/fee_replay.py`), including late commissions and corrections.
- `lot.realized_pnl` = Σ over closures of (`fill_price − acquisition_price`) × qty − allocated fees. That is the broker-basis figure, and the lot carries `basis_provenance` through its adoption.
- **`adoption_attributions`** is a projection with one row per `(adoption_id, closing_fill_id)`:
  - `qty`, `fill_price`, `allocated_fees`;
  - `broker_basis_pnl` = (`fill_price − broker_avg_entry_price`) × qty − fees;
  - `managed_pnl` = (`fill_price − adoption_mark_price`) × qty − fees.
  - It is **recomputed** from the lot's closures and the current canonical fee allocation every time settlement or fee replay touches the lot. It is never accumulated.

#### 3.6.3 No mixed origins

- Keyed by `(account_ref, asset_key)`: open lots are all `fill` or all `adopted`.
- Settlement's lot creation for a BUY fill on an asset with an open adopted lot latches `MIXED_LOT_ORIGIN` (an integrity hold) and creates no lot. The scanner block (3.9) is meant to stop this from ever happening.

#### 3.6.4 Full closure and late fees (REV142-03)

- At zero remaining quantity, the existing `_project_holding` deletes the holding. The lot, the adoption row and the attribution rows remain.
- A fee correction arriving afterwards goes through the canonical fee path to the closing fill. Fee replay finds the closed adopted lot through `closures`, recomputes `lot.realized_pnl` and the affected `adoption_attributions` row, and:
  - posts the fee's cash once, through the existing fee cash path;
  - never changes `remaining_quantity`;
  - never recreates the holding.

### 3.7 P&L measurements (ADOPT-05)

| Destination | Fed by | Notes |
|---|---|---|
| `cash_ledger` | the real closing fill and its fees | the **only** cash effect; one per fill and fee |
| `lot.realized_pnl` / realized P&L reports | broker-basis P&L | shown as "Inherited inventory P&L (unverified basis)", apart from strategy P&L |
| `adoption_attributions.managed_pnl` | post-adoption P&L | labeled "Managed since adoption"; projection only, no ledger or equity effect |
| Strategy performance, win rate, trade statistics | — | adopted closes **excluded** (`trade_attributions.origin == "adopted"`) |
| `prove_edge` | — | every asset with an adopted lot (open or closed in the generation) is `ineligible_adopted` |
| Generation equity | 3.8 | **all** inherited-position performance is excluded: before and after adoption |
| Daily P&L and drawdown risk checks | broker equity | unchanged; the losses are real money, so they count as today |

The two P&L figures are never summed. The reports show `broker_basis_pnl − managed_pnl` only as "change before adoption", which equals (`adoption_mark_price − broker_avg_entry_price`) × qty.

### 3.8 Verification-run accounting (ADOPT-06, REV142-01)

Per bound generation, for each `opening_inventory` adoption *a* of asset *k* with checkpoint quantity *q₀* and checkpoint market value *V₀*:

- **Quantity identity:** for *k*, the checkpoint quantity is **no longer subtracted**. `broker_qty(k) = Σ open lots(k)`, where all lots are adopted (3.6.3).
  - Additionally `a.adopted_quantity == q₀`. Otherwise `ADOPTION_CHECKPOINT_MISMATCH` latches. It can't happen through 3.2, so it's a corruption guard.
- **Opening exclusion:**

  `X(k) = R(k) × mark(k) + N(k) − V₀(k)`

  - `R(k)` = remaining quantity of the adopted lot;
  - `mark(k)` = the broker's `market_value / qty`, as today; when `R = 0`, the term is 0 and no mark is needed. This replaces the `opening_inventory_mark_missing` raise for adopted keys;
  - `N(k)` = Σ over the lot's closures of (`fill_price × qty − allocated fees`): net proceeds of the adopted closing fills **only**, identified by `closures` keys.
  - Not-yet-adopted opening positions keep today's formula.
- **Generation equity:** `account.equity − Σₖ X(k) − capital_flows`, structurally unchanged.
  - The net proceeds are already in `account.equity` through cash, and are subtracted once through `N(k)`, so they net to zero.
  - The remaining value is in equity through the position, and is subtracted through `R × mark`.
  - Result: the inherited asset contributes **exactly zero** to generation equity, before, during and after adoption.
- **Generation cash:** unchanged. Expected cash = opening cash + in-run cash movements, and the closing fill is an in-run movement.
- **What `N(k)` never contains:** deposits, withdrawals, interest, dividends, other cash adjustments, and fills of other assets. `N(k)` reads only adopted-lot closures, so they can't be pulled in. Deposits, withdrawals and interest keep their existing classification (`generation_adjustment_totals`). Dividends follow 3.8a.

**Worked example (also test T16).**
- Checkpoint: 10 shares, `V₀` = $1,000.
- Adopted at $110 mark. Sell 4 at $115 with a $2 fee: `N` = $458.
- The remaining 6 are marked at $112: `R × mark` = $672.
- `X` = 672 + 458 − 1000 = **$130**. The broker equity change from this asset is also $130, so the generation's change is $0.

### 3.8a Dividends on adopted inventory (Q1)

**Today (traced, rev. 2 correction).**
- `reconciliation/generation_fees.py::adjustment_receipt` recognizes only `CSD`, `CSW`, `INT`, `JNLC` and `JNLD`. No code creates a `cash_ledger` entry for a dividend activity (`DIV*`), or for dividend withholding.
- Membership (`reconciliation/membership.py::_classify`) still classifies an in-run dividend activity as `in_generation`.
- So the cash arrives at the broker, and the generation's expected cash (opening cash + ledger movements) no longer equals observed cash. `generation_cash` is `mismatch`, and the mandatory invariants fail. **A dividend in a verification run already fails closed today, for any asset.** Rev. 1 wrongly said it stays a `profit_adjustment`.
- Rev.140 §3.10 likewise fails closed on corporate actions.

**Rev. 3: delegated to Rev.143.** Dividend cash recognition and verified attribution are specified separately in `docs/superpowers/specs/2026-10-27-dividend-recognition-design.md` (branch `rev143-dividends-design`), which is scheduled **before** this Rev. Rev.142 therefore:
- **Relies on Rev.143's `adopted` bucket.** A dividend on adopted shares is attributed there: entitlement by the trade date before the ex-date, verified against the announcement and the Alpaca activity to the share and the cent. It is recorded once in the cash ledger and excluded from generation performance through Rev.143's `D_excl`.
- **Never adds dividends to `X(k)`.** The exclusion is defined once, in Rev.143 (DV-I4).
- **Allocates no dividend cash itself.** An unverified dividend is held in Rev.143's `D_unresolved`, so it fails closed there.
- **Gives Rev.143 an `origin` to bucket by.** The adopted lot (`origin="adopted"`) and its `opened_at` are the inputs Rev.143's `adopted` bucket reads.

The rev. 2 fail-closed placeholder (`adopted_income_unresolved`) is retired.

### 3.9 Risk, scanner and epochs

- **Open risk:** `open_portfolio_risk` already sums every holding at `average_price − effective stop`. Adopted holdings now count. That's correct, because the risk is real, and it reduces the budget for new entries (section 6).
- **Re-entry exclusion (Q2).** A strategy BUY is refused for an `(account_ref, asset_key)` when either of these holds:
  1. A bound opening checkpoint lists the asset and it is not yet adopted. This is today's block, unchanged.
  2. A `position_adoptions` row exists for the account and asset, and either:
     - inside a verification generation: its `adopted_at` ≥ the bound generation's `opened_at`, or its `source_id` is the bound checkpoint ID;
     - outside a verification generation: unconditionally.
  - **Durable:** the exclusion is derived from immutable adoption rows and the bound checkpoint, so it survives restarts with no extra state.
  - **Enforced twice:** in the scanner, as a candidate rejection `ADOPTED_ASSET_EXCLUDED`, and in the gateway as the authorization boundary. A BUY there is rejected with `ADOPTED_ASSET_REENTRY_BLOCKED` before any risk evaluation. Protective SELLs are unaffected.
  - **Release:** only at an authorized new-generation boundary, meaning a new bound opening checkpoint. The new checkpoint's binding verifies, for each excluded asset of the previous generation, that its adopted lot is closed and its accounting is settled; otherwise the asset stays excluded in the new generation. Settled means no open intents, the fee status (and crypto epoch) is `reconciled_net`, and there is no `pending` or `unresolved` Rev.143 dividend attribution for the asset.
  - **Outside a verification generation, Rev.142 introduces no release.** An asset adopted outside a run stays excluded from strategy entries for that account until a future Rev designs re-entry. This is a deliberate operational consequence (section 6).
- **Crypto accounting epochs** (`reconciliation/epochs.py`). Adopted inventory has its own epoch, which is excluded from strategy evidence. It is never assigned to a strategy epoch.
  - **Membership.** The adoption transaction creates `crypto_epoch:adopted:{adoption_id}` with:
    - `origin="adopted"`, `adoption_id`, `trade_intent_ids=[]`;
    - `starting_broker_quantity = adopted_quantity`, set at creation, so the `start is None` recovery branch, which sums preceding fills, never runs for it;
    - the generation fields from the bound checkpoint, as `new_epoch` does today.
  - **Exits.** `reserve_epoch` for a protective SELL on an asset with an open adopted lot appends the intent to that adopted epoch. It never appends to a strategy epoch, and never calls `new_epoch` with `broker − opening`. An adopted asset never has a strategy epoch, because of the mixed-origin rule (3.6.3) and the re-entry exclusion.
  - **Lot selection.** Epoch reconciliation selects the adopted epoch's lots by `lot.adoption_id == epoch.adoption_id`, not by `originating_fill_id` (sites `epochs.py:258,276`). A coin-denominated fee that debits the adopted lot (`asset_fee_quantities`) is attributed to the adopted epoch. Strategy epochs exclude adopted lots from their `preceding` computation.
  - **Partial and complete exits.** The epoch stays pending until the adopted lot is closed, every appended intent is terminal and the fees are reconciled. Then it moves to `reconciled_net` as today.
  - **Late fee evidence** after `reconciled_net` follows today's checkpoint supersession (`superseded_checkpoint_ids`) on the adopted epoch.
  - **Restart:** the epoch row is written atomically with the adoption, and `reserve_epoch` is already idempotent per intent.
  - **Evidence:** an epoch with `origin="adopted"` is excluded from `prove_edge` and strategy fee evidence, and reported with the adoption.
- **Rev.141:** the per-profile limits are unaffected. `symbol_entries_today` counts BUY intents only, and adoption creates none.

### 3.10 Integrity holds (REV142-02)

- Adoption never calls a clear, re-enable or resolve function, and neither does settlement of an adopted closing fill.
- An `unexpected_external` position typically latches `FINANCIAL_INTEGRITY_BLOCKED` through the existing quantity mismatch before or during adoption. The block stays until the existing authorized re-enable procedure.
- Protective exits are allowed under the block, as today. New entries stay blocked.
- After full liquidation the quantities match again, but `financial_integrity_manual_reenable_required` stays set. A matched snapshot is not a resolution (test T20).

### 3.11 Schema

All tables are additive, using `CREATE TABLE IF NOT EXISTS`, with no rebuild of existing tables.

| Table | Columns beyond `payload` | Constraint | Mutability |
|---|---|---|---|
| `position_adoptions` | `adoption_id` PK, `account_ref`, `asset_key`, `source_id` | `UNIQUE(account_ref, asset_key, source_id)` | **immutable:** `BEFORE UPDATE` / `BEFORE DELETE` triggers, verified at startup with Rev.140's trigger check |
| `adoption_attributions` | `record_id` PK = `{adoption_id}:{closing_fill_id}`, `adoption_id`, `closing_fill_id` | `UNIQUE(adoption_id, closing_fill_id)` | projection, recomputed |
| `unmanaged_observations` | `episode_id` PK, `account_ref`, `asset_key`, `status` | partial `UNIQUE(account_ref, asset_key) WHERE status='observing'` | mutable until closed |

**`PositionLot`** gains `origin: Literal["fill", "adopted"] = "fill"` and `adoption_id: str | None = None`, in the payload.
- Hydration defaults existing rows to `"fill"`.
- `__post_init__` requires `adoption_id` exactly when `origin == "adopted"`, and requires the `originating_fill_id` prefix `adoption:` exactly then.

**`TradeAttribution`** gains `origin` (default `"fill"`) and `adoption_id`.

**Crypto `accounting_epochs` payloads** gain `origin` (default `"fill"`) and `adoption_id` (3.9). Existing rows hydrate as `"fill"`.

### 3.12 Operator surface

- **Telegram on adoption:** `Adopted MSFT 13.252 (opening inventory) — basis $543.73 (broker, unverified), mark $531.96, stop $500.23 (atr|8% fallback)`.
  - A breached stop appends `stop already breached — selling when market opens` (or `— selling now`).
  - `unexpected_external` adds the existing critical unmanaged alert.
- **Dashboard** (`web/app.py` and the positions view):
  - an "Adopted" badge with classification;
  - the stop and `stop_source`;
  - basis provenance;
  - inherited P&L shown apart from strategy P&L, with managed P&L as a labeled secondary figure.
- **Alerts:**

  | Code | Severity | Deduplication |
  |---|---|---|
  | `ADOPTION_STOP_INPUT_INVALID` | critical | once per asset per day |
  | `ADOPTED_EXIT_REFUSED` | critical | once per asset per hour |
  | `MIXED_LOT_ORIGIN` | integrity hold | — |
  | `ADOPTION_CHECKPOINT_MISMATCH` | integrity hold | — |
  | `ADOPTED_ASSET_REENTRY_BLOCKED` | gateway rejection (no alert) | — |

## 4. Invariants

- **AD-I1 neutrality:** an adoption transaction changes no row in `fills`, `cash_ledger`, `trade_intents` or `settlements`, and makes no broker write.
- **AD-I2 uniqueness:** at most one `position_adoptions` row per `(account_ref, asset_key, source_id)`, and at most one open lot set per `(account_ref, asset_key)` of a single origin.
- **AD-I3 quantity:** for an adopted asset, `broker_qty = remaining adopted quantity`, outside a generation and inside it (3.8).
- **AD-I4 closure:** for every closing fill *f* of an adopted lot, `closures[f]` exists exactly once, its cash is posted exactly once, and Σ closures + remaining = `adopted_quantity`.
- **AD-I5 stops:** `holding.stop_loss == adoption.stop_loss` while the adopted lot is open. `current_stop` is non-decreasing for a long position across settlement, replay and restart.
- **AD-I6 P&L:** for each attribution row, `broker_basis_pnl − managed_pnl == (adoption_mark_price − broker_avg_entry_price) × qty`, and no report sums the two.
- **AD-I7 generation:** an adopted opening asset contributes 0 to generation equity, and `X(k)` contains only that asset's adopted closures and mark.
- **AD-I8 holds:** no Rev.142 code path clears an integrity hold or `financial_integrity_manual_reenable_required`.
- **AD-I9 no synthetic fill:** no `fills` row, lookup or default exists for any `adoption:` ID.
- **AD-I10 re-entry:** no strategy BUY is authorized for an excluded account and asset (3.9).
- **AD-I11 epochs:** adopted crypto inventory belongs only to its adopted epoch; no strategy epoch contains an adopted lot or a protective SELL of one.
- **AD-I12 income:** Rev.142 never allocates dividend or withholding cash. Rev.143 attributes it (bucket `adopted`), and `X(k)` contains no dividends (3.8a).

## 5. Recovery

| Crash point | Outcome |
|---|---|
| Before the adoption commit | Nothing was written; the next cycle retries. |
| After the commit | The next cycle sees the holding and lot, and does nothing. |
| During an exit | The existing gateway and settlement recovery applies (`submission_unknown`, Rev.140 §3.1a cursor), with nothing adoption-specific. |
| During attribution recompute | The projection is recomputed from closures at the next touch; it has no ledger effect. |
| Restart with the setting turned off after adoptions | Adopted holdings and lots stay and stay managed. The setting gates **new** adoptions only, and startup logs the count of open adopted lots. |

## 6. Behaviour changes with the setting on

- Inherited and unknown long positions get stops and full exit management, and can be sold automatically. That's the purpose.
- Open risk rises by the adopted positions' risk to their stops, so new-entry capacity under the profile's `max_open_risk_pct` falls. For example, MSFT at a ~$500 stop on a $543.73 basis adds ~$577 of open risk.
- Opening-inventory positions are no longer silently skipped. If adoption fails, they are reported as unmanaged.
- **Adopted symbols are excluded from new strategy entries** for the rest of the verification generation (Q2). **Outside a verification run the exclusion has no release in Rev.142.** For example, a manual AAPL purchase that gets adopted stops the strategy buying AAPL on that account until a future Rev adds re-entry.
- A dividend on an adopted asset is attributed by Rev.143 to its `adopted` bucket and excluded from run performance (3.8a).
- Reports split strategy P&L from inherited P&L.
- With the setting off, nothing changes. The flag stays off for the remainder of A0 and is frozen in any verification run.

## 7. Tests (deterministic, on `SimulatedBroker` with its clock; no network)

| # | Test |
|---|---|
| T1 | `entry_stop_loss` reproduces the scanner's previous stop for ATR, fallback (no candles; out-of-band ATR) and option inputs; scanner opportunities are unchanged. |
| T2 | Adoption stop uses the broker average entry as reference; `stop_source` recorded; invalid price, asset class or profile is refused with `ADOPTION_STOP_INPUT_INVALID`. |
| T3 | Neutrality: row counts of `fills`, `cash_ledger`, `trade_intents` and `settlements` are unchanged by adoption; the broker write log is empty. |
| T4 | Idempotency: the same monitor cycle twice yields one adoption, one lot and one holding. |
| T5 | Concurrency: two monitor cycles racing on adoption yield exactly one adoption; the loser does nothing (eligibility re-check and UNIQUE). |
| T6 | Classification: checkpoint asset with equal quantity → `opening_inventory`; different quantity, or no checkpoint → `unexpected_external`. |
| T7 | Stability: an unexpected position is adopted only on the second matching cycle; a quantity change resets the episode; a vanished position closes it. |
| T8 | Same cycle: an adopted position already below its stop is sold in the adoption cycle when the market is open, and deferred with the alert when closed. |
| T9 | Short, partially managed and quarantined positions are not adopted; the reason is recorded. |
| T10 | Exit refused by Rev.140 (external open sell order) keeps the adoption and raises `ADOPTED_EXIT_REFUSED` once per hour. |
| T11 | Every `originating_fill_id` site in 3.6.1 handles an adopted lot (one test per site); an unknown origin raises. |
| T12 | Partial sale (scale-out at 1R) keeps `stop_loss`; `current_stop` moves to break-even and is not lowered by settlement. |
| T13 | Ratchet → trail → partial sale → settlement replay → restart: `current_stop` non-decreasing throughout (ADOPT-07). |
| T14 | Exactly-once closure: replay the closing fill and its settlement event; `closures`, cash and attribution are unchanged. |
| T15 | P&L: the review's example (basis $100, mark $110, sale $115 × 10, fee $2) gives broker basis +$148, managed +$48, difference $100; no report sums them. |
| T16 | Generation equity: the worked example in 3.8 before adoption, after the partial sale and after the full sale, with a price move between; generation equity change is $0 and generation cash matches. |
| T17 | REV142-01: a deposit, a withdrawal, interest, a fee correction on another asset and a trade in another asset are absent from `X(k)`, and classified as today. |
| T18 | REV142-03: a fee correction after full closure updates `realized_pnl` and the attribution once; replaying it changes nothing; no lot reopens; one cash effect. |
| T19 | Mixed origin: a BUY fill on an adopted asset latches `MIXED_LOT_ORIGIN` and creates no lot; the scanner refuses the entry earlier. |
| T20 | REV142-02: an unexpected position latches the integrity block; it is adopted and fully liquidated; the block and the re-enable requirement persist. |
| T21 | Strategy exclusion: win rate, trade statistics and `prove_edge` exclude adopted closes; `prove_edge` is `ineligible_adopted`. |
| T22 | Activation: setting on without the Rev.140 gate refuses startup; each of the three operational checks failing alone refuses; setting off is byte-identical to today's monitor decisions on a recorded A0 scenario. |
| T23 | Immutability: update and delete on `position_adoptions` fail; a missing trigger refuses startup. |
| T24 | Evidence bundle: `verification/evidence.py` builds with adopted lots, lists them as their own class and raises no missing-fill error. |
| T25 | End to end (Rev.138 runtime harness): inherited position → adoption → ratchet → scale-out → stop-out; every snapshot's mandatory invariants match. |
| T26 | Source guard: the AST inventory of `originating_fill_id` reads equals the approved list; adding an unlisted read fails the test. |
| T27 | Evidence generation (`verification/evidence.py`) after a partial and after a full adopted-lot closure, for equity and crypto: it builds, lists adoptions as their own class, and contains no fill entry for any `adoption:` ID. |
| T28 | Crypto epochs: adoption creates the adopted epoch atomically; a protective SELL joins it, never a strategy epoch; a coin fee on the adopted lot attributes to it; partial exit stays pending; full exit reconciles; a late fee supersedes once; restart between adoption and SELL changes nothing; `prove_edge` excludes it. |
| T29 | Q2 durability: after full closure, a strategy BUY is refused in the scanner (`ADOPTED_ASSET_EXCLUDED`) and the gateway (`ADOPTED_ASSET_REENTRY_BLOCKED`), across a runtime restart; a new bound checkpoint releases it only when the closure obligations are settled, and keeps it excluded otherwise; outside a generation it is never released. |
| T30 | Q1 with Rev.143: a dividend and its withholding on an adopted asset in a generation are attributed to the `adopted` bucket, excluded through `D_excl`, recorded once in the cash ledger, and absent from `X(k)`; generation equity change from the asset stays $0. |

## 8. Out of scope

- Adopting short positions (Rev.140 keeps them unsupported).
- Merging adopted and system-opened inventory in one asset.
- IBKR. A2 can reuse this once A1 provides account-scoped identities; the keys here are already account-qualified.
- Verifying the historical basis from Alpaca's activity history, which would upgrade `basis_provenance`.
- Turning the setting on during A0 or any other verification run already in progress.

## 9. Open questions

Q1 and Q2 were decided by the operator for rev. 2 (section 2, 3.8a, 3.9). Q3 is answered in rev. 3; Q4 remains for the operator:

- **Q3 (answered in rev. 3).** Dividend recognition is Rev.143, scheduled before Rev.142.
- **Q4.** Outside a verification run, an adopted asset stays excluded from strategy entries with no release (3.9). Is that acceptable until a re-entry Rev, or should re-entry outside runs be designed next?

## 10. Change log

**rev. 3 (2026-10-27).** Dividends delegated to Rev.143.
1. **3.8a:** the fail-closed placeholder `adopted_income_unresolved` and the attribution contract were replaced by a reference to the Rev.143 spec. Rev.143 is scheduled before Rev.142 (order: Rev.140 → Rev.141 → Rev.143 → Rev.142).
2. **Single exclusion:** dividends are excluded through Rev.143's `D_excl`, never through `X(k)`. The rev. 2 wording "adding it to `X(k)`" is removed.
3. **Entitlement:** rev. 2 said record date. Rev.143 uses the trade date before the ex-date, which is correct under T+1.
4. T30, AD-I12, section 6 and the alerts table updated; Q3 answered.

**rev. 2 (2026-10-27).** Operator decisions Q1 and Q2, and the reviewer's forensic requirements 1–5.

1. **Correction:** rev. 1 said a dividend on adopted inventory "stays a generation `profit_adjustment`, exactly as today". That was wrong. `adjustment_receipt` recognizes no dividend activity, so a dividend in a verification run already fails the generation cash check, for any asset. Section 3.8a records the traced behaviour.
2. **Q1 (3.8a):** split into a fail-closed rule inside Rev.142 (`adopted_income_unresolved`, no invented allocation) and an attribution contract for a future dividend-recognition Rev: record-date eligible quantity from verified announcements, withholding, reinvestment and corrections. New test T30; the dividend case was removed from T17.
3. **Q2 (3.9):** re-entry exclusion derived from immutable adoption rows and the bound checkpoint, so it is durable across restarts. It is enforced in the scanner and at the gateway, and released only at a new bound checkpoint with closure obligations settled. No release outside a generation. New test T29; consequence stated in section 6.
4. **Crypto epochs (3.9, 3.3.3 step 5):** a separate adopted epoch, created atomically with the adoption. It covers protective-SELL membership, lot selection by `adoption_id`, coin fees, partial and full exits, late fees, restart and evidence exclusion. New test T28.
5. **Opening-fill lookups (3.6.1):** an explicit no-synthetic-fill rule, and an AST source-level guard (T26). Evidence generation is tested after partial and full closure (T27).
6. Open questions Q3 (dividend-recognition scheduling) and Q4 (re-entry outside runs) added for the operator.

**rev. 1 (2026-10-27).** Written from the approved Sections 1–3 and reviews ADOPT-01 to 08 and REV142-01 to 03.

1. Exact stop policy reuse, including the percentage fallback (operator decision). The selection is extracted into `entry_stop_loss`, with a scanner regression test, so the two cannot drift.
2. `_project_holding`'s `current_stop` carry-forward is reused unchanged. Only `_entry_protective_levels` and the other `originating_fill_id` sites get adopted branches.
3. **Simplification relative to Section 1:** the runtime-start inventory snapshot was dropped. `opening_inventory` exists only with a bound verification checkpoint. Otherwise a restart would reclassify a mid-run position as opening, and the reviewer noted that a startup snapshot isn't evidence of acquisition history.
4. The candle source for adoption ATR is pinned to the scanner's request, not the monitor's 30-day fetch, because ATR smoothing depends on lookback.
5. The full `originating_fill_id` inventory is enumerated (twenty sites, including nine in `verification/evidence.py`). This was found while drafting, and was not in the reviewed sections.
6. Crypto accounting epochs get an adopted-lot rule (3.9). Found while drafting.
7. Startup gate: three operational checks, including a reservation-token self-check against the real gateway (REV142 activation note).

## 11. Evidence required for implementation approval

- Rev.140 implemented, merged and its acceptance gates passed. Rev.143 merged. A0 concluded with its evidence preserved.
- The Rev.142 plan reviewed, with the `originating_fill_id` inventory re-run on the plan's base commit.
- All T1–T30 pass; the full Python suite, mypy (project config) and Ruff (changed files) are reported exactly, along with the frontend tests for the dashboard badge.
- T22's recorded-scenario comparison is attached as evidence that the setting off changes nothing.
