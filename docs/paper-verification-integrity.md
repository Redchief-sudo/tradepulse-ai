# Paper verification integrity (Rev.101)

This feature identifies a forward paper-verification dataset with one frozen source baseline. It does not change trading algorithms, enable targets, implement partial exits, select risk parameters, enable live trading, or activate learning. The Generation-2 historical calibration freeze remains separate and unchanged.

## Authority and immutable records

The protected set contains every `.py`, `.json`, `.toml`, `.yaml`, `.yml`, `.sql`, `.ini`, and `.cfg` file under `tradepulse/`, plus existing root `pyproject.toml`, `uv.lock`, `requirements.txt`, and `requirements.lock`. New matching files are detected independently of Git tracking. Package symlinks are refused. Paths are relative POSIX paths; SHA-256 hashes actual file bytes. The aggregate hashes the sorted, compact JSON path-to-digest mapping, without timestamps or generation labels. The manifest enumerates every protected file.

Normal logs, SQLite databases/WAL/SHM, journals, reports, telemetry, PID/lock files, Python/test caches, and generated `data/calibration/` evidence are outside that set. No filesystem permission is changed. Static configuration and schema files inside the package are authority, not an appropriate destination for runtime reports.

A separate manifest context binds the resolved database path, non-secret effective settings, configured universe-file contents, and a digest of broker credentials. Secrets themselves are not serialized. Changing risk configuration, model selection, market-data configuration, universes, or broker credentials requires a new generation even when Python source is unchanged. Log verbosity may change. The source fingerprint is distinct from the public provenance/build fingerprint; Git identity is supplemental metadata only.

Artifacts live beside the database in `<database filename>.paper-verification/<generation>/`. The database-level binding prevents accidental use through an ordinary unlocked CLI command. Each generation needs a fresh evidence database; freeze refuses existing trading/equity/reconciliation evidence and refuses overwriting a bound database. Generation labels are qualified by their database binding. Preserve the database and its entire sidecar together.

Manifests, start records, invalidation records, sealed summaries, and sealed evidence are published with atomic no-replace writes. Digest records detect edits; missing/corrupt artifacts fail closed. There is no automatic manifest regeneration, acceptance of new source hashes, or invalidation reset. This is an application-level cryptographic integrity guard, not protection against an administrator who can replace the program and all its trust records. It does not hash the operating system, installed dependencies, Python bytecode, or remote providers. Back up manifests and seals independently for historical audit.

## Operator workflow

Finish and validate source changes before freezing. Use the same source, environment, broker account, credentials, and settings for both disposable soaks and the later official generation, with execution mode `paper` and live trading disabled. Each run needs a distinct, never-used database and generation name. Preserve the legacy database and any legacy evidence seal; neither reusing that database nor clearing its integrity latch is a prerequisite for starting a new soak.

Arrange exclusive use of the broker account before starting. Database and generation leases do not coordinate separate databases using one account. Confirm that no other runtime or operator will trade or monitor exits on that account and that no legacy broker orders remain pending. Do not automatically cancel orders or close positions to meet this prerequisite. A fresh database can checkpoint existing broker inventory as an excluded opening balance, but it does not acquire the old database's protective-management history. Mixed old and new inventory in the same symbol also needs review: the position monitor currently requests the entire broker position quantity on exit, while generation reconciliation preserves the opening quantity. Do not treat this combination as proven safe merely because freeze accepts the opening checkpoint.

Run the disposable soaks sequentially from the validated checkout, choosing unused paths and an available local dashboard port:

```bash
.venv/bin/python scripts/run_accounting_soak.py \
  --run-number 1 \
  --database /absolute/path/soak-accounting-1.db \
  --report /absolute/path/soak-accounting-1.json \
  --port 8766

.venv/bin/python scripts/run_accounting_soak.py \
  --run-number 2 \
  --database /absolute/path/soak-accounting-2.db \
  --report /absolute/path/soak-accounting-2.json \
  --port 8766
```

The runner sets paper mode and disables live trading. Run 1 requires at least 12 uninterrupted hours after guarded startup, followed by graceful shutdown, reconciliation, and at least 30 minutes of restart operation. Run 2 requires at least 24 uninterrupted hours plus the same restart exercise; it must also capture a complete equity session from before opening through closing using persisted broker-clock receipts. `--hours` and `--restart-minutes` can extend these minimums. Startup and shutdown add time beyond the 12.5/24.5-hour minimum runtime. The runner refuses existing databases, sidecars, and report paths rather than cleaning or reusing them.

Elapsed time alone cannot pass a soak. Reports require continuous evidence from all six lanes, real completed round trips and reconciled epochs for equities, crypto, and options, conserved fee receipts, an exercised late-fee reopening, complete slippage evidence within the overlay, and clean accounting/reconciliation evidence. No synthetic trades or relaxed gates substitute for missing coverage. Preserve an incomplete or failed run; any replacement run needs new paths.

After shutdown, the runner preserves a private, self-contained SQLite backup and its verification sidecar beside the report. The report binds the database, manifest, opening checkpoint, artifacts, source, configuration, and broker account with hashes. Official freeze independently reanalyzes two passing preserved reports with distinct database identities and generations, run numbers 1 and 2, and the same source/configuration/account. A protected source or configuration change requires both soaks to be repeated. Keep the reports, preserved databases, and sidecars together.

Only after both reports pass, freeze a third fresh database for the official generation:

```bash
export TRADEPULSE_DATABASE_URL=sqlite:////absolute/path/paper-generation-1.db
export TRADEPULSE_EXECUTION_MODE=paper
export TRADEPULSE_LIVE_TRADING_ENABLED=false

tradepulse verification freeze --generation paper-1 \
  --fee-bps 25 --slippage-bps 15 \
  --soak-report /absolute/path/soak-accounting-1.json \
  --soak-report /absolute/path/soak-accounting-2.json
tradepulse verification verify --generation paper-1
tradepulse run --verification-generation paper-1
tradepulse verification status --generation paper-1
```

Freeze requires exactly **25 fee basis points and 15 slippage basis points per side**; the soak runner supplies them automatically. These rates apply to actual fill notional, including the canonical options multiplier, as a verification-only model. They do not change fills, accounting, execution, or observed broker fees. Modeled trade net and receipt-backed observed generation net remain separate authorities. Rates are frozen in the manifest and cannot be supplied to verify/status or changed during a generation; missing, zero, or different rates are refused.

Official startup checks source and frozen context before invoking the runtime. A missing, corrupt, mismatched, invalidated, or sealed generation refuses startup with a nonzero exit. A generation process lease prevents concurrent official processes and prevents an external status command from sealing while supervised work is in flight.

During `run`, an independent verification task checks source and assesses evidence every five seconds after the preceding check completes. Source mismatch records the exact modified/missing/new paths, permanently invalidates the generation, and requests the existing graceful shutdown. In-flight work drains; it does not become certified evidence for an invalid generation. A transient change made and restored entirely between checks is outside this polling mechanism's guarantee. Verification errors also request shutdown rather than silently downgrading to ordinary paper mode.

With `--verification-generation`, `run` performs startup reconciliation and adds an independent reconciliation lane with a 60-second cadence alongside equity, crypto, options, monitor, and settlement lanes. It also settles and reconciles after trading work drains at shutdown. For an additional standalone reconciliation, stop the runtime cleanly to release its generation lease, use the same generation, then restart:

```bash
tradepulse reconcile --verification-generation paper-1
tradepulse run --verification-generation paper-1
```

The one-shot command verifies before and after its existing work; it adds no scheduling loop. Ordinary scan/monitor/settle/reconcile/start/reset/dashboard CLI invocations are refused for a bound database; official `run` retains its existing local dashboard and controls. `stop` and ordinary session `status` remain available. Use `verification status` for generation integrity/completion, not session `status`.

A normal restart reuses the immutable start timestamp and manifest. It never creates a new generation. The official 60-day duration is calendar time from the first successful guarded startup, including downtime. Soak duration and restart coverage are separately checked against paired runtime start/stop receipts and lane continuity; downtime cannot satisfy the uninterrupted soak minimum.

## Completion population and gates

Assessment reads a single SQLite transaction using existing persisted models. One eligible completed round trip is one fully filled opening TradeIntent whose complete originating lot population is closed. All contributing opening/closing settlements must be completed and integrity-verified, with all projection flags complete. Closure quantities must conserve the lot and fill quantities; every required `(lot, closing fill)` attribution must exist exactly once and agree with the corresponding quantity, identity, price, and lot P&L. An opening order split across multiple fills/lots counts once after all are closed. Open lots, partial closures, and incomplete opening orders are not completed samples.

Win rate is strictly positive **net** outcomes divided by all eligible completed opening intents, including breakevens and losses. Net expectancy uses the same denominator. Gross realized P&L is read from attribution and checked against lot totals; the separately frozen cost overlay produces verification net results. Missing costs or required equity/reconciliation evidence remain unavailable, never fabricated as favorable zeroes.

The centralized immutable verification policy requires all of:

| Criterion | Requirement |
|---|---:|
| Calendar duration | >= 60 days |
| Eligible completed round trips | >= 200 |
| Net win rate | >= 55.0% |
| Maximum drawdown | <= 10.0% |
| Net realized P&L | > 0 |
| Net expectancy per eligible trade | > 0 |
| Unresolved reconciliation discrepancies | 0 |
| Incomplete/failed/unverified settlements | 0 |
| Active financial-integrity holds/incidents | 0 |
| Missing/duplicate required attribution | 0 |
| Other detected evidence inconsistencies | 0 |

Drawdown walks the entire persisted broker equity-snapshot series chronologically, preserving the running high-water mark: `(peak - equity) / peak * 100`. It never resets at restart or selects only profitable trades. The series must span the fills and have positive initial equity. This is maximum **observed snapshot** drawdown, not a claim about unsampled intrabar equity or an independently cash-flow-adjusted curve.

Reconciliation uses the most recent persisted result per subject (position/view/accounting share a position subject); insertion order resolves equal-timestamp records. Existing holds and financial-integrity session flags block passing. A latched or forced-reset financial-integrity audit event remains unresolved until a subsequent verified reset. No broker request or reconciliation calculation is added by the assessor.

Each assessment reports actual values, operators, required thresholds, and individual pass flags. The overall state is `PROVE_EDGE_IN_PROGRESS` while duration/sample are insufficient, `PROVE_EDGE_THRESHOLD_NOT_MET` when other thresholds are unmet, or `PROVE_EDGE_FAILED_INTEGRITY` when source/evidence integrity fails. No favorable performance statistic overrides an integrity failure.

## Sealing and later development

When all criteria pass during an official run, the guard requests graceful shutdown, waits for existing work to finish, then reassesses the final persisted population. Only a still-passing assessment seals the generation. An idle `verification status` can also assess and seal while holding the exclusive generation lease. Interrupted/incomplete sealing fails closed rather than overwriting an artifact.

The seal preserves the complete assessed evidence snapshot, evidence SHA-256, manifest digest, source fingerprint, population, criteria, and summary. Later writes to the operational database do not change this sealed dataset. Seal/evidence corruption is detected on inspection. Future source differences are reported separately from the historical sealed result; a sealed generation cannot restart as official verification.

`PROVE_EDGE_PASSED` sets workflow eligibility to `POST_PROVE_EDGE_DEVELOPMENT`. It edits no source, changes no permissions, and activates no multi-broker, persistent-memory, or adaptive-learning behavior. Those remain future development requiring tests, audit, a new revision, and a new frozen generation/database. For a necessary correction before passing, stop the old run, preserve its artifacts, make and test the correction, and freeze the new generation. An observed source mismatch permanently disqualifies continuation of the old generation even if its files are restored.
