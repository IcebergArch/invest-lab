from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant_lab.chronos_adapter import ChronosBoltTinyProvider
from quant_lab.forecast import EvaluationConfig, evaluate_forecasts


def quantile_tensor(horizon: int) -> list[list[list[float]]]:
    return [[[float(10 * (q + 1) + step) for step in range(horizon)] for q in range(9)]]


class ChronosBoltAdapterTest(unittest.TestCase):
    def test_injected_runner_receives_capped_history_and_maps_native_quantiles(self) -> None:
        seen = []

        def runner(context, horizon):
            seen.append((context, horizon))
            return quantile_tensor(horizon)

        provider = ChronosBoltTinyProvider(max_context=3, runner=runner)
        output = provider.forecast([1.0, 2.0, 3.0, 4.0], 2)
        self.assertEqual([((2.0, 3.0, 4.0), 2)], seen)
        self.assertEqual((50.0, 51.0), output.values)
        self.assertEqual((10.0, 11.0), output.p10)
        self.assertEqual((90.0, 91.0), output.p90)
        self.assertIsNone(provider.model_revision)

    def test_live_load_pins_weights_uses_cpu_float32_and_reuses_model(self) -> None:
        revision = "a" * 40
        fake_model = SimpleNamespace(predict=Mock(return_value=quantile_tensor(2)))
        from_pretrained = Mock(return_value=fake_model)
        fake_torch = SimpleNamespace(float32=object(), tensor=Mock(return_value="tensor"))
        fake_chronos = SimpleNamespace(BaseChronosPipeline=SimpleNamespace(
            from_pretrained=from_pretrained
        ))

        def fake_import(name):
            return {"torch": fake_torch, "chronos": fake_chronos}[name]

        with patch("quant_lab.chronos_adapter.import_module", side_effect=fake_import):
            provider = ChronosBoltTinyProvider(model_revision=revision, max_context=2)
            provider.forecast([20.0, 21.0, 22.0], 2)
            provider.forecast([30.0, 31.0, 32.0], 2)
        from_pretrained.assert_called_once_with(
            "amazon/chronos-bolt-tiny", revision=revision,
            device_map="cpu", torch_dtype=fake_torch.float32,
        )
        self.assertEqual(2, fake_torch.tensor.call_count)
        fake_torch.tensor.assert_any_call((21.0, 22.0), dtype=fake_torch.float32)
        self.assertEqual(2, fake_model.predict.call_count)
        fake_model.predict.assert_any_call("tensor", prediction_length=2)

    def test_live_load_requires_full_revision_before_importing_optional_package(self) -> None:
        with patch("quant_lab.chronos_adapter.import_module") as importing:
            provider = ChronosBoltTinyProvider(model_revision="main")
            dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(5)]
            report = evaluate_forecasts(
                dates, [100.0] * len(dates), provider, "stock:TEST",
                config=EvaluationConfig(horizons=(1,), min_context=2),
            )
        self.assertEqual("missing_model_revision", report["status"])
        self.assertEqual([], report["records"])
        importing.assert_not_called()

    def test_missing_optional_dependency_is_reported_without_partial_records(self) -> None:
        error = ModuleNotFoundError("No module named 'chronos'", name="chronos")
        with patch("quant_lab.chronos_adapter.import_module", side_effect=error):
            provider = ChronosBoltTinyProvider(model_revision="b" * 40)
            dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(5)]
            report = evaluate_forecasts(
                dates, [100.0] * len(dates), provider, "stock:TEST",
                config=EvaluationConfig(horizons=(1,), min_context=2),
            )
        self.assertEqual("missing_optional_dependency", report["status"])
        self.assertEqual([], report["records"])
        self.assertIn("chronos", report["reason"])

    def test_rejects_wrong_shape_or_invalid_quantile_order(self) -> None:
        wrong_shape = ChronosBoltTinyProvider(runner=lambda context, horizon: [[[10.0] * horizon]])
        with self.assertRaisesRegex(ValueError, "unexpected forecast shape"):
            wrong_shape.forecast([10.0], 2)
        bad_order = quantile_tensor(2)
        bad_order[0][0] = [80.0, 81.0]
        bad_order[0][8] = [20.0, 21.0]
        provider = ChronosBoltTinyProvider(runner=lambda context, horizon: bad_order)
        with self.assertRaisesRegex(ValueError, "quantiles must be ordered"):
            provider.forecast([10.0], 2)

    def test_config_and_request_bounds(self) -> None:
        with self.assertRaisesRegex(ValueError, "max_context"):
            ChronosBoltTinyProvider(max_context=2049)
        provider = ChronosBoltTinyProvider(runner=lambda context, horizon: quantile_tensor(horizon))
        with self.assertRaisesRegex(ValueError, "native 64-step"):
            provider.forecast([10.0], 65)
        with self.assertRaisesRegex(ValueError, "finite positive prices"):
            provider.forecast([0.0], 1)


if __name__ == "__main__":
    unittest.main()
