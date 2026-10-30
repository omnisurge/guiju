"""可得性事实 —— 所有者要求的「把 2014 这个事实告诉使用者」。"""

import unittest

from guiju.domain.availability import FULL_TEXT_SINCE_YEAR, Window, coverage_statement, note, state


class AvailabilityTest(unittest.TestCase):
    def test_window_boundary(self):
        self.assertIs(state("2013-12-31"), Window.PRE_WINDOW)
        self.assertIs(state("2014-01-01"), Window.IN_WINDOW)
        self.assertIs(state("2026-09-23"), Window.IN_WINDOW)
        self.assertIs(state(""), Window.UNKNOWN)

    def test_pre_window_note_tells_the_truth_and_forbids_retry(self):
        msg = note(Window.PRE_WINDOW, "2005-03-30")
        self.assertIn(str(FULL_TEXT_SINCE_YEAR), msg)
        self.assertIn("不是故障", msg)
        self.assertIn("不需要重试", msg)

    def test_in_window_note_says_it_is_abnormal(self):
        self.assertIn("异常", note(Window.IN_WINDOW, "2020-01-01"))

    def test_coverage_statement_mentions_year(self):
        self.assertIn(str(FULL_TEXT_SINCE_YEAR), coverage_statement())


if __name__ == "__main__":
    unittest.main()
