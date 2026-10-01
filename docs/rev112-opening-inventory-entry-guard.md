# Rev.112: never reopen an opening-inventory instrument

Rev.112 follows Rev.111 (`26fa31ee`).

A bound verification generation can start while the broker account already holds positions. The opening checkpoint records them and generation reconciliation excludes their quantity and performance. The exit monitor, however, sizes an exit from the whole broker position (`requested_quantity=abs(position.qty)`). If the strategy opened a new lot in one of those exact instruments, the lot's exit would also sell the excluded opening quantity. Sells would then exceed the generation's lots and break conservation, latching the financial-integrity block. This is the "mixed opening and new inventory in one symbol" case that `paper-verification-integrity.md` warns is unproven.

The scanner now rejects any candidate whose resolved trade instrument (the stock, the coin, or the selected option contract) is held in the bound generation's opening checkpoint. The rejection is `OPENING_INVENTORY_INSTRUMENT` and is persisted like every other rejection. The check is instrument-exact: holding a stock at opening does not block options on it. Unbound operation is unchanged.

Opening inventory is still neither monitored nor exited by the generation; the monitor ignores broker positions without a local holding. Do not trade an opening-inventory position by hand during a generation: its disappearance invalidates the opening-inventory mark in generation equity.

No thresholds, fee/slippage overlay rates, risk parameters or other trading logic changed. Validation: full Python suite passed. The new scanner tests are an end-to-end options cycle refused for an opening-inventory contract with no order sent, and a control in which holding only the underlying stock still trades.
