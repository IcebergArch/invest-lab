from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant_lab.daily_decision_rules_v2 import (build_daily_rule_proposal_from_store,
                                               combine_daily_rule_proposals_from_store,
                                               daily_rule_catalog_v2)
from quant_lab.api_server import QuantAPIService
from quant_lab.daily_factors_v2 import FACTOR_SET_VERSION
from quant_lab.factor_materialization import materialize_stock_factors
from quant_lab.factor_store import FactorObservation, FactorStore
from quant_lab.decision_ensemble import PortfolioBudget
from quant_lab.models import DailyBar, Instrument
from quant_lab.storage import MarketStore

DAY = date(2026, 9, 29)
DECISION = datetime(2026, 9, 29, 8, tzinfo=timezone.utc)


def observation(*, symbol: str = "stock:A", factor_id: str = "intraday_return",
                asof: date = DAY, value: float | None = .01,
                evidence: str = "verified_first_available",
                captured: datetime = DECISION - timedelta(hours=1),
                available: datetime | None = DECISION - timedelta(hours=2),
                input_hash: str = "a" * 64) -> FactorObservation:
    return FactorObservation(
        factor_id, FACTOR_SET_VERSION, {"window": 1}, symbol, asof,
        value, "ok" if value is not None else "missing_input", "return_fraction",
        "forward_adjusted", "fixture", "snapshot-1", input_hash,
        captured, available, evidence,
        "fixture-publication-log" if evidence == "verified_first_available" else None)


class FactorStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)
        self.store = FactorStore(self.path / "factors.sqlite3")
        self.store.initialize()

    def test_append_is_idempotent_and_revised_input_is_a_new_observation(self) -> None:
        first = observation()
        self.assertEqual(self.store.append([first]), self.store.append([first]))
        with self.assertRaisesRegex(ValueError, "conflicts"):
            self.store.append([replace(first, value=.2)])
        revised = replace(first, value=.2, input_sha256="b" * 64)
        self.store.append([revised])
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.store.decision_snapshot(["stock:A"],
                [("intraday_return", FACTOR_SET_VERSION, {"window": 1})],
                asof=DAY, decision_at=DECISION, source_snapshot_id="snapshot-1",
                price_basis="forward_adjusted")

    def test_analysis_tracks_change_but_decision_requires_pit_evidence(self) -> None:
        retrospective = replace(observation(asof=DAY - timedelta(days=1), value=.02),
                                availability_evidence="retrospective", first_available_at=None)
        latest = replace(observation(), availability_evidence="retrospective",
                         first_available_at=None)
        self.store.append([retrospective, latest])
        insight = self.store.analysis_insights("stock:A", asof=DAY)[0]
        self.assertAlmostEqual(-.01, insight["change"])
        self.assertEqual("retrospective", insight["availability_evidence"])
        with self.assertRaisesRegex(ValueError, "point-in-time"):
            self.store.decision_snapshot(["stock:A"],
                [("intraday_return", FACTOR_SET_VERSION, {"window": 1})],
                asof=DAY, decision_at=DECISION, source_snapshot_id="snapshot-1",
                price_basis="forward_adjusted")

    def test_analysis_exposes_latest_missing_input_instead_of_stale_value(self) -> None:
        earlier = replace(observation(asof=DAY - timedelta(days=1)),
                          availability_evidence="retrospective", first_available_at=None,
                          availability_evidence_id=None)
        missing = replace(earlier, asof_date=DAY, value=None, status="missing_input",
                          input_sha256="b" * 64)
        self.store.append([earlier, missing])
        insight = self.store.analysis_insights("stock:A", asof=DAY)[0]
        self.assertEqual("missing_input", insight["status"])
        self.assertIsNone(insight["value"])
        self.assertIsNone(insight["change"])

    def test_future_capture_and_future_availability_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "evidence ID"):
            self.store.append([replace(observation(), availability_evidence_id=None)])
        for field in ("source_captured_at", "first_available_at"):
            value = replace(observation(), **{field: DECISION + timedelta(seconds=1)})
            store = FactorStore(self.path / f"{field}.sqlite3")
            store.initialize()
            store.append([value])
            with self.assertRaisesRegex(ValueError, "point-in-time"):
                store.decision_snapshot(["stock:A"],
                    [("intraday_return", FACTOR_SET_VERSION, {"window": 1})],
                    asof=DAY, decision_at=DECISION,
                    source_snapshot_id="snapshot-1", price_basis="forward_adjusted")

    def test_decision_function_consumes_shared_snapshot(self) -> None:
        self.store.append([observation(symbol="stock:A", value=.01),
                           observation(symbol="stock:B", value=-.01),
                           observation(symbol="stock:A", factor_id="overnight_gap", value=.01),
                           observation(symbol="stock:B", factor_id="overnight_gap", value=-.01)])
        proposal = build_daily_rule_proposal_from_store(
            self.store, "gap_followthrough", ["stock:A", "stock:B"],
            asof=DAY, decision_at=DECISION, source_snapshot_id="snapshot-1",
            price_basis="forward_adjusted")
        self.assertEqual({"stock:A": 1., "stock:B": 0.}, proposal.target_weights)
        self.assertEqual("research_only_unvalidated",
                         daily_rule_catalog_v2()[0]["deployment_status"])
        self.store.append([
            replace(observation(symbol="stock:A", factor_id="rolling_range_volatility",
                                value=.3), parameters={"window": 20},
                    output_unit="per_session_volatility_fraction"),
            replace(observation(symbol="stock:B", factor_id="rolling_range_volatility",
                                value=.1), parameters={"window": 20},
                    output_unit="per_session_volatility_fraction"),
        ])
        combined = combine_daily_rule_proposals_from_store(
            ["gap_followthrough", "low_range_risk"], self.store,
            ["stock:A", "stock:B"],
            {"gap_followthrough": 1, "low_range_risk": 1},
            PortfolioBudget(.8, .35), asof=DAY, decision_at=DECISION,
            source_snapshot_id="snapshot-1", price_basis="forward_adjusted")
        self.assertEqual({"stock:A": .35, "stock:B": .35}, combined["target_weights"])
        self.assertEqual("research_only_unvalidated", combined["deployment_status"])

    def test_market_materialization_is_analysis_only(self) -> None:
        market = MarketStore(self.path / "market.sqlite3")
        market.initialize()
        market.upsert_instruments([Instrument("stock:A", "000001.SZ", "甲", "stock",
                                              "test", "fixture", "000001.SZ",
                                              adjustment="qfq")])
        days = [date(2026, 7, 1) + timedelta(days=i) for i in range(70)]
        market.upsert_bars([DailyBar("stock:A", day, 10 + i / 10, 10.2 + i / 10,
                                     9.8 + i / 10, 10 + i / 10, 1000, 10000,
                                     "lot", "CNY", "qfq", "fixture", f"hash-{i}")
                            for i, day in enumerate(days)])
        result = materialize_stock_factors(market, self.store, "stock:A",
                                           asof=days[-1], sessions=2)
        self.assertEqual(22, result["observation_count"])
        self.assertFalse(result["point_in_time_eligible"])
        summary = self.store.summary()
        self.assertEqual(22, summary["latest_snapshot_observation_count"])
        self.assertEqual(0, summary["latest_snapshot_evidenced_availability_count"])
        insights = self.store.analysis_insights("stock:A", asof=days[-1])
        self.assertEqual(11, len(insights))
        self.assertIn("relative_volume", {item["factor_id"] for item in insights})
        service = QuantAPIService(market, self.path / "reports", journal_dir=self.path / "journal")
        report = service.stock_report({"symbol": "000001.SZ"})
        self.assertEqual("ready", report["status"])
        self.assertEqual(11, len(report["factor_insights"]))
        self.assertTrue(all(item["availability_evidence"] == "retrospective"
                            for item in report["factor_insights"]))
        self.assertEqual(22, service.console_status()["factor_store"]["latest_snapshot_observation_count"])


if __name__ == "__main__":
    unittest.main()
