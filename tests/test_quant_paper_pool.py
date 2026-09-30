from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from sys import path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
path.insert(0, str(ROOT / 'src'))
path.insert(0, str(ROOT / 'scripts'))
import quant_paper_pool


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 1, 17, 0, tzinfo=tz or ZoneInfo('Asia/Shanghai'))


class QuantPaperPoolIntegrationTest(unittest.TestCase):
    def test_next_session_executes_preexisting_decision_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / 'market.sqlite3'
            policy_path = root / 'policy.json'
            out = root / 'account'
            policy = json.loads((ROOT / 'policies' / 'ensemble-focus-three-v2-five-bps.json').read_text())
            policy_path.write_text(json.dumps(policy))
            first = date(2026, 9, 30) - timedelta(days=70)
            dates = [(first + timedelta(days=i)).isoformat() for i in range(71)]
            self.assertEqual(dates[-2], policy['start_asof'])
            with sqlite3.connect(db) as con:
                con.execute('CREATE TABLE daily_bars (instrument_id TEXT, trade_date TEXT, '
                            'close REAL, adjustment TEXT, source_id TEXT, payload_hash TEXT, '
                            'run_id TEXT, fetched_at TEXT)')
                for index, day in enumerate(dates):
                    for key, price in zip(policy['universe'],
                                          (10 + index * 0.05, 20 + index * 0.03, 30 + index * 0.08)):
                        con.execute('INSERT INTO daily_bars VALUES (?,?,?,?,?,?,?,?)',
                                    (key, day, price, 'qfq', 'synthetic', f'hash-{index}',
                                     'synthetic-run', '2026-09-30T08:30:00+00:00'))
                    con.execute('INSERT INTO daily_bars VALUES (?,?,?,?,?,?,?,?)',
                                ('index:000300.SH', day, 100 + index, 'none', 'synthetic',
                                 f'index-{index}', 'synthetic-run', '2026-09-30T08:30:00+00:00'))
            args = SimpleNamespace(out=str(out), policy=str(policy_path), db=str(db), date='2026-09-30')
            def raw(_db, keys, day):
                return {key: {'close': 10.0 + index * 10, 'volume_lots': 10000,
                              'adjustment': 'none', 'source_id': 'synthetic',
                              'payload_hash': f'raw-{day}-{key}',
                              'fetched_at': '2026-09-30T08:30:00+00:00'}
                        for index, key in enumerate(keys)}
            with patch.object(quant_paper_pool, 'datetime', FixedDateTime), patch.object(quant_paper_pool, 'raw_prices', raw):
                quant_paper_pool.init(args)
                first_run = quant_paper_pool.advance(args)
                second_run = quant_paper_pool.advance(args)
            self.assertEqual(first_run['new_sessions'], 1)
            self.assertEqual(second_run['new_sessions'], 0)
            self.assertEqual(first_run['status']['observed_sessions'], 1)
            self.assertGreater(first_run['status']['simulated_order_count'], 0)
            self.assertFalse(first_run['status']['preliminary_gate_passed'])
            row = json.loads((out / 'sessions' / '2026-09-30.json').read_text())
            opening = json.loads((out / 'sessions' / '2026-09-29.json').read_text())
            self.assertEqual(row['executed_decision_sha256'], opening['pending_decision']['decision_sha256'])
            self.assertIn('stress_strategy', row)
            self.assertLessEqual(row['stress_strategy']['equity'], row['strategy']['equity'])


if __name__ == '__main__':
    unittest.main()
