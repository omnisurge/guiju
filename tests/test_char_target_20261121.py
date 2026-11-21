"""B2/W1 characterization target suite: all 76 cases execute against the net layer.

The target fixture values are recorded from the final net implementation; every
case asserts all ten fields through the same real LocalServer recorder used by
the C0 baseline.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from guiju.net.block import BlockDetector
from guiju.net.body import BodyClient, finalize
from guiju.net.policy import BodyPolicy
from guiju.net.throttle import Throttle
from guiju.net.transport import Transport
from tests.char_case_recorder import record_case

FIXTURE = Path(__file__).parent / "fixtures/net/characterization-20261121.json"
CASES = json.loads(FIXTURE.read_text("utf-8"))["cases"]
REQUIRED = (
    "mode", "url_shape", "request_headers", "outcome", "exit",
    "body_read_bytes", "part_size", "final_exists", "request_count", "sleeps",
)


class TargetBackend:
    sidecar = True

    def __init__(self):
        self.policy = BodyPolicy(check_url_fn=lambda url: url)
        self.throttle = Throttle(0)
        self.body = BodyClient(self.policy, throttle=self.throttle)

    def download(self, url, part, *, attempts=1):
        return self.body.download(url, part, attempts=attempts)

    def get(self, url, *, attempts=1):
        return self.body.get_page(url, attempts=attempts)

    def check_url(self, url):
        return BodyPolicy().check_url(url)

    def block_check(self, body, content_type, reject_binary=False):
        BlockDetector().check(body, content_type, reject_binary=reject_binary)

    def remote_length(self, url):
        return Transport(self.policy, self.throttle).probe(url).content_length

    def proxy_ok(self):
        transport = Transport(self.policy, self.throttle)
        return transport.proxy_handler.proxies == {} and bool(transport.proxy_handler)

    def finalize_ok(self):
        tmp = Path(tempfile.mkdtemp(prefix="guiju-target-finalize-"))
        part = tmp / "a.part"
        dest = tmp / "a.pdf"
        part.write_bytes(b"%PDF-target")
        Path(str(part) + ".meta").write_text('{"etag": "", "last_modified": "", "total": 11, "size": 11}', encoding="utf-8")
        finalize(part, dest)
        return dest.read_bytes() == b"%PDF-target" and not part.exists() and not Path(str(part) + ".meta").exists()

    def throttle_sleeps(self, case_id):
        sleeps: list[float] = []
        t = Throttle(1.2, clock=lambda: 0.0, sleep=sleeps.append)
        n = int(case_id.rsplit("-", 1)[1])
        t.wait("a.gov.cn")
        t.wait("a.gov.cn" if n in (1, 3) else "b.gov.cn")
        return sleeps


class CharacterizationTest(unittest.TestCase):
    backend = TargetBackend()

    def assert_case(self, case_id: str) -> None:
        case = CASES.get(case_id)
        if case is None:
            self.fail(f"unhandled case {case_id}")
        actual = record_case(case_id, self.backend)
        for field in REQUIRED:
            self.assertIn(field, case["target"], f"{case_id} missing target.{field}")
            self.assertEqual(actual[field], case["target"][field], f"{case_id}.{field}")
        self.assertEqual(set(actual), set(REQUIRED))

    def validate_case(self, case: dict, case_id: str = "?") -> None:
        required = set(REQUIRED)
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

    def test_fixture_schema(self) -> None:
        self.assertEqual(len(CASES), 76)
        allowed_types = {
            "mode": str, "url_shape": str, "request_headers": dict,
            "outcome": str, "exit": int, "body_read_bytes": int,
            "part_size": int, "final_exists": bool, "request_count": int,
            "sleeps": list,
        }
        for case_id, case in CASES.items():
            self.validate_case(case, case_id)
            for side in ("expected", "target"):
                for key, typ in allowed_types.items():
                    self.assertIsInstance(case[side][key], typ, f"{case_id}.{side}.{key}")

    def test_fixture_schema_negative_removed_key(self) -> None:
        import copy
        for case_id, case in CASES.items():
            for side in ("expected", "target"):
                for key in REQUIRED:
                    broken = copy.deepcopy(case)
                    del broken[side][key]
                    with self.subTest(case=case_id, side=side, key=key), self.assertRaises(AssertionError):
                        self.validate_case(broken, case_id)


    def test_unknown_case_fails(self) -> None:
        with self.assertRaises(AssertionError):
            self.assert_case("CHAR-UNKNOWN-99")

    def test_unknown_group_fails(self) -> None:
        with self.assertRaises(AssertionError):
            from tests.char_case_recorder import record_case
            record_case("CHAR-UNKNOWN-01", self.backend)

    def test_target_fixture_mutation_outcome_detected(self) -> None:
        case = CASES["CHAR-206-01"]
        original = case["target"]["outcome"]
        case["target"]["outcome"] = "BOGUS"
        try:
            with self.assertRaises(AssertionError):
                self.assert_case("CHAR-206-01")
        finally:
            case["target"]["outcome"] = original

    def test_target_fixture_mutation_part_size_detected(self) -> None:
        case = CASES["CHAR-206-01"]
        original = case["target"]["part_size"]
        case["target"]["part_size"] = 999
        try:
            with self.assertRaises(AssertionError):
                self.assert_case("CHAR-206-01")
        finally:
            case["target"]["part_size"] = original

def _make(case_id: str):
    def test(self):
        self.assert_case(case_id)
    return test


for _case_id in CASES:
    setattr(CharacterizationTest, "test_" + _case_id.lower().replace("-", "_"), _make(_case_id))


if __name__ == "__main__":
    unittest.main()
