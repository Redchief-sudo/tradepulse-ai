# Review batch: Rev.145, 140, 141, 144, 143, 142 (2026-10-29)

These six Revs were built on stacked branches, in this order, and are reviewed together. They are merged to `main` only after the review.

Branch tip `rev142`; base `main` at `c10bb2fa`.

| Rev | What | Record | Branch |
|---|---|---|---|
| 145 | Fill race: defer fills newer than the cycle's population; the census `after` is inclusive | `docs/rev139-phase-a0.md` (A0 outcome) | `rev145-fill-race` |
| 140 | Unsupported short inventory: a conservative sellable bound, the fill cursor, quarantine, resolve, and legacy adoption | `docs/rev140-unsupported-short-inventory.md` | `rev140` |
| 141 | Every risk profile sets the Phase 1 entry limits; pending entries count | its commit message | `rev141-risk-profiles` |
| 144 | A shared market calendar, cross-checked daily against the broker; entries need a verified date and an agreeing clock | `docs/rev144-market-calendar.md` | `rev144` |
| 143 | Dividend cash recorded once; verified attribution, with `D_excl` and `D_unresolved` | `docs/rev143-dividend-recognition.md` | `rev143` |
| 142 | Adoption of unmanaged long positions for protective exits | `docs/rev142-position-adoption.md` | `rev142` |

## Verification at the tip

- **pytest:** 1726 passed, 0 failed, 0 skipped (`main`: 1359).
- **mypy:** clean under the project config. Over the whole package: 117 errors, the same as `main`.
- **Ruff:** no new findings in new modules.
- **Rev.140 replay:** all seven preserved soak databases and the service database replay with no issues and no changes.

## Items that most need the reviewer's decision

1. **Rev.140 A1:** the local sellable bound also subtracts external open orders. A seeded test found the counterexample.
2. **Rev.142, a cross-spec finding:** an adopted *unexpected external* position cannot be sold until its unresolved broker execution is resolved. Opening inventory is unaffected.
3. **Rev.143:** `DIVIDEND_REPRESENTATION` is `unknown`, because the paper account has no dividend history. Every dividend's cash is recorded, but its attribution is refused, so a dividend during the run leaves the evidence incomplete, with a named reason.
4. **Rev.144:** the gateway runs the daily calendar check itself when today has none. Four pre-Rev.144 test files opt in to an always-open test calendar.
5. **Rev.142 / Rev.143 identity choices:** adopted-lot identity and strategy dividend income are derived, not stored, so that existing payloads stay byte-identical.

## Evidence (outside git)

- `~/tradepulse-evidence/2026-10-28-rev140-gate3-measurements/`
- `~/tradepulse-evidence/2026-10-29-rev140-replay/`
- `~/tradepulse-evidence/2026-10-29-rev144-g0/`
- `~/tradepulse-evidence/2026-10-29-rev143-g0/`
