# Rev.114: repository consistency pass

Rev.114 follows Rev.113 (`65c0a416`). It contains no behaviour change. The full Python suite passed.

- **Lint, semantics-preserving only.** Ruff fixes for unused imports, a redefined test import, import order, deprecated `typing` imports, redundant `return None`, and the `__all__` order, across 87 files.
  - Explanatory `# noqa: BLE001 - <reason>` comments on deliberate broad exception handlers were kept, even where ruff considered the `noqa` unnecessary. They document why each catch is deliberate.
  - The `Decimal("2")` house style (about 1,300 sites) is intentional and unchanged.
- **Two clarifications in accounting code, with identical behaviour.**
  - `fee_population` validates a cash-only row's `net_amount` without keeping the value, which is now explicit.
  - `finalize_population`'s fee-receipt closure binds `raw_by_id` explicitly instead of capturing the loop variable. It was always called within the same iteration.
- **Removed `AlpacaClient.close_position`.** It had no callers and was a broker-side position close outside the execution gateway, which `AGENTS.md` forbids. It is the operation that sold an AAPL position out of band on 2026-09-26.
- **Docs aligned with code.** `AGENTS.md` no longer names the removed method. `paper-verification-integrity.md` now describes mixed opening/new inventory as prevented (Rev.112), not just warned about.
