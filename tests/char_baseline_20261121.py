"""C0 baseline characterization for the pre-B2 (2a7be88) tree.

Each case is executed against a real LocalServer/socket-backed fixture and
compared with all ten recorded expected fields.  This module deliberately
does not import guiju.net: it must run on 2a7be88 before the new outbound
layer exists.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from guiju import compliance
from guiju.errors import ComplianceDenied, NeedsHuman, SourceUnreachable
from tests.char_case_recorder import record_case
from tests.char_baseline_harness import LocalServer, no_proxy

FIXTURE = Path(__file__).parent / "fixtures/net/characterization-20261121.json"
CASES = json.loads(FIXTURE.read_text("utf-8"))["cases"]
REQUIRED = (
    "mode", "url_shape", "request_headers", "outcome", "exit",
    "body_read_bytes", "part_size", "final_exists", "request_count", "sleeps",
)


class BaselineBackend:
    sidecar = False

    def _local(self):
        class Ctx:
            def __enter__(self):
                self._patches = mock.patch.multiple(
                    compliance,
                    check_url=lambda url: url,
                    _throttle=lambda host: None,
                )
                self._patches.start()
                self._no_proxy = no_proxy()
                self._no_proxy.__enter__()
                return self

            def __exit__(self, exc_type, exc, tb):
                self._no_proxy.__exit__(exc_type, exc, tb)
                self._patches.stop()
                return False

        return Ctx()

    def download(self, url, part, *, attempts=1):
        with self._local():
            return compliance.download(url, part, attempts=attempts)

    def get(self, url, *, attempts=1):
        with self._local():
            return compliance.get(url, attempts=attempts)

    def check_url(self, url):
        return compliance.check_url(url)

    def block_check(self, body, content_type, reject_binary=False):
        compliance._detect_block(compliance.Response(url="https://x.gov.cn/a", body=body, content_type=content_type))

    def remote_length(self, url):
        with self._local():
            return compliance._remote_length(url, referer=None, timeout=5)

    def proxy_ok(self):
        return hasattr(compliance, "_opener")

    def finalize_ok(self):
        source = Path("guiju/cli/commands/pull.py").read_text("utf-8")
        return "os.replace" in source

    def throttle_sleeps(self, case_id):
        sleeps: list[float] = []
        now = [0.0]
        compliance._last_call.clear()
        n = int(case_id.rsplit("-", 1)[1])
        with mock.patch.object(compliance.time, "monotonic", side_effect=lambda: now[0]),              mock.patch.object(compliance.time, "sleep", side_effect=lambda value: (sleeps.append(value), now.__setitem__(0, now[0] + value))):
            t = compliance._throttle
            t("a.gov.cn")
            t("a.gov.cn" if n in (1, 3) else "b.gov.cn")
        return sleeps


class BaselineCharacterizationTest(unittest.TestCase):
    backend = BaselineBackend()

    def assert_case(self, case_id: str) -> None:
        case = CASES.get(case_id)
        if case is None:
            self.fail(f"unhandled case {case_id}")
        actual = record_case(case_id, self.backend)
        for field in REQUIRED:
            self.assertIn(field, case["expected"], f"{case_id} missing expected.{field}")
            self.assertEqual(actual[field], case["expected"][field], f"{case_id}.{field}")
        self.assertEqual(set(actual), set(REQUIRED))

    def test_fixture_schema(self) -> None:
        self.assertEqual(len(CASES), 76)
        self.assertEqual(len(CASES), 76)
        allowed_types = {
            "mode": str, "url_shape": str, "request_headers": dict,
            "outcome": str, "exit": int, "body_read_bytes": int,
            "part_size": int, "final_exists": bool, "request_count": int,
            "sleeps": list,
        }
        group_counts = {}
        for case_id, case in CASES.items():
            group_counts[case["group"]] = group_counts.get(case["group"], 0) + 1
            for side in ("expected", "target"):
                obj = case.get(side)
                self.assertIsInstance(obj, dict, f"{case_id}.{side}")
                self.assertEqual(set(obj), set(REQUIRED), f"{case_id}.{side} keys")
                for key, typ in allowed_types.items():
                    self.assertIsInstance(obj[key], typ, f"{case_id}.{side}.{key} type")
                    if typ is str:
                        self.assertNotIn(obj[key], ("", "W1 target"), f"{case_id}.{side}.{key} placeholder")
                self.assertFalse(obj["final_exists"] and obj["exit"] not in (0, 10), f"{case_id}.{side} final/exit")
        self.assertEqual(group_counts, {"A": 7, "B": 10, "C": 5, "D": 7, "E": 8, "F": 5, "G": 6, "H": 12, "I": 6, "J": 4, "K": 6})

    def validate_case(self, case: dict, case_id: str = "?") -> None:
        required = set(REQUIRED)
        allowed_types = {
            "mode": str, "url_shape": str, "request_headers": dict,
            "outcome": str, "exit": int, "body_read_bytes": int,
            "part_size": int, "final_exists": bool, "request_count": int,
            "sleeps": list,
        }
        if not isinstance(case, dict):
            raise AssertionError(f"{case_id} case not object")
        for side in ("expected", "target"):
            obj = case.get(side)
            if not isinstance(obj, dict):
                raise AssertionError(f"{case_id}.{side} missing")
            if set(obj) != required:
                raise AssertionError(f"{case_id}.{side} keys")
            for key in REQUIRED:
                val = obj[key]
                if val is None or (isinstance(val, str) and not val):
                    raise AssertionError(f"{case_id}.{side}.{key} empty")
                if not isinstance(val, allowed_types[key]):
                    raise AssertionError(f"{case_id}.{side}.{key} type")

    def test_fixture_schema_negative_removed_key(self) -> None:
        import copy
        self.validate_case(CASES["CHAR-200-01"], "CHAR-200-01")
        for case_id, case in CASES.items():
            for side in ("expected", "target"):
                for key in REQUIRED:
                    broken = copy.deepcopy(case)
                    del broken[side][key]
                    with self.subTest(case=case_id, side=side, key=key):
                        with self.assertRaises(AssertionError):
                            self.validate_case(broken, case_id)

    def test_baseline_source_has_no_net_import(self) -> None:
        import ast
        here = Path(__file__).resolve()
        for path in (here, here.with_name("char_baseline_harness.py"), here.with_name("char_case_recorder.py")):
            tree = ast.parse(path.read_text("utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertNotIn("guiju.net", [alias.name for alias in node.names], path.name)
                elif isinstance(node, ast.ImportFrom):
                    self.assertNotEqual(node.module, "guiju.net", path.name)


def _make(case_id: str):
    def test(self):
        self.assert_case(case_id)
    return test


for _case_id in CASES:
    setattr(BaselineCharacterizationTest, "test_" + _case_id.lower().replace("-", "_"), _make(_case_id))


if __name__ == "__main__":
    unittest.main()
