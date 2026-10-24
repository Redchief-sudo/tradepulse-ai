# TradePulse operations runbook

Paper-trading runtime. The supervised unit is `deploy/tradepulse-run.service`; it forces paper mode and never re-activates a stopped session.

## 1. Host

Protection (monitor, settlement, reconciliation) runs only while the process runs. Use an always-on, mains-powered host.

Database: the service uses `tradepulse-service.db`, never `tradepulse.db` (the legacy database is FINANCIAL_INTEGRITY_BLOCKED and preserved as evidence). Set it in `.env`, so the service and every operator command (`tradepulse start`, `stop`, `status`, `reconcile`) read the same database:

```
TRADEPULSE_DATABASE_URL=sqlite:///tradepulse-service.db
```

The unit runs `tradepulse run --require-database sqlite:///tradepulse-service.db`, which refuses to start (exit 78, not restarted) unless the database the runtime actually resolved is that file. The check uses the resolved value, so an exported `TRADEPULSE_DATABASE_URL` or an earlier duplicate line in `.env` (the first one wins) cannot silently open another database. Only the soaks' opening checkpoint knows about positions held before a run, so start the service with a flat paper account: any pre-existing broker position is accounting drift to a fresh database and latches FINANCIAL_INTEGRITY_BLOCKED on the first reconciliation.

Install:

```
mkdir -p ~/.config/systemd/user && cp deploy/tradepulse-run.service ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now tradepulse-run && loginctl enable-linger $USER
```

The unit sets `TRADEPULSE_EXECUTION_MODE=paper` and `TRADEPULSE_LIVE_TRADING_ENABLED=false` in the process environment. `.env` cannot override them, because `_load_dotenv` only fills keys that are not already set.

The first activation is always deliberate: run `tradepulse start`, since `--resume` never activates a stopped session. Scanning begins within 30 s of `start` (`SCAN_IDLE_POLL_SECONDS`). No service restart is needed.

Deploy step: after deploying, rebuild the frontend (`cd frontend && npm run build`). A stale prebuilt `frontend/dist` cannot send the `X-TradePulse-Control` header, so the dashboard's start, stop and reset buttons would get 403.

## 1a. Account lease (one runtime per account)

The order-capable commands (`tradepulse run`, `scan` and `monitor`) each take an exclusive lease on their broker account before building anything that can place an order, and hold it until every order-capable task has finished. The gateway re-checks the lease immediately before every order submission; if it is no longer held, the order is rejected as `ACCOUNT_LEASE_NOT_HELD` with a critical alert, and no broker call is made. `settle`, `reconcile`, `status`, `start`, `stop`, `dashboard` and the verification commands place no orders and take no lease.

Mutual exclusion holds among processes on **one host** that resolve the **same lease directory** on a **local filesystem**. It does not cover another host, another user or `TRADEPULSE_LEASE_DIR` (a different domain), or broker API clients that are not TradePulse. Those remain the operator's responsibility (one host per account, design section 3.8), and reconciliation's integrity lock detects them after the fact.

- **Where the lease lives:** `TRADEPULSE_LEASE_DIR` if set, otherwise `$XDG_STATE_HOME/tradepulse/leases`, otherwise `~/.local/state/tradepulse/leases`. The directory is mode 0700. A directory on NFS, CIFS/SMB or SSHFS is refused.
- **The file:** `<venue>-<account number>.lock`, mode 0600. Never delete it: the kernel releases the lock when its process exits or dies, so a crashed runtime never strands the lease, and deleting the file is exactly what would let two runtimes lock the same account.
- **When a command refuses:** it logs `account_lease_held`, naming the holder's command, pid and start time, and exits 1. Stop that process; do not delete the lock file. On success it logs `account_lease_acquired` with the venue, account number, path and holder.

## 1b. Broker conformance kit

`python_tests/broker_conformance/` checks that a broker adapter behaves the way TradePulse needs: account identity, order acceptance and rejection, fills, recovery by client order id, fees captured once, activity ordering and paging, account totals, the trading calendar, and outages (simulator only).

- **Default run** (`.venv/bin/python -m pytest python_tests`): only the simulator variants run. Every `integration` test is deselected, not skipped, and any lookup of an `alpaca.markets` host fails the test (`real broker host blocked in default test run`), so an unmocked broker call can never reach a real account.
- **Read-only against the paper account:** `.venv/bin/python -m pytest -m integration python_tests/broker_conformance -k "alpaca_paper and not test_order_"`. It reads `.env` the way the CLI does and refuses anything but paper with live trading disabled. It places no orders.
- **Order tests on paper** (`test_order_*`: one rejected order and one 1-share SPY round trip) run only with the operator's approval for that run, and only when all of these hold:
  - `-m integration` is given and `TRADEPULSE_CONFORMANCE_ORDERS=1` is set;
  - the configuration is paper with live trading disabled;
  - the kit can take the account lease (holder "conformance kit");
  - no `tradepulse-*` user unit is active and no `tradepulse run`/`scan`/`monitor` or soak process is running;
  - the market is open (otherwise they skip).

  However a test ends, the kit cancels any of its orders still open and checks that each symbol's position is back where it started, all while still holding the lease. Anything it cannot resolve (an order it cannot look up or cancel, a position that did not return) fails the run with a report naming each item. It never sends an offsetting trade, so resolve those by hand.

  The process check fails closed: a process whose command line cannot be read counts as a possible runtime.

  Those trades bypass the execution gateway, so they never reach a TradePulse database. Run them on an account that no TradePulse database is reconciling, or expect that database's reconciliation to flag them.

## 2. Restart semantics

`tradepulse run --resume` continues only an already-ACTIVE or MARKET_CLOSED session. It never activates a MANUALLY_STOPPED, DISABLED, RISK_STOPPED or FINANCIAL_INTEGRITY_BLOCKED one.

After an operator stop, a risk stop or an integrity hold, a restart keeps trading off: every lane starts, the scan lanes idle (no AI call, no market data, no scan record) while monitor, settlement and reconciliation keep running. Clear the condition with the existing reset commands, then run `tradepulse start`; scanning resumes within 30 s, with no restart.

## 2a. When something is blocked

`tradepulse status` (and the dashboard's Blockers panel, `GET /api/blockers`) lists everything currently stopping trading. Each entry shows what it blocks, why, since when, when reconciliation last checked it, and whether it clears automatically or needs you.

| Kind | Clears automatically when | Needs you when |
|---|---|---|
| `session_latch` | never | always: follow the printed command (`reset-integrity`, `reset-risk` or `start`) |
| `integrity_hold` | verification pending: reconciliation re-verifies the order | a fill-quantity dispute is proven |
| `accounting_epoch` | Alpaca's fee activities post and the epoch finalizes | it is `integrity_blocked`, or finalization keeps failing with the reason shown |
| `order_in_flight` | Alpaca reports the order final (reconciliation fetches it every pass) | never by time alone |
| `stranded_intent` | the sweep proves the order by client order id, or Alpaca returns a definitive not-found | the order cannot be proven (account, identity or integrity hold) |

A timeout or a failed broker lookup never clears an uncertain order; only broker evidence does.

## 3. Starting a soak

Stop the service first, because a soak needs exclusive use of the account (the runner now enforces this and refuses with `SUPERVISED_SERVICE_ACTIVE` if the service is still running):

```
systemctl --user stop tradepulse-run
```

Then start the soak:

```
cd ~/tradepulse-ai && D=$PWD/data/soaks/$(date -u +%Y%m%dT%H%M%SZ); mkdir -p "$D" && chmod 700 "$D" && echo "$D" && \
setsid nohup systemd-inhibit --what=sleep:idle:handle-lid-switch:handle-power-key --who=tradepulse-soak --why="accounting soak 1" \
  .venv/bin/python scripts/run_accounting_soak.py --run-number 1 --hours 20 --database "$D/soak-accounting-1.db" \
  --report "$D/soak-accounting-1.json" --port 8766 > "$D/launcher.log" 2>&1 < /dev/null &
```

The runner writes `<report stem>.preflight.json` beside the report (for example `soak-accounting-1.preflight.json`), an immutable record of its checks. Read it. Refusals:

- `SUPERVISED_SERVICE_ACTIVE` (the runner runs `systemctl --user is-active tradepulse-run` and refuses before building a broker if the service is running). `SUPERVISED_SERVICE_UNVERIFIABLE`: the check fails closed, so any answer other than `inactive` or `failed` (an unreachable user bus, a unit still starting or stopping) also refuses. If `systemctl` is missing it proceeds and records `"service_check": "systemctl_unavailable"` in the evidence.
- `OPEN_BROKER_ORDERS`
- `PRE_GENERATION_TRADE_TODAY`
- `FEE_DAY_NOT_CLOSED:<day>`
- `FEE_EVIDENCE_MISSING:<day>:<REG|OCC>`
- `ACKNOWLEDGEMENT_INVALID:<value>`

A refused run must be retried with a new report path; evidence is never overwritten. Use `--acknowledge-fee-day YYYYMMDD` (repeatable) only after checking the broker statement by hand: it waives only a missing fee subtype for a validated past day within the 10-day window. The frozen opening checkpoint must match the preflight's account, otherwise the run fails with `soak_opening_account_mismatch`.

The preflight is a preliminary screen; the generation-membership latch remains the backstop.

Never trade the account, close opening inventory or edit `tradepulse/` during a run. The final reconciliation must land after the ~17:45 PT fee batch.

## 4. Stopping cleanly

For a soak, `kill -INT <runtime pid>` makes the soak runner write its report. For the service: `systemctl --user stop tradepulse-run`.

## 5. Secrets

Rotate the Telegram bot token (earlier logs contained it). Never paste keys into agent sessions.

## 6. Repository hygiene

Unlink the Base44 app from `Redchief-sudo/tradepulse-ai`, fetch before every push, and delete `backup/base44-bot-push-20260928` when it is no longer wanted.
