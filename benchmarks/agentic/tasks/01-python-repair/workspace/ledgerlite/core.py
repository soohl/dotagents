from __future__ import annotations

from decimal import Decimal


class LedgerError(ValueError):
    pass


def parse_amount(value: object) -> Decimal:
    # Defect: binary float conversion and default rounding corrupt money.
    return Decimal(float(value)).quantize(Decimal("0.01"))


def summarize(transactions: list[dict]) -> dict[str, dict[str, str]]:
    totals: dict[str, dict[str, Decimal]] = {}
    for transaction in transactions:
        account = transaction["account"]
        bucket = totals.setdefault(
            account, {"debit": Decimal(0), "credit": Decimal(0)}
        )
        # Defect: both kinds are subtracted.
        bucket[transaction["kind"]] -= parse_amount(transaction["amount"])
    return {
        account: {
            "debit": str(values["debit"]),
            "credit": str(values["credit"]),
            "net": str(values["debit"] + values["credit"]),
        }
        for account, values in totals.items()
    }
