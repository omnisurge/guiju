"""A1：批量 pull 命中 40 后必须立即停止，不得继续消耗后续请求。"""

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from guiju.application import pull
from guiju.errors import ComplianceDenied, NeedsHuman, SourceUnreachable


class BatchStopTest(unittest.TestCase):
    def _run(self, side_effect):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        refs = Path(tmp.name) / "refs.txt"
        refs.write_text("A-1\nB-2\nC-3\n", encoding="utf-8")
        args = argparse.Namespace(list=str(refs), out=tmp.name, readable=False)
        with mock.patch.object(pull, "_pull_one", side_effect=side_effect) as one, \
                mock.patch.object(pull.render, "emit") as emit, \
                mock.patch.object(pull.render, "note"), \
                mock.patch.object(pull.render, "is_json", return_value=True):
            code = pull._batch(mock.Mock(), args, mock.Mock())
        payload = emit.call_args.args[0]
        return code, one.call_args_list, payload

    def test_compliance_error_stops_immediately(self):
        calls = []

        def side_effect(store, args, ref, source, out_dir):
            calls.append(ref)
            if ref == "B-2":
                raise ComplianceDenied("blocked")
            return {"exit": 0, "code": ref}

        code, one_calls, payload = self._run(side_effect)
        self.assertEqual(code, 40)
        self.assertEqual(calls, ["A-1", "B-2"])
        self.assertEqual(len(one_calls), 2)
        self.assertEqual(payload["done"], 2)
        self.assertEqual(payload["total"], 3)
        self.assertEqual(payload["stopped"], "compliance")

    def test_compliance_result_dict_stops_immediately(self):
        calls = []

        def side_effect(store, args, ref, source, out_dir):
            calls.append(ref)
            return {"exit": 40, "code": ref} if ref == "B-2" else {"exit": 0, "code": ref}

        code, one_calls, payload = self._run(side_effect)
        self.assertEqual(code, 40)
        self.assertEqual(calls, ["A-1", "B-2"])
        self.assertEqual(len(one_calls), 2)
        self.assertEqual(payload["stopped"], "compliance")
        self.assertEqual(payload["results"][1]["outcome"], "compliance")

    def test_earlier_fatal_codes_do_not_mask_40(self):
        cases = [
            (SourceUnreachable("unreachable"), 20),
            (NeedsHuman("needs human"), 50),
            (OSError("disk full"), 1),
        ]
        for first, expected in cases:
            with self.subTest(first=type(first).__name__):
                calls = []

                def side_effect(store, args, ref, source, out_dir,
                                _calls=calls, _first=first):
                    _calls.append(ref)
                    if ref == "A-1":
                        raise _first
                    if ref == "B-2":
                        raise ComplianceDenied("blocked")
                    return {"exit": 0, "code": ref}

                code, one_calls, payload = self._run(side_effect)
                self.assertEqual(code, 40)
                self.assertEqual(calls, ["A-1", "B-2"])
                self.assertEqual(len(one_calls), 2)
                self.assertEqual(payload["counts"][str(expected)], 1)
                self.assertEqual(payload["counts"]["40"], 1)
                self.assertEqual(payload["stopped"], "compliance")

    def test_non_40_fatal_code_keeps_batch_going(self):
        calls = []

        def side_effect(store, args, ref, source, out_dir):
            calls.append(ref)
            if ref == "A-1":
                raise SourceUnreachable("unreachable")
            return {"exit": 0, "code": ref}

        code, one_calls, payload = self._run(side_effect)
        self.assertEqual(code, 20)
        self.assertEqual(calls, ["A-1", "B-2", "C-3"])
        self.assertEqual(len(one_calls), 3)
        self.assertIsNone(payload["stopped"])


if __name__ == "__main__":
    unittest.main()
