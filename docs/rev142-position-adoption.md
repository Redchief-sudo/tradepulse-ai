# Rev.142 — Adopting unmanaged long positions for protective management

**Spec:** `docs/superpowers/specs/2026-10-27-position-adoption-design.md` (rev. 5)
**Branch:** `rev142`, stacked on Rev.143.
**Commits:**
- `2d4d302e`: implementation;
- `53ff9843`: the new-generation release condition;
- this record.

With `TRADEPULSE_ADOPT_UNMANAGED_POSITIONS=true`, every eligible long broker position that has no holding and no lot is adopted. It becomes an `adopted:<id>` lot plus a holding, with a stop from the scanner's own policy measured from Alpaca's average entry price. The existing monitor, gateway and settlement then protect it and can sell it under the full exit rules. With the setting off (the default), nothing changes (T22).

## What changed

| Spec | Change |
|---|---|
| §3.1 | Startup gate. Three checks: Rev.140's schema triggers, the fill cursor bootstrapped for the leased account, and a token-less SELL self-check refused before any broker call. It runs at `run` startup, and once per process before the first adoption. A failure refuses with `ADOPTION_REQUIRES_REV140`. |
| §3.2, §3.3.1 | `opening_inventory` requires a bound checkpoint at exactly this quantity; otherwise the position is `unexpected_external`, adopted on its second matching sighting. Episodes go `observing` → `adopted` / `superseded` / `vanished`. Short, partially managed and quarantined positions are never adopted. |
| §3.3.2 | `entry_stop_loss`, extracted unchanged from the scanner and shared with it. T1 compares it with the old inline expression across profiles and inputs. |
| §3.3.3 | One transaction writes the adoption row, lot, holding and audit event, plus an adopted epoch for crypto. It writes no fill, cash, intent or settlement. A concurrent loser does nothing. |
| §3.4, §3.5 | The monitor adopts before it reads holdings, so a breached stop sells in the same cycle, or is deferred outside market hours. Exits pass Rev.140's gate unchanged. A refused exit raises `ADOPTED_EXIT_REFUSED`, once per asset per hour. |
| §3.6 | Every `originating_fill_id` read branches on origin. T26 is an AST guard over the approved site list, and T32 runs a real adopted lot with fill lookups set to fail. Attributions carry no opening intent. The `adoption_attributions` projection holds broker-basis and managed P&L, recomputed every pass and never summed. A BUY fill on an adopted asset latches `MIXED_LOT_ORIGIN`. |
| §3.8 | An adopted opening asset is no longer subtracted from the broker quantity. `X(k) = R × mark + N − V₀` (T16, the worked example: $130), so the asset contributes $0 to run equity. `ADOPTION_CHECKPOINT_MISMATCH` is the corruption guard. |
| §3.9 | Re-entry: `ADOPTED_ASSET_EXCLUDED` in the scanner and `ADOPTED_ASSET_REENTRY_BLOCKED` at the gateway. Released only at a new checkpoint, once the lot is closed with no open intent and no open dividend. Crypto has its own adopted epoch: protective SELLs join it, and strategy entries are refused. `prove_edge` is `ineligible_adopted`. |
| §3.8a | Rev.143: an adopted opening holding's dividend goes to bucket `adopted`, and adopted exits before an ex-date reduce the inherited quantity. |

## Deviations and findings (for the reviewer)

1. **Cross-spec finding (TP145-01): `unexpected_external` adoption is monitored and quarantined, not automatically exit-capable.** Its originating broker FILL is not TradePulse's, so Rev.140 records it as an unresolved broker execution and refuses every sell of the asset until exact evidence resolves it. No Rev.140 outcome type covers "adopted". The position is protected (stop and monitoring), but its exit is refused with `ADOPTED_EXIT_REFUSED` until the operator resolves the execution.
   - Opening inventory is unaffected: the cursor bootstrap's baseline explains it.
   - The 24 h run's planned position is opening inventory: the 1-share buy is placed before the bootstrap. The tests use the same setup.
   - A future Rev should add an `adopted_position` outcome, or the reviewer should confirm this behaviour.
2. **Identity is derived, not stored.** `origin` and `adoption_id` are properties computed from `lot_id = adopted:<id>` and `originating_fill_id = adoption:<id>`, and `__post_init__` checks that the two agree. The spec stores them in the payload. Deriving them keeps every existing lot payload byte-identical, so the Rev.140 replays and digests are unaffected, and no path can drop the identity (AD-I13). `TradeAttribution.origin` is derived the same way, from its `lot_id`.
3. **Crypto is not adopted (TP145-02).** `_eligible` refuses it with `CRYPTO_ADOPTION_UNSUPPORTED`, so it stays reported as unmanaged, as before Rev.142. Historical fee replay and fee replay are built around an opening fill, and spec §3.9's coin-fee attribution to the adopted epoch is not built. Their `ASSET_FEE_ADOPTED_LOT_UNSUPPORTED` checks stay as a second guard.
4. **Adopted closing fees (TP145-03)** in `N(k)` and the P&L projection come from `closing_fee_allocation`. It takes each fill's own fee, plus each broker fee receipt exactly once: a receipt naming a fill goes to that fill, and a receipt naming only an order is split across that order's fills by quantity (largest remainder).
5. **The startup gate is cached per process,** and runs at `run` startup and before the first adoption of a `scan` or `monitor` command.
6. **`unexpected_external` adoption IDs** are a hash of the episode ID; opening-inventory IDs are a hash of the checkpoint and asset. The `UNIQUE(account_ref, asset_key, source_id)` constraint is unchanged.
7. **Not built:** the dashboard badge and P&L split (§3.12), and T11 as one test per site (the T26 guard and the runtime tests cover the sites). T12, T13, T18, T20 (as today's behaviour) and T21 were added in the remediation; see `docs/review-remediation-2026-10-29.md`.
8. **Remediation (2026-10-29):** adopted closes are left out of `observed_generation_net` (R-1), and a stale-view rebuild keeps an adopted holding's `current_stop` and `scaled_out` (R-2).

## Tests

| File | Tests |
|---|---|
| `test_rev142_identity.py` | T1, T6, T23, T26, T31 |
| `test_rev142_adoption.py` | T2–T5, T7–T10, T14, T19, T22, T25, T29: the real runtime on the simulator, with the position bought outside TradePulse before the cursor bootstrap |
| `test_rev142_accounting.py` | T15–T17, T18 (projection), T24, T27, T28 (membership), T30, T32 |

## Verification (2026-10-29)

- **pytest:** 1726 passed, 0 failed, 0 skipped. Rev.143 had 1691.
- **mypy:** clean under the project config, and clean on `tradepulse/adoption` checked explicitly. Over the whole package: 117 errors, the same as `main`.
- **Ruff:** the new modules and tests are clean, and the edited files gain only the repo's `Decimal("0")` style finding.

## For the 24 h run

- **Setting:** `TRADEPULSE_ADOPT_UNMANAGED_POSITIONS=true`. It is frozen in the verification context.
- **Order of steps:**
  1. place the 1-share position, with the operator's final yes;
  2. `tradepulse reconcile bootstrap-fill-cursor --reason "..."`;
  3. bind the generation;
  4. start the run.
  The position is then `opening_inventory` and adopted in the first monitor cycle.
