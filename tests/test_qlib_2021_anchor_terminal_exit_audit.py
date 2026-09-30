import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from qlib_2021_anchor_terminal_exit_audit import classify_window


class TerminalPathClassificationTests(unittest.TestCase):
    def test_entry_after_last_trade_is_unexecutable(self):
        self.assertEqual(classify_window('2025-08-13', '2025-12-01', '2025-08-12'),
                         'entry_after_last_trade_unexecutable')

    def test_window_crossing_terminal_halt_has_potential_claim(self):
        self.assertEqual(classify_window('2025-08-12', '2025-12-01', '2025-08-12'),
                         'window_crosses_terminal_halt_potential_claim')

    def test_window_maturing_on_last_trade_is_before_exit(self):
        self.assertEqual(classify_window('2025-01-03', '2025-08-12', '2025-08-12'),
                         'entire_window_before_last_trade')


if __name__ == '__main__':
    unittest.main()
