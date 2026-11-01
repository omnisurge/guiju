"""备源只能恢复源故障；成功空结果、合规拒绝与取消都不能换源。"""

import unittest
from unittest import mock

from guiju.currency.fallback import FallbackCurrency
from guiju.currency.model import QueryCancelled, Record
from guiju.errors import ComplianceDenied, NeedsHuman, SourceUnreachable


def source(name, rows=None, error=None):
    item = mock.Mock()
    item.name = name
    item.lookup = mock.Mock(side_effect=error, return_value=[] if rows is None else rows)
    return item


class FallbackTest(unittest.TestCase):
    def test_primary_nonempty_or_empty_success_stops(self):
        for rows in ([], [Record("GB1", "current", source="p")]):
            primary, backup = source("p", rows), source("b")
            self.assertEqual(FallbackCurrency("p", primary, backup).lookup("GB1"), rows)
            backup.lookup.assert_not_called()

    def test_backup_order_skips_errors_and_empty_and_marks_first_hit(self):
        original = Record("GB1", "current", source="b3", publish_date="2020-01-01", raw="证据")
        primary = source("p", error=SourceUnreachable("offline"))
        backups = [source("b1", error=NeedsHuman("broken")), source("b2"), source("b3", [original]), source("b4")]
        chain = FallbackCurrency("p", primary, None, *backups)
        result = chain.lookup("GB1")
        self.assertEqual(result[0].fallback, "b3")
        self.assertEqual(result[0].publish_date, original.publish_date)
        self.assertEqual(original.fallback, "")
        self.assertIsNot(result[0], original)
        for backup in backups[:3]:
            backup.lookup.assert_called_once_with("GB1")
        backups[3].lookup.assert_not_called()
        self.assertEqual(chain.source_names, ("p", "b1", "b2", "b3", "b4"))

    def test_all_failures_report_order_and_do_not_return_empty_success(self):
        primary = source("p", error=SourceUnreachable("primary down"))
        chain = FallbackCurrency("p", primary, source("b1"), source("b2", error=SourceUnreachable("backup down")))
        with self.assertRaises(SourceUnreachable) as ctx:
            chain.lookup("GB1")
        self.assertIsNotNone(ctx.exception.__cause__)
        self.assertLess(str(ctx.exception).index("p:"), str(ctx.exception).index("b1 无命中"))
        self.assertIn("b2: backup down", str(ctx.exception))

    def test_primary_parse_failure_retains_needs_human_if_no_backup_hit(self):
        with self.assertRaises(NeedsHuman):
            FallbackCurrency("p", source("p", error=NeedsHuman("改版")), source("b")).lookup("GB1")

    def test_primary_40_never_calls_backup(self):
        error = ComplianceDenied("captcha")
        backup = source("b")
        with self.assertRaises(ComplianceDenied) as ctx:
            FallbackCurrency("p", source("p", error=error), backup).lookup("GB1")
        self.assertIs(ctx.exception, error)
        backup.lookup.assert_not_called()

    def test_backup_40_never_continues_or_rewraps(self):
        error = ComplianceDenied("blocked")
        last = source("last")
        with self.assertRaises(ComplianceDenied) as ctx:
            FallbackCurrency("p", source("p", error=SourceUnreachable("down")),
                             source("blocked", error=error), last).lookup("GB1")
        self.assertIs(ctx.exception, error)
        last.lookup.assert_not_called()

    def test_no_primary_is_not_replaced_by_backup(self):
        backup = source("b")
        with self.assertRaises(SourceUnreachable):
            FallbackCurrency("p", None, backup).lookup("GB1")
        backup.lookup.assert_not_called()

    def test_pre_cancelled_never_calls_primary(self):
        primary = source("p")
        with self.assertRaises(QueryCancelled):
            FallbackCurrency("p", primary, cancelled=lambda: True).lookup("GB1")
        primary.lookup.assert_not_called()

    def test_cancel_during_primary_stops_backup(self):
        state = {"cancelled": False}
        primary, backup = source("p"), source("b")

        def fail(_code):
            state["cancelled"] = True
            raise SourceUnreachable("slow")

        primary.lookup.side_effect = fail
        with self.assertRaises(QueryCancelled):
            FallbackCurrency("p", primary, backup, cancelled=lambda: state["cancelled"]).lookup("GB1")
        backup.lookup.assert_not_called()

    def test_programming_errors_and_keyboard_interrupt_are_not_recovered(self):
        for error in (ValueError("bug"), KeyboardInterrupt(), QueryCancelled("cancelled")):
            backup = source("b")
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)):
                FallbackCurrency("p", source("p", error=error), backup).lookup("GB1")
            backup.lookup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
