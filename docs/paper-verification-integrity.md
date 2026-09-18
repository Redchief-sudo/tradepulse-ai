# Paper verification integrity (Rev.101)

This feature identifies a forward paper-verification dataset with one frozen source baseline. It does not change trading algorithms, enable targets, implement partial exits, select risk parameters, enable live trading, or activate learning. The Generation-2 historical calibration freeze remains separate and unchanged.

## Authority and immutable records

The protected set contains every `.py`, `.json`, `.toml`, `.yaml`, `.yml`, `.sql`, `.ini`, and `.cfg` file under `tradepulse/`, plus existing root `pyproject.toml`, `uv.lock`, `requirements.txt`, and `requirements.lock`. New matching files are detected independently of Git tracking. Package symlinks are refused. Paths are relative POSIX paths; SHA-256 hashes actual file bytes. The aggregate hashes the sorted, compact JSON path-to-digest mapping, without timestamps or generation labels. The manifest enumerates every protected file.

Normal logs, SQLite databases/WAL/SHM, journals, reports, telemetry, PID/lock files, Python/test caches, and generated `data/calibration/` evidence are outside that set. No filesystem permission is changed. Static configuration and schema files inside the package are authority, not an appropriate destination for runtime reports.

A separate manifest context binds the resolved database path, non-secret effective settings, configured universe-file contents, and a digest of broker credentials. Secrets themselves are not serialized. Changing risk configuration, model selection, market-data configuration, universes, or broker credentials requires a new generation even when Python source is unchanged. Log verbosity may change. The source fingerprint is distinct from the public provenance/build fingerprint; Git identity is supplemental metadata only.

Artifacts live beside the database in `<database filename>.paper-verification/<generation>/`. The database-level binding prevents accidental use through an ordinary unlocked CLI command. Each generation needs a fresh evidence database; freeze refuses existing trading/equity/reconciliation evidence and refuses overwriting a bound database. Generation labels are qualified by their database binding. Preserve the database and its entire sidecar together.

Manifests, start records, invalidation records, sealed summaries, and sealed evidence are published with atomic no-replace writes. Digest records detect edits; missing/corrupt artifacts fail closed. There is no automatic manifest regeneration, acceptance of new source hashes, or invalidation reset. This is an application-level cryptographic integrity guard, not protection against an administrator who can replace the program and all its trust records. It does not hash the operating system, installed dependencies, Python bytecode, or remote providers. Back up manifests and seals independently for historical audit.

## Operator workflow

Stop existing processes using the chosen database before freezing. Use the same environment, broker credentials, and settings for freeze and run, with execution mode `paper` and live trading disabled. Choose a new database path for the generation; do not reset or reuse the previous generation's database.

```bash
export TRADEPULSE_DATABASE_URL=sqlite:////absolute/path/paper-generation-1.db
export TRADEPULSE_EXECUTION_MODE=paper
export TRADEPULSE_LIVE_TRADING_ENABLED=false

tradepulse verification freeze --generation paper-1
tradepulse verification verify --generation paper-1
tradepulse run --verification-generation paper-1
tradepulse verification status --generation paper-1
```

Rev.100 writes `Fill.fees` and `Fill.slippage` as literal zero; those fields do not establish a cost model. Freeze therefore assumes **no cost rates**. Without an explicit model, net P&L, net expectancy, and net win rate remain unavailable and the completion gate cannot pass. To define a model, supply both `--fee-bps` and `--slippage-bps` to **freeze**, using deliberately chosen numerical rates. They are per-side basis points of actual fill notional (including the canonical options multiplier), applied uniformly to all asset classes as a verification-only overlay. They never change fills, accounting, execution, or the production P&L calculation. Zero is accepted only when explicitly supplied. Rates are frozen in the manifest and cannot be supplied to verify/status or changed during a generation. No rates are recommended or preselected here.

Official startup checks source and frozen context before invoking the runtime. A missing, corrupt, mismatched, invalidated, or sealed generation refuses startup with a nonzero exit. A generation process lease prevents concurrent official processes and prevents an external status command from sealing while supervised work is in flight.

During `run`, an independent verification task checks source and assesses evidence every five seconds after the preceding check completes. Source mismatch records the exact modified/missing/new paths, permanently invalidates the generation, and requests the existing graceful shutdown. In-flight work drains; it does not become certified evidence for an invalid generation. A transient change made and restored entirely between checks is outside this polling mechanism's guarantee. Verification errors also request shutdown rather than silently downgrading to ordinary paper mode.

`run` does not schedule reconciliation. Stop it cleanly, run the existing one-shot reconciliation with the same generation, then restart:

```bash
tradepulse reconcile --verification-generation paper-1
tradepulse run --verification-generation paper-1
```

The one-shot command verifies before and after its existing work; it adds no scheduling loop. Ordinary scan/monitor/settle/reconcile/start/reset/dashboard CLI invocations are refused for a bound database; official `run` retains its existing local dashboard and controls. `stop` and ordinary session `status` remain available. Use `verification status` for generation integrity/completion, not session `status`.

A normal restart reuses the immutable start timestamp and manifest. It never creates a new generation. Elapsed duration is calendar time from the first successful verification-guard startup, including downtime, not a claim of uninterrupted operation.

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
