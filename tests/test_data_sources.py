from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant_lab.data_sources import TencentDailySource
from quant_lab.models import Instrument


class FakeHttp:
    def __init__(self, response: dict):
        self.response = response
        self.params: dict | None = None
        self.endpoint: str | None = None

    def get(self, endpoint: str, params: dict):
        self.endpoint = endpoint
        self.params = params
        return self.response, "payload-hash"


def instrument(adjustment: str) -> Instrument:
    return Instrument("stock:000338.SZ", "000338.SZ", "潍柴动力", "stock",
                      "focus_stock", "eastmoney_kline", "0.000338", "SZSE", adjustment)


def response(**series: list) -> dict:
    return {"code": 0, "data": {"sz000338": series}}


RAW_ROW = ["2026-09-28", "10.0", "11.0", "12.0", "9.0", "100"]
ADJUSTED_ROW = ["2026-09-28", "9.0", "10.0", "11.0", "8.0", "100"]
DAY = date(2026, 9, 28)


class TencentPriceBasisTest(unittest.TestCase):
    def test_qfq_request_uses_only_qfqday(self) -> None:
        http = FakeHttp(response(qfqday=[ADJUSTED_ROW], day=[RAW_ROW]))
        bars = TencentDailySource(http).fetch(instrument("qfq"), DAY, DAY)
        self.assertEqual("sz000338,day,2026-09-28,2026-09-28,640,qfq",
                         http.params["param"])
        self.assertEqual(TencentDailySource.endpoint, http.endpoint)
        self.assertEqual(10.0, bars[0].close)
        self.assertEqual("qfq", bars[0].adjustment)

    def test_qfq_request_rejects_raw_only_response(self) -> None:
        http = FakeHttp(response(day=[RAW_ROW]))
        with self.assertRaisesRegex(ValueError, "missing qfqday"):
            TencentDailySource(http).fetch(instrument("qfq"), DAY, DAY)

    def test_qfq_request_rejects_empty_adjusted_series_when_raw_has_rows(self) -> None:
        http = FakeHttp(response(qfqday=[], day=[RAW_ROW]))
        with self.assertRaisesRegex(ValueError, "empty qfqday"):
            TencentDailySource(http).fetch(instrument("qfq"), DAY, DAY)

    def test_unadjusted_request_keeps_raw_day_behavior(self) -> None:
        http = FakeHttp(response(day=[RAW_ROW], qfqday=[ADJUSTED_ROW]))
        bars = TencentDailySource(http).fetch(instrument("none"), DAY, DAY)
        self.assertEqual("sz000338,day,2026-09-28,2026-09-28,640",
                         http.params["param"])
        self.assertEqual(TencentDailySource.raw_endpoint, http.endpoint)
        self.assertEqual(11.0, bars[0].close)
        self.assertEqual("none", bars[0].adjustment)


if __name__ == "__main__":
    unittest.main()
