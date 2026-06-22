from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Mapping


def calculate_metrics(
    equity_curve: Iterable[Mapping[str, Decimal]],
    initial_cash: Decimal,
) -> Mapping[str, Decimal]:
    points = list(equity_curve)
    if not points:
        return {
            "initial_cash": initial_cash,
            "final_equity": initial_cash,
            "total_return": Decimal("0"),
            "max_drawdown": Decimal("0"),
        }

    equities = [point["equity"] for point in points]
    final_equity = equities[-1]
    total_return = final_equity / initial_cash - Decimal("1")

    high_watermark = equities[0]
    max_drawdown = Decimal("0")
    for equity in equities:
        if equity > high_watermark:
            high_watermark = equity
        if high_watermark > 0:
            drawdown = equity / high_watermark - Decimal("1")
            if drawdown < max_drawdown:
                max_drawdown = drawdown

    return {
        "initial_cash": initial_cash,
        "final_equity": final_equity,
        "total_return": total_return,
        "max_drawdown": max_drawdown,
    }
