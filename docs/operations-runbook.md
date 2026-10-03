# TradePulse operations runbook

Paper-trading runtime. The supervised unit is `deploy/tradepulse-run.service`; it forces paper mode and never re-activates a stopped session.

## 1. Host

Protection (monitor, settlement, reconciliation) runs only while the process runs. Use an always-on, mains-powered host.

Install:

```
mkdir -p ~/.config/systemd/user && cp deploy/tradepulse-run.service ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now tradepulse-run && loginctl enable-linger $USER
```

The unit sets `TRADEPULSE_EXECUTION_MODE=paper` and `TRADEPULSE_LIVE_TRADING_ENABLED=false` in the process environment. `.env` cannot override them, because `_load_dotenv` only fills keys that are not already set.

The first activation is always deliberate: run `tradepulse start`, since `--resume` never activates a stopped session. Scanning begins within 30 s of `start` (`SCAN_IDLE_POLL_SECONDS`). No service restart is needed.

Deploy step: after deploying, rebuild the frontend (`cd frontend && npm run build`). A stale prebuilt `frontend/dist` cannot send the `X-TradePulse-Control` header, so the dashboard's start, stop and reset buttons would get 403.

## 2. Restart semantics

`tradepulse run --resume` continues only an already-ACTIVE or MARKET_CLOSED session. It never activates a MANUALLY_STOPPED, DISABLED, RISK_STOPPED or FINANCIAL_INTEGRITY_BLOCKED one.

After an operator stop, a risk stop or an integrity hold, a restart keeps trading off: every lane starts, the scan lanes idle (no AI call, no market data, no scan record) while monitor, settlement and reconciliation keep running. Clear the condition with the existing reset commands, then run `tradepulse start`; scanning resumes within 30 s, with no restart.

## 3. Starting a soak

Stop the service first, because a soak needs exclusive use of the account:

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
