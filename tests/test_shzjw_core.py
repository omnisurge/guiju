"""唯一上海解析核心的离线契约测试。

本文件的行与响应均为手构数据，不是上游实测样本或 golden；所有 fetch/HTTP 都用 mock。
"""

import asyncio
import copy
import json
import unittest
from unittest import mock

from guiju.domain import shzjw as core
from guiju.errors import ComplianceDenied, NeedsHuman, SourceUnreachable
from guiju.sources import shzjw as source


def row(number=1, **fields):
    """手构标准行，仅用于解析与分页边界。"""
    return {
        "standardNumber": f"DG/TJ08-{number}-2026",
        "standardProjectName": f"手构标准{number}",
        "standardFileId": f"fake-file-{number}",
        **fields,
    }


def page(rows, total=None):
    return {"data": {"list": rows, "total": len(rows) if total is None else total}}


class RecordTest(unittest.TestCase):
    def test_all_legacy_fields_without_context(self):
        raw = row(
            2007,
            standardNumber=" DG/TJ08-2007-2026 ",
            standardProjectName=" 手构标准 ",
            releaseTime="2026-08-10 00:00:00",
            implementationTime="2027-02-01 00:00:00",
            abolishTime=None,
            editorUnitName=" 手构单位 ",
        )
        before = raw.copy()
        self.assertEqual(
            core.record(raw),
            {
                "code": "DG/TJ08-2007-2026",
                "code_key": "DG/TJ08-2007-2026",
                "name": "手构标准",
                "status": "current",
                "kind": "standard",
                "published_on": "2026-08-10",
                "implement_on": "2027-02-01",
                "abolish_on": None,
                "file_id": "fake-file-2007",
                "download_url": f"{core.BASE}api/files/webDownload?fileId=fake-file-2007",
                "page_url": f"{core.PAGE}?keyword=DG/TJ08-2007-2026",
                "editor": "手构单位",
            },
        )
        self.assertEqual(raw, before)

    def test_no_context_keeps_old_empty_defaults_and_unsigned_link(self):
        rec = core.record({})
        self.assertEqual(rec["code"], "")
        self.assertIsNone(rec["code_key"])
        self.assertEqual(rec["name"], "")
        self.assertEqual(rec["status"], "current")
        self.assertEqual(rec["kind"], "standard")
        self.assertEqual(rec["published_on"], "")
        self.assertEqual(rec["implement_on"], "")
        self.assertIsNone(rec["abolish_on"])
        self.assertIsNone(rec["file_id"])
        self.assertIsNone(rec["download_url"])
        self.assertEqual(rec["page_url"], f"{core.PAGE}?keyword=")
        self.assertEqual(rec["editor"], "")
        self.assertEqual(core.download_url("legacy+id"), f"{core.BASE}api/files/webDownload?fileId=legacy+id")

    def test_name_only_link_and_official_code_are_not_reinterpreted(self):
        rec = core.record({"standardProjectName": " 名称 "})
        self.assertEqual(rec["page_url"], f"{core.PAGE}?keyword=%E5%90%8D%E7%A7%B0")
        rec = core.record(row(standardNumber="dg/tj 08-2007—2026"))
        self.assertEqual(rec["code"], "dg/tj 08-2007—2026")
        self.assertEqual(rec["code_key"], "DG/TJ08-2007-2026")

    def test_normalize_code_is_shared_by_record_exact_and_rank(self):
        official = "DG/TJ08−2007−2026"
        current = core.record(row(standardNumber=official))
        old = core.record(row(standardNumber="DG/TJ08-2007-2026"), abolished=True)
        self.assertEqual(current["code"], official)
        self.assertEqual(current["code_key"], "DG/TJ08-2007-2026")
        for ref in (official, "dg/tj 08-2007-2026", "DG/TJ08—2007－2026"):
            with self.subTest(ref=ref):
                self.assertEqual(core.normalize_code(ref), "DG/TJ08-2007-2026")
                self.assertEqual(core._exact([current], ref), [current])
                self.assertEqual(core._rank([old, current], ref), [current])
        external_key = {**old, "code_key": official}
        self.assertEqual(core._rank([external_key, current], official), [current])
        self.assertEqual(core.normalize_code("DG/TJ08-2007"), "DG/TJ08-2007")
        self.assertNotEqual(core.normalize_code("DG/TJ08-9999"), core.normalize_code("DG/TJ08-2007"))

    def test_column_abolition_cannot_be_overridden_by_missing_or_false_row(self):
        for fields in ({}, {"isAbolish": False}, {"isAbolish": None, "abolishTime": None}):
            with self.subTest(fields=fields):
                rec = core.record(row(**fields), abolished=True)
                self.assertEqual(rec["status"], "withdrawn")
                self.assertIsNone(rec["abolish_on"])

    def test_explicit_abolition_evidence_survives_current_column(self):
        for fields in (
            {"isAbolish": True},
            {"isAbolish": False, "abolishTime": "2026-01-01 00:00:00"},
        ):
            for context in (None, False, True):
                with self.subTest(fields=fields, context=context):
                    rec = core.record(row(**fields), abolished=context)
                    self.assertEqual(rec["status"], "withdrawn")

    def test_design_column_only_fills_absent_or_null_type(self):
        for fields in ({}, {"standardType": None}, {"standardType": 1}, {"standardType": "1"}):
            with self.subTest(fields=fields):
                self.assertEqual(core.record(row(**fields), standard_type=1)["kind"], "design")
        for value in (0, "0"):
            self.assertEqual(core.record(row(standardType=value), standard_type=1)["kind"], "standard")
        for value in (1, "1"):
            self.assertEqual(core.record(row(standardType=value), standard_type=0)["kind"], "design")
            self.assertEqual(core.record(row(standardType=value))["kind"], "design")

    def test_reported_official_string_type_shape_keeps_future_implementation_current(self):
        # 复现 Lead 反馈的官方 list 字段形状；此处仍是手构投影，不是本测试联网采样。
        raw = row(
            2007, standardType="0", releaseTime="2026-08-10 00:00:00",
            implementationTime="2027-02-01 00:00:00", abolishTime=None,
        )
        records, total = core.parse_page({"success": True, "code": 200, "message": "手构", "data": {"list": [raw], "total": 1}})
        self.assertEqual(total, 1)
        self.assertEqual(records[0]["kind"], "standard")
        self.assertEqual(records[0]["status"], "current")
        self.assertEqual(records[0]["published_on"], "2026-08-10")
        self.assertEqual(records[0]["implement_on"], "2027-02-01")


class ParsePageTest(unittest.TestCase):
    def assert_bad(self, payload):
        with self.assertRaises(NeedsHuman) as caught:
            core.parse_page(payload)
        self.assertEqual(caught.exception.exit, 50)

    def test_valid_empty_is_not_a_parse_failure(self):
        self.assertEqual(core.parse_page(page([])), ([], 0))

    def test_success_when_present_requires_boolean_without_coercion(self):
        for value in (None, 0, 1, 0.0, 1.0, "true", "false", "0", "1", [], {}):
            with self.subTest(success=value):
                self.assert_bad({"success": value, **page([])})
        self.assertEqual(core.parse_page({"success": True, **page([])}), ([], 0))
        self.assertEqual(core.parse_page(page([])), ([], 0))

    def test_explicit_failure_is_not_empty_or_nonempty_success(self):
        for rows in ([], [row()]):
            with self.subTest(rows=rows):
                self.assert_bad({"success": False, **page(rows)})

    def test_context_and_no_payload_mutation(self):
        payload = page([row(isAbolish=False), row(2, standardType=0)])
        before = copy.deepcopy(payload)
        records, total = core.parse_page(payload, abolished=True, standard_type=1)
        self.assertEqual(total, 2)
        self.assertEqual([rec["status"] for rec in records], ["withdrawn", "withdrawn"])
        self.assertEqual([rec["kind"] for rec in records], ["design", "standard"])
        self.assertEqual(payload, before)

    def test_envelope_data_list_and_total_are_required_and_strict(self):
        invalid = [None, False, [], "", {}, {"data": None}, {"data": []}, {"data": ""}]
        invalid.extend({"data": data} for data in ({}, {"list": []}, {"total": 0}))
        invalid.extend(page_data for page_data in (
            {"data": {"list": value, "total": 0}} for value in (None, {}, "", False)
        ))
        invalid.extend({"data": {"list": [], "total": value}} for value in (None, "0", 0.0, True, False, -1))
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assert_bad(payload)

    def test_rows_require_object_and_some_identifier(self):
        for invalid in (None, False, [], "", {}, {"standardNumber": None, "standardProjectName": " "}):
            with self.subTest(row=invalid):
                self.assert_bad(page([invalid]))
        self.assertEqual(core.parse_page(page([{"standardProjectName": "名称"}]))[0][0]["name"], "名称")

    def test_every_used_text_field_rejects_non_text_non_null(self):
        for key in (
            "standardNumber", "standardProjectName", "releaseTime", "implementationTime",
            "abolishTime", "standardFileId", "editorUnitName",
        ):
            for value in (False, 0, 1, [], {}):
                with self.subTest(key=key, value=value):
                    self.assert_bad(page([row(**{key: value})]))
            with self.subTest(key=key, value=None):
                core.parse_page(page([row(**{key: None})]))

    def test_boolean_and_type_fields_do_not_accept_truthy_strings_or_integers(self):
        for value in ("true", "false", 0, 1, [], {}):
            with self.subTest(field="isAbolish", value=value):
                self.assert_bad(page([row(isAbolish=value)]))
        for value in (True, False, "01", " 1", "design", 1.0, -1, 2, [], {}):
            with self.subTest(field="standardType", value=value):
                self.assert_bad(page([row(standardType=value)]))

    def test_total_cannot_be_less_than_rows(self):
        self.assert_bad(page([row()], total=0))


class SearchTest(unittest.TestCase):
    def test_default_remains_single_page_even_when_total_is_larger(self):
        fetch = mock.Mock(return_value=page([row()], total=100))
        records = core.search("手构关键词", fetch=fetch, abolished=True, standard_type=1)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "withdrawn")
        self.assertEqual(records[0]["kind"], "design")
        fetch.assert_called_once_with({
            "pageNum": 1, "pageSize": 30, "isAbolish": True, "standardType": 1,
            "condition": "手构关键词", "publishStartDate": "", "publishEndDate": "",
            "implementStartDate": "", "implementEndDate": "",
        })

    def test_complete_mode_visits_all_pages_sequentially_with_same_four_column_fields(self):
        pages = [page([row(1), row(2)], 5), page([row(3), row(4)], 5), page([row(5)], 5)]
        calls = []

        def fetch(query):
            self.assertEqual(query["pageNum"], len(calls) + 1)
            calls.append(query.copy())
            return pages[query["pageNum"] - 1]

        with mock.patch.object(core, "parse_page", wraps=core.parse_page) as parse:
            records = core.search(
                "手构", fetch=fetch, all_pages=True, page_size=2, abolished=True, standard_type=1
            )
        self.assertEqual([rec["file_id"] for rec in records], [f"fake-file-{i}" for i in range(1, 6)])
        self.assertEqual(parse.call_count, 3)
        self.assertTrue(all(rec["status"] == "withdrawn" and rec["kind"] == "design" for rec in records))
        first = calls[0]
        for number, query in enumerate(calls, 1):
            self.assertEqual(query, {**first, "pageNum": number})

    def test_same_code_with_different_metadata_is_not_a_repeated_page(self):
        current = row(1)
        withdrawn = row(1, isAbolish=True, standardFileId="different-file")
        fetch = mock.Mock(side_effect=[page([withdrawn], 2), page([current], 2)])
        records = core.search("手构", fetch=fetch, page_size=1, all_pages=True)
        self.assertEqual([rec["status"] for rec in records], ["withdrawn", "current"])
        self.assertEqual(core._rank(records, "DG/TJ08-1-2026"), [records[1]])

    def test_empty_complete_query_fetches_once(self):
        fetch = mock.Mock(return_value=page([]))
        self.assertEqual(core.search("无命中", fetch=fetch, all_pages=True), [])
        fetch.assert_called_once()

    def test_premature_empty_short_oversized_and_repeated_pages_are_50_not_partial_hits(self):
        cases = {
            "empty_first": [page([], 3)],
            "short_first": [page([row(1)], 3)],
            "oversized_first": [page([row(1), row(2), row(3)], 3)],
            "empty_last": [page([row(1), row(2)], 3), page([], 3)],
            "short_middle": [page([row(1), row(2)], 5), page([row(3)], 5)],
            "oversized_last": [page([row(1), row(2)], 3), page([row(3), row(4)], 3)],
            "repeat_page": [page([row(1), row(2)], 4), page([row(1), row(2)], 4)],
            "reordered_repeat_page": [page([row(1), row(2)], 4), page([row(2), row(1)], 4)],
            "overlap_page": [page([row(1), row(2)], 4), page([row(2), row(3)], 4)],
            "repeat_inside_page": [page([row(1), row(1)], 2)],
        }
        for name, pages in cases.items():
            with self.subTest(case=name):
                fetch = mock.Mock(side_effect=pages)
                with self.assertRaises(NeedsHuman) as caught:
                    core.search("手构", fetch=fetch, page_size=2, all_pages=True)
                self.assertEqual(caught.exception.exit, 50)
                self.assertEqual(fetch.call_count, len(pages))

    def test_total_drift_in_either_direction_or_to_zero_is_50(self):
        for last in (page([row(3), row(4)], 5), page([row(3)], 3), page([], 0)):
            with self.subTest(last=last):
                fetch = mock.Mock(side_effect=[page([row(1), row(2)], 4), last])
                with self.assertRaises(NeedsHuman) as caught:
                    core.search("手构", fetch=fetch, page_size=2, all_pages=True)
                self.assertEqual(caught.exception.exit, 50)
                self.assertEqual(fetch.call_count, 2)

    def test_page_cap_refuses_truncated_search_but_accepts_exact_boundary(self):
        with mock.patch.object(core, "MAX_PAGES", 2):
            fetch = mock.Mock(return_value=page([row(1), row(2)], 5))
            with self.assertRaises(NeedsHuman):
                core.search("手构", fetch=fetch, page_size=2, all_pages=True)
            fetch.assert_called_once()
            fetch = mock.Mock(side_effect=[page([row(1), row(2)], 4), page([row(3), row(4)], 4)])
            self.assertEqual(len(core.search("手构", fetch=fetch, page_size=2, all_pages=True)), 4)
            self.assertEqual(fetch.call_count, 2)

    def test_invalid_page_size_does_not_fetch(self):
        for size in (0, -1, False, True, "2", 1.5):
            with self.subTest(size=size):
                fetch = mock.Mock(side_effect=AssertionError("不应调用传输"))
                with self.assertRaises(NeedsHuman):
                    core.search("手构", fetch=fetch, page_size=size, all_pages=True)
                fetch.assert_not_called()

    def test_invalid_later_page_is_not_a_successful_partial_result(self):
        for last in ({"data": {"list": []}}, {"success": False, **page([row(2)], 2)}):
            with self.subTest(last=last):
                fetch = mock.Mock(side_effect=[page([row(1)], 2), last])
                with self.assertRaises(NeedsHuman):
                    core.search("手构", fetch=fetch, page_size=1, all_pages=True)
                self.assertEqual(fetch.call_count, 2)

    def test_transport_failures_compliance_cancellation_and_programmer_errors_propagate(self):
        errors = (
            SourceUnreachable("离线"), NeedsHuman("结构错误"), ComplianceDenied("禁止"),
            KeyboardInterrupt(), asyncio.CancelledError(), AssertionError("程序错误"),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                fetch = mock.Mock(side_effect=error)
                with self.assertRaises(type(error)) as caught:
                    core.search("手构", fetch=fetch, all_pages=True)
                self.assertIs(caught.exception, error)
                fetch.assert_called_once()


class FindTest(unittest.TestCase):
    def test_default_exact_preference_keeps_legacy_bare_over_year_selection(self):
        base = "DG/TJ08-2007"
        current = core.record(row(2007))
        bare = core.record(row(2007, standardNumber=base), abolished=True)
        for options in ({}, {"prefer_exact": True}):
            with self.subTest(options=options):
                searcher = mock.Mock(side_effect=[[current], [], [bare], []])
                self.assertEqual(core.find(base, searcher=searcher, limit=None, **options), [bare])
                self.assertEqual(core._rank([current, bare], base, limit=None, **options), [bare])
                self.assertEqual(searcher.call_count, 4)

    def test_non_exact_preference_keeps_bare_withdrawn_and_current_year(self):
        base = "DG/TJ08-2007"
        current = core.record(row(2007))
        bare = core.record(row(2007, standardNumber=base), abolished=True)
        searcher = mock.Mock(side_effect=[[current], [], [bare], []])
        with mock.patch.object(core, "_exact", side_effect=AssertionError("不能只选裸基础号")):
            self.assertEqual(
                core.find(base, searcher=searcher, limit=None, prefer_exact=False), [current, bare]
            )
            self.assertEqual(
                core._rank([current, bare], base, limit=None, prefer_exact=False), [current, bare]
            )
        self.assertEqual(searcher.call_args_list, [
            mock.call(base, standard_type=0),
            mock.call(base, standard_type=1),
            mock.call(base, abolished=True, standard_type=0),
            mock.call(base, abolished=True, standard_type=1),
        ])

    def test_non_exact_preference_visits_four_columns_after_current_bare_exact(self):
        base = "DG/TJ08-2007"
        bare = core.record(row(2007, standardNumber=base))
        current = core.record(row(2007))
        old_bare = core.record(row(2007, standardNumber=base), abolished=True)
        old_year = core.record(row(2007, standardNumber=base + "-1999"), abolished=True)
        searcher = mock.Mock(side_effect=[[bare], [current], [old_bare, old_year], []])
        self.assertEqual(
            core.find(base, searcher=searcher, limit=None, prefer_exact=False), [bare, current, old_year]
        )
        self.assertEqual(searcher.call_count, 4)
        searcher = mock.Mock(side_effect=[[bare], AssertionError("默认仍应在裸号精确命中时停止")])
        self.assertEqual(core.find(base, searcher=searcher), [bare])
        searcher.assert_called_once_with(base, standard_type=0)

    def test_non_exact_preference_still_deduplicates_same_code_current_first(self):
        base = "DG/TJ08-2007"
        old = core.record(row(2007), abolished=True)
        current = core.record(row(2007))
        bare = core.record(row(2007, standardNumber=base), abolished=True)
        for records in ([old, current, bare], [current, old, bare]):
            with self.subTest(records=records):
                self.assertEqual(
                    core._rank(records, base, limit=None, prefer_exact=False), [current, bare]
                )

    def test_non_exact_preference_respects_none_and_explicit_result_limits(self):
        base = "DG/TJ08-2007"
        records = [core.record(row(2007, standardNumber=base))]
        records.extend(core.record(row(2007, standardNumber=f"{base}-{year}")) for year in range(2000, 2012))
        for limit, expected in ((None, records), (3, records[:3]), (10, records[:10])):
            with self.subTest(limit=limit):
                searcher = mock.Mock(side_effect=[records, [], [], []])
                self.assertEqual(
                    core.find(base, searcher=searcher, limit=limit, prefer_exact=False), expected
                )
                self.assertEqual(searcher.call_count, 4)

    def test_current_exact_short_circuits_other_columns(self):
        rec = core.record(row())
        searcher = mock.Mock(return_value=[rec])
        self.assertEqual(core.find("dg/tj 08-1-2026", searcher=searcher), [rec])
        searcher.assert_called_once_with("dg/tj 08-1-2026", standard_type=0)

    def test_design_ref_starts_in_design_column(self):
        rec = core.record(row(standardNumber="DBJT08-144-2026", standardType=1))
        searcher = mock.Mock(return_value=[rec])
        self.assertEqual(core.find(rec["code"], searcher=searcher), [rec])
        searcher.assert_called_once_with(rec["code"], standard_type=1)

    def test_no_exact_hit_preserves_all_four_column_query_order(self):
        for ref, types in (("无命中", [0, 1]), (" DBJT08-999 ", [1, 0])):
            with self.subTest(ref=ref):
                searcher = mock.Mock(return_value=[])
                self.assertEqual(core.find(ref, searcher=searcher), [])
                self.assertEqual(searcher.call_args_list, [
                    mock.call(ref, standard_type=types[0]),
                    mock.call(ref, standard_type=types[1]),
                    mock.call(ref, abolished=True, standard_type=types[0]),
                    mock.call(ref, abolished=True, standard_type=types[1]),
                ])

    def test_withdrawn_exact_beats_fuzzy_current_without_guessing_other_code(self):
        fuzzy = core.record(row(2))
        exact = core.record(row(1), abolished=True)
        searcher = mock.Mock(side_effect=[[fuzzy], [], [exact], []])
        self.assertEqual(core.find(exact["code"], searcher=searcher), [exact])
        self.assertEqual(searcher.call_count, 4)

    def test_rank_keeps_current_over_withdrawn_duplicates_and_code_over_name(self):
        old = core.record(row(1), abolished=True)
        current = core.record(row(1))
        named = core.record(row(2, standardProjectName=current["code"]))
        for hits in ([old, current, named], [current, old, named]):
            with self.subTest(hits=hits):
                self.assertEqual(core._rank(hits, current["code"]), [current])
        named_only = core.record({"standardProjectName": "名称"})
        self.assertEqual(core._rank([named_only, named_only], "其它"), [named_only])

    def test_find_limit_none_keeps_more_than_ten_exact_name_matches(self):
        records = [core.record(row(i, standardProjectName="同名手构标准")) for i in range(1, 14)]
        searcher = mock.Mock(return_value=records)
        self.assertEqual(core.find("同名手构标准", searcher=searcher), records[:10])
        self.assertEqual(core.find("同名手构标准", searcher=searcher, limit=None), records)
        self.assertEqual(core.find("同名手构标准", searcher=searcher, limit=3), records[:3])
        self.assertEqual(core._rank(records, "模糊名称", limit=None), records)


class SourceCompatibilityTest(unittest.TestCase):
    def test_old_aliases_and_source_identity_share_the_core(self):
        for alias, target in (
            ("_record", "record"), ("download_url", "download_url"), ("_exact", "_exact"),
            ("_rank", "_rank"), ("_design_first", "_design_first"),
        ):
            with self.subTest(alias=alias):
                self.assertIs(getattr(source, alias), getattr(core, target))
        for name in ("BASE", "PAGE", "REFERER", "QUERY_URL"):
            self.assertEqual(getattr(source, name), getattr(core, name))
        self.assertEqual(source.SOURCE.id, "shzjw")
        self.assertTrue(source.SOURCE.api_mode)
        for name in ("search", "find", "catalog_page", "download_to", "download"):
            self.assertIs(getattr(source.SOURCE, name), getattr(source, name))

    def test_content_search_injects_compliance_wrapper_and_uses_shared_parser(self):
        payload = page([row()], total=100)
        for abolished in (False, True):
            for standard_type in (0, 1):
                with (
                    self.subTest(abolished=abolished, standard_type=standard_type),
                    mock.patch.object(source.compliance, "post_json", return_value=payload) as post,
                    mock.patch.object(core, "search", wraps=core.search) as search,
                    mock.patch.object(core, "parse_page", wraps=core.parse_page) as parse,
                ):
                    records = source.search("手构", page_size=7, abolished=abolished, standard_type=standard_type)
                    search.assert_called_once_with(
                        "手构", fetch=source._fetch, page_size=7, abolished=abolished, standard_type=standard_type
                    )
                    parse.assert_called_once_with(payload, abolished=abolished, standard_type=standard_type)
                    post.assert_called_once_with(core.QUERY_URL, {
                        "pageNum": 1, "pageSize": 7, "isAbolish": abolished, "standardType": standard_type,
                        "condition": "手构", "publishStartDate": "", "publishEndDate": "",
                        "implementStartDate": "", "implementEndDate": "",
                    }, referer=core.REFERER)
                    self.assertEqual(len(records), 1)

    def test_catalog_reuses_same_page_parser_and_preserves_projection(self):
        payload = page([row(releaseTime="2026-01-01 00:00:00", standardProjectName=None)], total=100)
        with (
            mock.patch.object(source.compliance, "post_json", return_value=payload) as post,
            mock.patch.object(core, "parse_page", wraps=core.parse_page) as parse,
        ):
            items, total = source.catalog_page(2)
        parse.assert_called_once_with(payload)
        self.assertEqual(total, 100)
        self.assertEqual(items, [{
            "date": "2026-01-01", "url": f"{core.PAGE}?keyword=DG/TJ08-1-2026", "title": "DG/TJ08-1-2026",
        }])
        post.assert_called_once_with(core.QUERY_URL, {
            "pageNum": 2, "pageSize": 100, "isAbolish": False, "standardType": 0,
            "condition": "", "publishStartDate": "", "publishEndDate": "",
            "implementStartDate": "", "implementEndDate": "",
        }, referer=core.REFERER)

    def test_content_find_preserves_default_exact_preference_for_bare_code(self):
        base = "DG/TJ08-2007"
        bare = core.record(row(2007, standardNumber=base))
        current = core.record(row(2007))
        with mock.patch.object(source, "search", return_value=[bare, current]) as search:
            self.assertEqual(source.find(base), [bare])
        search.assert_called_once_with(base, standard_type=0)

    def test_patch_module_search_still_controls_find_and_source_find(self):
        rec = core.record(row())
        for find in (source.find, source.SOURCE.find):
            with (
                self.subTest(find=find),
                mock.patch.object(source, "search", return_value=[rec]) as search,
                mock.patch.object(source.compliance, "post_json", side_effect=AssertionError("不可联网")),
            ):
                self.assertEqual(find(rec["code"]), [rec])
                search.assert_called_once_with(rec["code"], standard_type=0)

    def test_search_and_catalog_parse_errors_are_50_not_empty_success(self):
        for payload in (None, {}, {"data": {"list": [], "total": "0"}}, {"success": False, **page([])}):
            for query in (lambda: source.search("手构"), lambda: source.catalog_page(1)):
                with (
                    self.subTest(payload=payload, query=query),
                    mock.patch.object(source.compliance, "post_json", return_value=payload),
                    self.assertRaises(NeedsHuman) as caught,
                ):
                    query()
                self.assertEqual(caught.exception.exit, 50)

    def test_search_and_catalog_invalid_json_are_50(self):
        for query in (lambda: source.search("手构"), lambda: source.catalog_page(1)):
            with (
                self.subTest(query=query),
                mock.patch.object(source.compliance, "post_json", side_effect=json.JSONDecodeError("坏JSON", "", 0)),
                self.assertRaises(NeedsHuman) as caught,
            ):
                query()
            self.assertEqual(caught.exception.exit, 50)

    def test_source_queries_keep_empty_success_and_never_enter_download_code(self):
        with (
            mock.patch.object(source.compliance, "post_json", return_value=page([])),
            mock.patch.object(source.compliance, "download", side_effect=AssertionError("不能下载")),
            mock.patch.object(source, "_token", side_effect=AssertionError("不能签名")),
            mock.patch.object(source, "download_to", side_effect=AssertionError("不能下载")),
            mock.patch.object(source, "download", side_effect=AssertionError("不能下载")),
        ):
            self.assertEqual(source.search("无命中"), [])
            self.assertEqual(source.find("无命中"), [])
            self.assertEqual(source.catalog_page(1), ([], 0))


if __name__ == "__main__":
    unittest.main()
