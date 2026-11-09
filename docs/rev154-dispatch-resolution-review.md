# Rev.154 review candidate: ambiguous dispatch safety

Base: uploaded `vegabond-tradepulse(rev.153).zip`, archive revision
`3042abe7b39ae4759b90871b56d723fad7c434cf`.
This is an isolated source-copy candidate. Nothing was merged, pushed, deployed,
or submitted to a broker. The original upload is unchanged.

## Audit findings in Rev.153

1. **Safety defect confirmed.** `_resolve_stranded` turns a successful lookup
   returning `None` into `REJECTED` for `SUBMITTED` and `SUBMISSION_UNKNOWN`.
   Broker absence at one instant does not prove that an in-transit submission
   will never be accepted. The old unknown-order test explicitly required this
   unsafe release of the asset reservation and pending funding.
2. **Wrong grace-period origin confirmed.** Candidate selection and the
   under-reservation recheck use `created_at`. The gateway's dispatch-boundary
   `SUBMITTED` write does not fill `submitted_at`; its successful acceptance
   path previously populated that field later from the broker response.
3. **Approved-order concurrency protection already exists.** The sweep acquires
   and renews the same per-asset execution reservation as the gateway, rereads
   the complete intent, and conditionally commits against the original payload.
   The commit also fences reservation and parent reconciliation lease ownership
   and refuses an integrity hold. These protections are preserved.
4. **Manual clearance needs additional broker evidence.** The port previously
   exposed complete open orders, but no complete all-status history. Open orders
   alone cannot exclude a dispatch that already filled or was cancelled.

## Implemented behavior

- The gateway persists local `submitted_at` with `SUBMITTED` before dispatch and
  preserves it on acceptance. The 120-second sweep grace uses this dispatch time
  for sent orders and creation time for approved, unsent orders.
- A missing sent order transitions to, or remains, `SUBMISSION_UNKNOWN`.
  Its asset and frozen pending funding remain reserved. Each pass looks again.
  A matching broker order is adopted through the existing path.
- An approved, unsent intent can still be rejected under the asset reservation.
  No broker-not-found observation automatically rejects a sent intent.
- Legacy sent records without `submitted_at` remain eligible for safe lookup and
  adoption. They cannot be manually cleared by inventing a dispatch timestamp.
- Blocker guidance explains the unknown state, protective-exit blockage, and
  operator resolution procedure. Rev.123's historical behavior is marked as
  superseded.

## Operator command

Stop the order-capable runtime, retaining its database. With the normal account
and database configuration, run:

```bash
tradepulse resolve-dispatch <trade-intent-id> --reason 'Broker history investigated'
```

For an official generation, also supply its exact binding:

```bash
tradepulse resolve-dispatch <trade-intent-id> --reason 'Broker history investigated' --verification-generation <generation-id>
```

The existing verification authority guards the latter operation and checks its
result. An incorrect or omitted binding on a bound database is refused; existing
freeze/provenance checks are not bypassed.

The command requires:

- An exclusive account runtime lease, verified again inside the commit; it
  refuses while another order-capable process holds that account.
- A renewed asset reservation and an unchanged ambiguous intent without a
  broker order ID or integrity hold.
- A proven local dispatch timestamp at least 15 minutes old, the same broker
  account as risk approval, and a nonempty operator explanation.
- Successful exact client-ID lookups both before and after history inspection,
  each returning not found.
- Complete all-status order history from dispatch, including terminal orders.
  Pagination overlaps timestamp boundaries, checks continuity, and refuses a
  saturated timestamp or malformed population. A matching client ID or an
  unexplained order on the asset refuses clearance.
- Independently paginated complete activity history and no unexplained fill
  on the asset since dispatch. Missing pagination evidence, malformed relevant
  timestamps, lookup errors, or timeouts cannot authorize clearance.

One transaction compares the full original intent and fences the asset lease,
then writes the rejection reason `SUBMISSION_ABSENT_OPERATOR_RESOLVED`, the
broker and operator evidence in a reconciliation record, and a 30-day watch.
Failure to create the watch rolls the rejection back too. This command makes
no broker order submission or cancellation.

## Late appearance

At the beginning of each reconciliation pass, active watches are checked against
their recorded venue/account. A late order creates a durable critical audit event
and `RESOLVED_DISPATCH_APPEARED` order-level integrity hold, latches financial
integrity, and returns degraded before ordinary reconciliation can adopt it.
Notification delivery is retried using the existing alert mechanism.

Detected incidents persist beyond the 30-day lookup window and remain blocked
through restarts; they are not automatically cleared. Investigation and an
explicit accounting repair are required. This revision deliberately adds no
shortcut to clear that incident.

Watch transfer into a successor generation's handover remains Rev.147 work.
This candidate must not be treated as implementing that transfer, or as approval
to discard the current database while a watch is active.

## Review and validation

New coverage includes an in-transit request released only after the sweep, an
unreleased request across repeated passes, dispatch-origin grace, legacy missing
timestamps, safe operator resolution, account/age/history/fill refusals, a final
lookup detecting an intervening acceptance, lease loss, atomic rollback, late
appearance without adoption, restart persistence, expiry, and multi-page
all-status history including timestamp saturation.

The existing pending-funding test now requires unknown dispatch funding to stay
reserved. Port contract tests explicitly include the new history method.

Validation results are recorded in `rev154-validation.txt` alongside this file.
The broker tests use fakes or HTTP mocks; integration tests are not enabled.

Independent review is required before treating this candidate as a release.
