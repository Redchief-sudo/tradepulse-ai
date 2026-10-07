# Rev.122: recovery identity checks

Rev.122 follows Rev.121 (`c9ddfd42`). An external verification audit of Rev.121 confirmed its alert-evidence, concurrent-delivery and restart-counter fixes. It reproduced two remaining recovery gaps, both fixed here. A third gap, from the same root cause, was found while fixing the first. Each has a test that fails on Rev.121. Protected source changed, so both soaks must be run again from Rev.122.

## 1. High: non-string order ids were adopted

**Finding.** `_parse_order_data` built `broker_order_id` with `str(data.get("id", ""))`. So `id: null` became `"None"`, and numbers, booleans and objects became their string forms. Rev.121's blank check passed all of them, so recovery could adopt an order identity Alpaca never issued, poll a non-existent order and leave the symbol blocked.

**Change.**
- The parser accepts only a string `id` (stripped); anything else parses as `""`, meaning no id.
- `order_matches_intent` also requires the raw `id` to be a non-blank string equal to the parsed id, so a hand-built response cannot bypass the parser.

**Validation.**
- `test_order_id_that_is_not_a_string_parses_as_no_id` covers null, number, bool, object, list and whitespace.
- The stranded mismatch cases add null, numeric, boolean, object and different ids.
- Gateway: `test_unknown_submission_never_adopts_a_null_broker_order_id` and `..._a_numeric_broker_order_id`.

## 2. Medium: wrong time in force was adopted

**Finding.** The matcher compared order type and quantity but not time in force. A crypto order with `time_in_force="day"` was adopted, although the gateway always submits crypto as `gtc` and everything else as `day` (`default_time_in_force`).

**Change.** `order_matches_intent` requires the order's `time_in_force` to equal `default_time_in_force` for the intent's asset class. A missing value never matches.

**Validation.**
- The stranded cases add a crypto `day` order and a missing time in force.
- Gateway: `test_unknown_submission_never_adopts_an_order_with_another_time_in_force` (an equity `gtc` order).

## 3. High: a placement response without an id was accepted

**Finding.** The same parser feeds `place_order`. A successful placement response with an unusable `id` made the intent ACCEPTED with an empty (on Rev.121, `"None"`) order id. The gateway then polled `/v2/orders/` with no id, and neither recovery sweep would revisit the intent.

**Change.** A placement response without an order id goes through `_recover_unknown_submission`, the same client-order-id lookup used for an ambiguous submission. It adopts the real order only if `order_matches_intent` holds, otherwise leaves the intent SUBMISSION_UNKNOWN for an operator, and never resubmits.

**Validation.** `test_placement_response_without_an_order_id_recovers_by_client_order_id`: a 200 with `id: null` is recovered to the real `order-1` and fills, with one submission and one lookup. On Rev.121 it polled `/v2/orders/`.

**Test fixtures.** The recovery fixtures now carry `id` and `time_in_force` as Alpaca returns them.
