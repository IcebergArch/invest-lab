from __future__ import annotations

import sys
import unittest
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant_lab.flow_view import build_flow_view


def _market() -> dict[str, object]:
    return {
        "asof": "2026-09-28",
        "industry_asof": "2026-09-24",
        "activity_available": True,
        "industry_breadth": {"up": 4, "down": 27, "total": 31},
        "broad": [
            {"returns": {"1d": -0.01}, "source_id": "eastmoney_kline", "run_id": "broad-1"},
            {"returns": {"1d": -0.02}, "source_id": "eastmoney_kline", "run_id": "broad-1"},
        ],
        "groups": [
            {"name": "消费健康", "share_change_pp": 1.5, "returns": {"1d": -0.01}},
            {"name": "科技信息", "share_change_pp": -1.2, "returns": {"1d": -0.02}},
            {"name": "制造建设", "share_change_pp": 0.8, "returns": {"1d": 0.01}},
            {"name": "金融与公共服务", "share_change_pp": -0.1, "returns": {"1d": 0.005}},
            {"name": "资源能源", "share_change_pp": 0.03, "returns": {"1d": -0.005}},
            {"name": "其他（综合）", "share_change_pp": 5.0, "returns": {"1d": 0.1}},
        ],
        "industries": [
            {"source_id": "sw_research", "run_id": "industry-1"},
            {"source_id": "sw_research", "run_id": "industry-1"},
        ],
    }


class FlowViewTest(unittest.TestCase):
    def test_misaligned_dates_and_turnover_cannot_be_called_inflow(self) -> None:
        view = build_flow_view(_market())
        self.assertEqual("industry_behind", view["date_status"])
        self.assertEqual("2026-09-24", view["industry_asof"])
        self.assertEqual(False, view["net_flow_available"])
        self.assertEqual(3, len(view["reader_lines"]))
        self.assertEqual(["消费健康", "科技信息", "制造建设"],
                         [item["group"] for item in view["focus_points"]])
        self.assertEqual("增加", view["focus_points"][0]["attention"])
        self.assertEqual("下跌", view["focus_points"][0]["group_return_word"])
        self.assertIn("不能用行业变化解释较新一天", view["reader_lines"][2])
        self.assertNotIn("资金净流入", " ".join(view["reader_lines"]))
        self.assertEqual(["industry-1"], view["source_trace"]["industry"]["run_ids"])

    def test_no_amount_falls_back_without_making_up_activity(self) -> None:
        market = _market()
        market["activity_available"] = False
        view = build_flow_view(market)
        self.assertFalse(view["activity_available"])
        self.assertEqual([], view["focus_points"])
        self.assertIn("成交额数据不足", view["reader_lines"][1])
        self.assertFalse(view["net_flow_available"])

    def test_equal_dates_do_not_carry_stale_warning(self) -> None:
        market = deepcopy(_market())
        market["industry_asof"] = market["asof"]
        view = build_flow_view(market)
        self.assertEqual("aligned", view["date_status"])
        self.assertEqual("大盘和行业日期一致。", view["date_line"])


if __name__ == "__main__":
    unittest.main()
