from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional, Protocol, Sequence

from auto_invest.domain.models import Bar


class MarketDataPort(Protocol):
    def list_bars(
        self,
        instrument_ids: Sequence[str],
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> Iterable[Bar]:
        ...
