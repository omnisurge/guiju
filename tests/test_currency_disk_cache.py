"""原生磁盘缓存：只用临时目录的离线契约测试，不触及真实 GUIJU_HOME。"""

import copy
import hashlib
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import asdict, fields, replace
from pathlib import Path
from unittest import mock

from guiju.currency.cache import CACHE_NAMESPACE, DEFAULT_TTL, CacheEntry
from guiju.currency.model import Record
from guiju.store import currency as disk_mod
from guiju.store.currency import DiskCurrencyCache

QUERY = "GB50011"
RECORD = Record(
    code="GB 50011-2010",
    status="superseded",
    name="建筑抗震设计规范",
    implement_date="2010-12-01",
    source="csres",
    replaces="GB 50011-2001",
    publish_date="2010-05-31",
    raw="原始状态：作废\n保留原文证据",
    fallback="std_samr",
)


class DiskCurrencyCacheTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "cache"
        self.now = 1_700_000_000.5
        self.cache = DiskCurrencyCache(self.directory, clock=lambda: self.now)

    def seed(self, *, code=QUERY, namespace=CACHE_NAMESPACE, records=None):
        self.cache.put(code, [RECORD] if records is None else records, namespace=namespace)
        for path in self.directory.glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("query") == code and data.get("namespace") == namespace:
                return path, data
        self.fail("put 没有生成匹配的缓存文件")

    def assert_miss(self):
        self.assertIsNone(self.cache.get(QUERY))
        self.assertIsNone(self.cache.get_any(QUERY))

    def test_constructor_and_read_never_create_directory(self):
        with mock.patch.object(Path, "mkdir", side_effect=AssertionError("read must not mkdir")):
            cache = DiskCurrencyCache(self.directory, clock=lambda: self.now)
            self.assertIsNone(cache.get(QUERY))
            self.assertIsNone(cache.get_any(QUERY))
        self.assertFalse(self.directory.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_none_and_empty_directory_disable_all_io_and_home_discovery(self):
        fail = AssertionError("disabled cache must not discover or access any directory")
        fake_home = self.root / "unused-guiju-home"
        with ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, {"GUIJU_HOME": str(fake_home)}))
            for target, attribute in ((Path, "open"), (Path, "mkdir"), (Path, "home"), (os, "getenv")):
                stack.enter_context(mock.patch.object(target, attribute, side_effect=fail))
            for directory in (None, ""):
                with self.subTest(directory=directory):
                    cache = DiskCurrencyCache(directory, clock=mock.Mock(side_effect=fail))
                    cache.put(QUERY, [RECORD])
                    self.assertIsNone(cache.get(QUERY))
                    self.assertIsNone(cache.get_any(QUERY))
        self.assertFalse(fake_home.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_explicit_put_creates_nested_directory_and_roundtrips_all_fields(self):
        self.directory = self.root / "nested" / "cache"
        self.cache = DiskCurrencyCache(str(self.directory), clock=lambda: self.now)
        path, data = self.seed()
        self.assertEqual(data["schema"], 1)
        self.assertEqual(data["namespace"], CACHE_NAMESPACE)
        self.assertEqual(data["query"], QUERY)
        self.assertEqual(data["queried_at"], self.now)
        self.assertEqual(data["results"], [asdict(RECORD)])
        self.assertEqual(set(data["results"][0]), {field.name for field in fields(Record)})
        restarted = DiskCurrencyCache(self.directory, clock=lambda: self.now)
        self.assertEqual(restarted.get(QUERY), CacheEntry(self.now, (RECORD,)))
        self.assertEqual(restarted.get_any(QUERY), CacheEntry(self.now, (RECORD,)))
        self.assertEqual(path.parent, self.directory)
        self.assertEqual(list(self.directory.iterdir()), [path])

    def test_all_statuses_and_empty_optional_strings_roundtrip(self):
        records = [Record(code="GB 1-2026", status=status) for status in (
            "current", "superseded", "upcoming", "withdrawn", "unknown"
        )]
        self.seed(records=records)
        self.assertEqual(self.cache.get(QUERY).results, tuple(records))

    def test_negative_cache_is_distinct_from_miss_and_corruption(self):
        path, _ = self.seed(records=[])
        self.assertEqual(self.cache.get(QUERY), CacheEntry(self.now, ()))
        self.assertEqual(self.cache.get_any(QUERY), CacheEntry(self.now, ()))
        self.assertIsNone(self.cache.get("not-queried"))
        for raw in ("{}", "null", "[]"):
            with self.subTest(raw=raw):
                path.write_text(raw, encoding="utf-8")
                self.assert_miss()

    def test_default_24h_boundary_and_get_any_has_no_hidden_stale_cutoff(self):
        _, data = self.seed()
        fetched = data["queried_at"]
        self.assertEqual(DEFAULT_TTL, 86_400)
        self.now = fetched + DEFAULT_TTL - 0.125
        self.assertIsNotNone(self.cache.get(QUERY))
        self.now = fetched + DEFAULT_TTL
        self.assertIsNone(self.cache.get(QUERY))
        self.assertEqual(self.cache.get_any(QUERY), CacheEntry(fetched, (RECORD,)))
        self.now = fetched + 90 * DEFAULT_TTL
        self.assertIsNone(self.cache.get(QUERY))
        self.assertEqual(self.cache.get_any(QUERY), CacheEntry(fetched, (RECORD,)))

    def test_zero_and_negative_ttl_disable_fresh_but_not_get_any(self):
        self.seed()
        for ttl in (0, -1):
            with self.subTest(ttl=ttl):
                cache = DiskCurrencyCache(self.directory, ttl=ttl, clock=lambda: self.now)
                self.assertIsNone(cache.get(QUERY))
                self.assertEqual(cache.get_any(QUERY), CacheEntry(self.now, (RECORD,)))

    def test_clock_rollback_rejects_future_entries_even_for_get_any(self):
        self.seed()
        self.now -= 1
        self.assert_miss()

    def test_raw_query_namespace_and_path_boundaries_cannot_collide(self):
        pairs = [
            ("route-a", "GB50011"), ("route-b", "GB50011"),
            ("route-a", "GB 50011"), ("route-a", "gb50011"),
            ("a", "b:c"), ("a:b", "c"),
            ("a", "b\x00c"), ("a\x00b", "c"),
            ("../route", "../../escaped/GB50011"),
            ("中文/来源", "DG/TJ08-2007-2026"),
        ]
        expected = {}
        for index, (namespace, query) in enumerate(pairs):
            record = replace(RECORD, name=f"entry-{index}")
            self.cache.put(query, [record], namespace=namespace)
            expected[namespace, query] = record
        paths = list(self.directory.iterdir())
        self.assertEqual(len(paths), len(pairs))
        for path in paths:
            self.assertEqual(path.parent, self.directory)
            self.assertRegex(path.name, r"^native-v1-[0-9a-f]{64}\.json$")
        for (namespace, query), record in expected.items():
            with self.subTest(namespace=namespace, query=query):
                self.assertEqual(self.cache.get(query, namespace=namespace).results, (record,))
        self.assertIsNone(self.cache.get(QUERY))
        self.assertEqual(list(self.root.iterdir()), [self.directory])

    def test_copied_other_query_or_namespace_envelope_is_a_miss(self):
        target, _ = self.seed()
        for query, namespace in (("GB 50011", CACHE_NAMESPACE), (QUERY, "other-route")):
            with self.subTest(query=query, namespace=namespace):
                source, _ = self.seed(code=query, namespace=namespace)
                target.write_bytes(source.read_bytes())
                self.assert_miss()

    def test_unnamespaced_pre_native_cache_is_intentionally_not_reused(self):
        self.directory.mkdir()
        legacy = self.directory / (hashlib.sha256(QUERY.encode()).hexdigest() + ".json")
        legacy.write_text(json.dumps({"queried_at": self.now, "results": [asdict(RECORD)]}), encoding="utf-8")
        self.assert_miss()
        native, _ = self.seed()
        self.assertNotEqual(native.name, legacy.name)
        self.assertTrue(legacy.exists())

    def test_non_object_invalid_utf8_truncated_and_deep_json_are_misses(self):
        path, _ = self.seed()
        for raw in (b"", b"\xff", b"{", b"{not-json}", b"null", b"[]", b"0", b"true", b'"text"',
                    b"[" * 2000 + b"]" * 2000):
            with self.subTest(raw=raw[:40]):
                path.write_bytes(raw)
                self.assert_miss()
                self.assertTrue(path.exists(), "reads must not delete corrupt files")

    def test_envelope_requires_all_fields_correct_schema_and_identity(self):
        path, good = self.seed()
        bad_payloads = []
        for field in good:
            data = copy.deepcopy(good)
            del data[field]
            bad_payloads.append(data)
        for field, values in (
            ("schema", [True, 1.0, "1", None, 0, 2]),
            ("namespace", [None, [], {}, True, 3, "wrong"]),
            ("query", [None, [], {}, True, 3, "GB 50011"]),
            ("results", [None, {}, "[]", True, 0, [None], [[]], [True], ["record"]]),
        ):
            for value in values:
                data = copy.deepcopy(good)
                data[field] = value
                bad_payloads.append(data)
        bad_payloads.append({**good, "unexpected": "schema drift"})
        for data in bad_payloads:
            with self.subTest(data=data):
                path.write_text(json.dumps(data), encoding="utf-8")
                self.assert_miss()

    def test_timestamp_rejects_zero_negative_future_nonfinite_bool_and_wrong_types(self):
        path, good = self.seed()
        for stamp in (0, 0.0, -1, self.now + 1, float("nan"), float("inf"), -float("inf"),
                      True, False, None, str(self.now), [], {}, 10 ** 400):
            with self.subTest(stamp=stamp):
                data = {**good, "queried_at": stamp}
                path.write_text(json.dumps(data), encoding="utf-8")
                self.assert_miss()
        path.write_text(json.dumps({**good, "queried_at": int(self.now)}), encoding="utf-8")
        self.assertEqual(self.cache.get(QUERY).queried_at, float(int(self.now)))

    def test_record_requires_all_fields_exact_string_types_and_known_status(self):
        path, good = self.seed()
        for field in asdict(RECORD):
            for value in (None, False, 0, 0.0, [], {}):
                with self.subTest(field=field, value=value):
                    data = copy.deepcopy(good)
                    data["results"][0][field] = value
                    path.write_text(json.dumps(data), encoding="utf-8")
                    self.assert_miss()
            with self.subTest(missing=field):
                data = copy.deepcopy(good)
                del data["results"][0][field]
                path.write_text(json.dumps(data), encoding="utf-8")
                self.assert_miss()
        for status in ("", "CURRENT", "现行", "invalid"):
            data = copy.deepcopy(good)
            data["results"][0]["status"] = status
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assert_miss()
        data = copy.deepcopy(good)
        data["results"][0]["extra"] = "unknown schema field"
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assert_miss()

    def test_one_bad_record_rejects_whole_entry_not_partial_or_negative(self):
        path, good = self.seed()
        bad = {**asdict(RECORD), "status": "invalid"}
        for rows in ([asdict(RECORD), bad], [bad, asdict(RECORD)]):
            path.write_text(json.dumps({**good, "results": rows}), encoding="utf-8")
            self.assert_miss()

    def test_duplicate_envelope_or_record_keys_are_rejected(self):
        path, good = self.seed()
        encoded = json.dumps(good)
        duplicate_schema = encoded.replace('"schema": 1', '"schema": 999, "schema": 1', 1)
        duplicate_status = encoded.replace('"status": "superseded"',
                                           '"status": "invalid", "status": "superseded"', 1)
        for raw in (duplicate_schema, duplicate_status):
            self.assertNotEqual(raw, encoded)
            path.write_text(raw, encoding="utf-8")
            self.assert_miss()

    def test_read_uses_a_hard_byte_limit_and_oversize_put_does_not_create_directory(self):
        with mock.patch.object(disk_mod, "_MAX_CACHE_BYTES", 128):
            stream = mock.MagicMock()
            stream.__enter__.return_value = stream
            stream.read.return_value = b" " * 129
            with mock.patch.object(Path, "open", return_value=stream):
                self.assertIsNone(self.cache.get_any(QUERY))
            stream.read.assert_called_once_with(129)
            self.cache.put(QUERY, [RECORD])
        self.assertFalse(self.directory.exists())

    def test_read_io_errors_are_misses_but_programmer_assertions_propagate(self):
        path, _ = self.seed()
        original = path.read_bytes()
        for method in (self.cache.get, self.cache.get_any):
            with mock.patch.object(Path, "open", side_effect=PermissionError("read denied")):
                self.assertIsNone(method(QUERY))
            with mock.patch.object(Path, "open", side_effect=AssertionError("programmer bug")), \
                    self.assertRaisesRegex(AssertionError, "programmer bug"):
                method(QUERY)
        self.assertEqual(path.read_bytes(), original)

    def test_invalid_clock_never_reads_or_writes(self):
        fail = AssertionError("invalid clock must not perform IO")
        for now in (0, -1, float("nan"), float("inf"), True, "now", 10 ** 400):
            with self.subTest(now=now), mock.patch.object(Path, "open", side_effect=fail), \
                    mock.patch.object(Path, "mkdir", side_effect=fail):
                cache = DiskCurrencyCache(self.directory, clock=lambda now=now: now)
                self.assertIsNone(cache.get(QUERY))
                self.assertIsNone(cache.get_any(QUERY))
                cache.put(QUERY, [RECORD])
        self.assertFalse(self.directory.exists())

    def test_invalid_metadata_put_does_not_touch_directory(self):
        invalid = ([{}], [None], [replace(RECORD, status="invalid")], [replace(RECORD, fallback=None)],
                   None, {}, (RECORD,))
        with mock.patch.object(Path, "mkdir", side_effect=AssertionError("invalid metadata must not write")):
            for records in invalid:
                with self.subTest(records=records):
                    self.cache.put(QUERY, records)
        self.assertFalse(self.directory.exists())

    def test_mkdir_and_temporary_creation_io_errors_are_noops(self):
        for target in ("pathlib.Path.mkdir", "guiju.store.currency.tempfile.NamedTemporaryFile"):
            with self.subTest(target=target), mock.patch(target, side_effect=PermissionError("write denied")):
                self.cache.put(QUERY, [RECORD])
                self.assert_miss()
        self.assertEqual(list(self.directory.glob("*")), [])

    def test_fsync_or_replace_failure_preserves_old_cache_and_foreign_temporary(self):
        path, _ = self.seed()
        original = path.read_bytes()
        foreign = self.directory / f".{path.name}.somebody-else.tmp"
        foreign.write_bytes(b"foreign temporary")
        for target in ("guiju.store.currency.os.fsync", "guiju.store.currency.os.replace"):
            with self.subTest(target=target), mock.patch(target, side_effect=OSError("disk failure")):
                self.cache.put(QUERY, [replace(RECORD, name="must not overwrite")])
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(foreign.read_bytes(), b"foreign temporary")
            self.assertEqual(set(self.directory.iterdir()), {path, foreign})
            self.assertEqual(self.cache.get(QUERY).results, (RECORD,))

    def test_partial_write_failure_cleans_only_its_unique_temporary(self):
        path, _ = self.seed()
        original = path.read_bytes()
        foreign = self.directory / f".{path.name}.foreign.tmp"
        foreign.write_bytes(b"foreign")
        real_temporary = tempfile.NamedTemporaryFile
        temporary_paths = []

        def partial_file(*args, **kwargs):
            stream = real_temporary(*args, **kwargs)
            temporary_paths.append(Path(stream.name))
            write = stream.write

            def partial_write(data):
                write(data[:10])
                raise OSError("short disk write")

            stream.write = partial_write
            return stream

        with mock.patch.object(disk_mod.tempfile, "NamedTemporaryFile", side_effect=partial_file):
            self.cache.put(QUERY, [RECORD])
            self.cache.put(QUERY, [RECORD])
        self.assertEqual(len(set(temporary_paths)), 2)
        self.assertTrue(all(temp.parent == self.directory for temp in temporary_paths))
        self.assertTrue(all(not temp.exists() for temp in temporary_paths))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(set(self.directory.iterdir()), {path, foreign})

    def test_replace_happens_only_after_closed_complete_json_and_overwrites_atomically(self):
        path, _ = self.seed()
        original = path.read_bytes()
        real_replace = os.replace
        replacement = replace(RECORD, name="新查询结果", fallback="ccsn")
        observed = []

        def inspect_replace(source, destination):
            source = Path(source)
            self.assertEqual(source.parent, path.parent)
            self.assertEqual(Path(destination), path)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(json.loads(source.read_bytes())["results"], [asdict(replacement)])
            observed.append(source)
            real_replace(source, destination)

        with mock.patch.object(disk_mod.os, "replace", side_effect=inspect_replace):
            self.cache.put(QUERY, [replacement])
        self.assertEqual(len(observed), 1)
        self.assertFalse(observed[0].exists())
        self.assertEqual(list(self.directory.iterdir()), [path])
        self.assertEqual(self.cache.get(QUERY).results, (replacement,))

    def test_programmer_assertions_during_write_or_clock_are_not_swallowed(self):
        path, _ = self.seed()
        original = path.read_bytes()
        with mock.patch.object(disk_mod.os, "replace", side_effect=AssertionError("programmer bug")), \
                self.assertRaisesRegex(AssertionError, "programmer bug"):
            self.cache.put(QUERY, [RECORD])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.directory.iterdir()), [path])
        cache = DiskCurrencyCache(self.directory, clock=mock.Mock(side_effect=AssertionError("clock bug")))
        for method, args in ((cache.get, (QUERY,)), (cache.get_any, (QUERY,)), (cache.put, (QUERY, [RECORD]))):
            with self.assertRaisesRegex(AssertionError, "clock bug"):
                method(*args)


if __name__ == "__main__":
    unittest.main()
