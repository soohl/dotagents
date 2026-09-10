# Repair LedgerLite

Repair this compact dependency-free Python package and run its full tests.

Requirements:

- `parse_amount` trims input, rounds decimal values to cents using
  `ROUND_HALF_UP`, and rejects malformed, non-finite, boolean, or negative
  values with `ValueError`.
- `summarize` returns accounts sorted lexicographically. For each account,
  debit, credit, and net are two-decimal strings. Credits increase net and
  debits decrease it.
- `python -m ledgerlite INPUT.json` reads a JSON array of transaction objects,
  prints deterministic JSON, and exits 2 with a concise no-traceback error for
  malformed JSON, invalid transactions, missing files, or wrong usage.
- Each transaction needs a non-empty string `account`, kind `debit` or
  `credit`, and a valid amount. Unknown fields may be ignored.

Keep the public API intact, add no dependencies, and add useful tests.
