# Rev.124: verification watcher load

Rev.124 follows Rev.123 (`cacea286`). Found while soak 1 was resumed to manage its inherited positions. Protected source changed, so soak 2 runs from Rev.124. Soak 1's generation is frozen at Rev.123, so it keeps running on that code until it is stopped.

## Finding

After about 27 hours, the resumed soak-1 runtime logged 26 `trading_supervisor_lane_failed` errors, all `DatabaseError: database is locked`: 15 on the monitor lane, 8 on settle, and 1 each on equity, crypto and option. Every lane recovered and reconciliation stayed `ok`, but the runtime averaged 89% CPU with 1 GB resident.

The cause is `Verification.watch`. It ran the full `report()` every 5 seconds. Each run snapshots the whole database and runs `assess` over it. `assess` computes the generation equity of every equity snapshot, and `observed_generation_equity` scanned every reconciliation record for `generation_adjustment:` ids on each call. That is quadratic in run length: on the soak's 78 MB database (2,719 snapshots, 33,005 records) one assessment was about 180 million string comparisons, taking 52–62 s of CPU. The watcher therefore never idled. Its thread held the GIL, so lane threads that held SQLite's write lock were starved and kept the lock longer, and other lanes exceeded the 10 s busy timeout. A 60-day run would be far worse.

Reconciliation was not the cause. Profiling the full `_run_reconcile` command on a copy of the database gave 3.7 s per pass, with its longest write transaction at 0.32 s.

## Change

- `generation_adjustment_records(rows)` finds the adjustment records once per assessment. `observed_generation_equity` takes them as an optional argument, so `assess` is linear in run length.
- `Verification.watch` checks integrity every `WATCH_CHECK_SECONDS` (5 s): protected-source hashes, generation binding, configuration and thresholds. It runs the full `report()` once at start and then every `WATCH_REPORT_SECONDS` (300 s). A source change is still caught within one 5 s tick.

## Validation

- **Same data, before and after:** on a copy of the live soak-1 database, `assess` takes 1.92 s instead of 61.89 s. The canonical assessment results are byte-identical.
- **New test:** `test_watch_checks_integrity_every_tick_but_assesses_only_on_its_interval` checks that the report runs once while integrity is checked every tick, and that a protected-file change mid-run stops the watcher.
- **Existing test:** `test_runtime_source_failure_requests_graceful_shutdown` still passes.
- **Full suite:** passes.
