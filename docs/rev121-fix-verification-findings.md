# Rev.121: fix-verification findings

Rev.121 follows Rev.120 (`0d5744f2`). An external verification audit of Rev.120 confirmed three of its six fixes as complete. It found three remaining gaps, each reproduced here and closed with a test that fails on Rev.120. Protected source changed, so both soaks must be run again from Rev.121.

## 1. High: recovery adopted a response without a broker order id

**Finding.** `order_matches_intent` checked client id, symbol, side, type and quantity, but not that the order had an id. A lookup response without `id` parsed to `broker_order_id=""` and was adopted. The intent became ACCEPTED with an empty order id. Stranded recovery only considers RISK_APPROVED/SUBMITTED, and in-flight recovery needs an order id to poll, so both skipped it. The intent then blocked the symbol, including protective exits, with no automatic recovery.

**Change.** `order_matches_intent` returns False for a blank or missing broker order id. Both recovery paths use it, so the intent stays STRANDED_ORDER_IDENTITY_MISMATCH (stranded) or SUBMISSION_UNKNOWN (gateway) for an operator.

**Validation.**
- `test_unknown_submission_never_adopts_an_order_without_a_broker_order_id`.
- The stranded mismatch cases add `""` and `None` order ids.
- Both fail on Rev.120.

## 2. Medium: a failed delivery left no local evidence, and concurrent calls could double-send

**Finding.** Rev.120's `alert_once` wrote the audit event only after Telegram accepted the alert. With configured Telegram down, an unmanaged position, drift or stranded intent had no local critical audit event at all. Separately, two callers could both pass the "not yet recorded" check and both send.

**Change.** `alert_once` now separates evidence from delivery.
- **Evidence:** the critical audit event is written with `create_once` on first sighting, whether or not Telegram is reachable.
- **Delivery:** tracked by a separate `<event_id>:delivered` marker (`alert_delivered`, severity info), written only after Telegram accepts. A failed send is retried on each pass until delivered.
- **Concurrency:** delivery attempts take a per-alert database lease (`alert_delivery:<event_id>`, 60 s), so concurrent callers in one process or across processes on one database send once.
- **Unconfigured alerting:** evidence is still recorded and nothing is sent.
- **Stranded intents:** the call site now calls `alert_once` on every pass, so its delivery is retried too. Drift is still recorded only on first sighting.

**Validation.**
- `test_alert_once.py`: evidence is recorded despite a failed send; delivery is retried, then deduplicated; three concurrent callers produce one send; unconfigured alerting records evidence and sends nothing.
- `test_unmanaged_position_evidence_survives_a_failed_delivery` failed on Rev.120 (no audit event).
- Existing tests now also expect the delivery markers.

## 3. Low: restart-wide 429 totals undercounted

**Finding.** The 429 counter is cumulative within one process and restarts at zero, but the soak summary took the highest sample. Five 429s before a restart and seven after reported seven.

**Change.** Each sample records `rate_limit_process_id`, a random id fixed for the process. The soak summary sums each process's highest count. Samples without an id, written before Rev.121, form one group.

**Validation.** `test_broker_rate_limit_total_spans_runtime_restarts` (5 then 7 totals 12) failed on Rev.120 with 7. The reconcile-tick test asserts the process id is recorded.
