from __future__ import annotations

import gzip
import hashlib
import json
import math
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from quant_lab.group_behavior_pilot import (
    PILOT_VERSION, PilotConfig, archive_group_behavior_record, build_group_daily,
    evaluate_group_behavior, load_group_bars, read_group_behavior_record,
    run_qlib_group_behavior_pilot,
)
from quant_lab.event_study import MacroEvent, load_event_registry
from quant_lab.qlib_archive import QlibArchiveError
from quant_lab.qlib_local import publish_release_tree
from test_qlib_archive import feature, fixture_files, write_release


def synthetic_group(length: int = 240):
    dates = tuple(date(2024, 1, 1) + timedelta(days=i) for i in range(length))
    bars = []
    for i in range(length):
        bars.append({
            symbol: (10.0 + stock * 3 + i * (0.01 + stock * 0.001)
                     + math.sin(i / (7 + stock)) * (0.2 + stock * 0.05),
                     1000.0 + 100 * stock + 40 * math.sin(i / (3 + stock)))
            for stock, symbol in enumerate(("A", "B", "C"))
        })
    return dates, bars


def reviewed_event(event_id: str, timestamp: str,
                   availability: str = "clock_verified") -> MacroEvent:
    event = MacroEvent(
        event_id=event_id, title="Synthetic policy event", category="monetary_policy.rrr",
        announcement_at=datetime.fromisoformat(timestamp), time_precision="minute",
        effective_date=None, source_url="https://www.pbc.gov.cn/example.html",
        publication_certainty="official_verified", availability_confidence=availability,
    )
    event.validate()
    return event


class GroupBehaviorPilotTest(unittest.TestCase):
    def test_walk_forward_maturity_and_future_perturbation(self):
        dates, bars = synthetic_group()
        events = [
            reviewed_event("during", dates[181].isoformat() + "T10:00:00+08:00"),
            reviewed_event("after", dates[185].isoformat() + "T18:00:00+08:00"),
            reviewed_event("date_verified_after", dates[187].isoformat() +
                           "T18:00:00+08:00", "date_verified"),
        ]
        config = PilotConfig(start=dates[0], horizons=(5,), step=5,
                             min_stocks=2, min_train=20, train_window=50)
        original = evaluate_group_behavior(dates, bars, 3, events, config=config)
        section = original["horizons"]["5"]
        self.assertEqual(3, section["event_stress"]["summary"]["paired_count"])
        stressed = section["event_stress"]["records"]
        self.assertEqual(dates[180].isoformat(), stressed[0]["origin_date"])
        self.assertEqual(dates[185].isoformat(), stressed[1]["origin_date"])
        self.assertEqual(dates[186].isoformat(), stressed[2]["origin_date"])
        self.assertTrue(section["regular"]["summary"]["model_and_baseline_sample_keys_identical"])
        self.assertGreater(section["regular"]["summary"]["paired_count"], 0)
        for record in section["regular"]["records"] + stressed:
            self.assertLessEqual(record["last_matured_training_target_date"],
                                 record["origin_date"])
            self.assertEqual(abs(record["actual_return"]),
                             record["zero_return_absolute_return_error"])
            self.assertEqual(record["horizon_sessions"], 5)
        changed = [dict(day) for day in bars]
        for position in range(181, len(changed)):
            changed[position] = {key: (close * 1.4, volume)
                                 for key, (close, volume) in changed[position].items()}
        perturbed = evaluate_group_behavior(dates, changed, 3, events, config=config)
        before = stressed[0]
        after = perturbed["horizons"]["5"]["event_stress"]["records"][0]
        self.assertEqual(before["feature_values"], after["feature_values"])
        self.assertEqual(before["model_prediction_return"], after["model_prediction_return"])
        self.assertNotEqual(before["actual_return"], after["actual_return"])

    def test_event_stress_fails_closed_on_unverified_or_invalid_registry(self):
        dates, bars = synthetic_group()
        verified = reviewed_event("valid", dates[181].isoformat() + "T18:00:00+08:00")
        config = PilotConfig(start=dates[0], horizons=(5,), step=5,
                             min_stocks=2, min_train=20, train_window=50)
        for event in (
            replace(verified, publication_certainty="unverified"),
            replace(verified, availability_confidence="unverified"),
            replace(verified, source_url="not-a-source"),
        ):
            with self.subTest(event=event):
                with self.assertRaises(ValueError):
                    evaluate_group_behavior(dates, bars, 3, [event], config=config)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.json"
            path.write_text(json.dumps([verified.to_record()]), encoding="utf-8")
            self.assertEqual((verified,), load_event_registry(path))
            invalid = verified.to_record()
            invalid.pop("availability_confidence")
            path.write_text(json.dumps([invalid]), encoding="utf-8")
            with self.assertRaises((KeyError, ValueError)):
                load_event_registry(path)

    def test_release_run_retains_registry_byte_hash_and_canonical_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            files = fixture_files()
            for symbol in ("sh600000", "sz000003"):
                files[f"qlib_bin/features/{symbol}/volume.day.bin"] = feature(0, 100, 110, 120)
            archive, manifest = write_release(directory, files)
            published = directory / "published"
            publish_release_tree(archive, manifest, "2000-01-06", published)
            event = reviewed_event("valid", "2000-01-05T18:00:00+08:00")
            registry = directory / "events.json"
            raw = json.dumps([event.to_record()], indent=2).encode("utf-8")
            registry.write_bytes(raw)
            config = PilotConfig(start=date(2000, 1, 4), horizons=(1,),
                                 min_stocks=2, min_train=10, train_window=10)
            result = run_qlib_group_behavior_pilot(
                published, manifest, "2000-01-06", registry, config=config)
            self.assertEqual("sha256:" + hashlib.sha256(raw).hexdigest(),
                             result["event_registry_sha256"])
            self.assertEqual([event.to_record()], result["reviewed_events"])
            registry.write_text(json.dumps([replace(
                event, publication_certainty="unverified").to_record()]),
                encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "verified publication"):
                run_qlib_group_behavior_pilot(
                    published, manifest, "2000-01-06", registry, config=config)

    def test_adjusted_volume_ratio_is_clipped_with_count(self):
        bars = [
            {"A": (10.0, 100.0), "B": (10.0, 100.0)},
            {"A": (11.0, 10000.0), "B": (9.0, 1.0)},
        ]
        daily = build_group_daily(bars, 2, 2, 4.0)
        self.assertEqual(2, daily[1]["clipped_volume_pairs"])
        self.assertAlmostEqual(math.log(4.0), daily[1]["signed_log_volume_change"])
        self.assertAlmostEqual(0.0, daily[1]["log_volume_change"])

    def test_verified_volume_is_required_and_hash_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            files = fixture_files()
            archive, manifest = write_release(directory, files)
            published = directory / "published"
            publish_release_tree(archive, manifest, "2000-01-06", published)
            with self.assertRaisesRegex(QlibArchiveError, "missing volume feature"):
                load_group_bars(published, manifest, "2000-01-06")

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            files = fixture_files()
            files["qlib_bin/features/sh600000/volume.day.bin"] = feature(0, 100, math.nan, 110)
            files["qlib_bin/features/sz000003/volume.day.bin"] = feature(0, 100, 120)
            archive, manifest = write_release(directory, files)
            published = directory / "published"
            publish_release_tree(archive, manifest, "2000-01-06", published)
            dates, bars, source = load_group_bars(published, manifest, "2000-01-06")
            self.assertEqual(3, len(dates))
            self.assertEqual(2, len(source["symbols"]))
            self.assertTrue(bars[0])
            with (published / "qlib_bin/features/sh600000/volume.day.bin").open("ab") as stream:
                stream.write(b"tampered")
            with self.assertRaises(QlibArchiveError):
                load_group_bars(published, manifest, "2000-01-06")

    def test_archive_is_append_only_and_integrity_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            payload = {"pilot_version": PILOT_VERSION, "horizons": {}}
            saved = archive_group_behavior_record(temporary, payload)
            path = Path(saved["path"])
            self.assertEqual(payload, read_group_behavior_record(path)["payload"])
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            record = json.loads(gzip.decompress(path.read_bytes()))
            record["payload"]["horizons"]["5"] = {"fabricated": True}
            path.write_bytes(gzip.compress(json.dumps(record).encode(), mtime=0))
            with self.assertRaisesRegex(ValueError, "integrity"):
                read_group_behavior_record(path)


if __name__ == "__main__":
    unittest.main()
