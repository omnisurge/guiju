"""Low 修复项回归测试：currency 域（L1-L8 + 上轮遗留）。

全部离线：不联网、不读真实凭据；用假 HttpClient / 打桩 urlopen。

覆盖：
  L1  裸 DB31（非 /T）进上海路由；DB31/… 仍被拒，不走 DB 外源
  L2a GUIJU_CURRENCY_CACHE_OFF=1 让 native_currency 不装缓存
  L2b（已删）原 mcp 端点的 cache 参数测试随 spec-currency 退役移除
  L2c 非 native 内层的 memo 命中原样重放五元组（不重查）
  L2d MisconfiguredCurrency hint 给出 native 配置形态
  L3  CachedCurrency._load 时钟回拨：>1h 未来 → age=None；1h 内漂移 → 钳 0
  L4  native._guard 只查取消，admit 不随守卫重复计费；lookup 恰好一次入口 admit
  L5  jtst：空结果页不告警、tr 存在却零可解析记录告警、pagination/close
  L6  csres/stdsamr/dbba 同型：警告规则、pagination、close、上下文管理器
  L7  存疑确认：guiju/store/currency.py 无 prune_expired，.tmp 不落库（原设计）
  L8  fallback 可选 admit 每 lookup 恰一次；缓存键不做规范化（分裂行为保留）
  R4A 34 no_currency_record 与 31 not_indexed 分家；NoCurrencySource 旧名兼容
  R4B post_form 放行由 Route(hosts=…, form_post=True) 驱动，_FORM_HOSTS 已删
"""

import json
import unittest
import warnings
from unittest import mock

import guiju.currency as currency_pkg
from guiju.currency import (
    CachedCurrency,
    MisconfiguredCurrency,
    _cache,
    native_currency,
)
from guiju.currency.cache import CACHE_NAMESPACE, DEFAULT_TTL, CacheEntry
from guiju.currency.csres import CsresCurrency, parse_csres
from guiju.currency.dbba import DbbaCurrency
from guiju.currency.fallback import FallbackCurrency
from guiju.currency.httpx import HttpClient
from guiju.currency.jtst import JtstCurrency, parse_jtst
from guiju.currency.model import QueryCancelled, Record
from guiju.currency.native import NativeCurrency, NoCurrencyRecord, NoCurrencySource
from guiju.currency.registry import Registry, Route, default_registry
from guiju.currency.shzjw import _valid_query, is_shanghai_code, normalize_code
from guiju.currency.stdsamr import StdSamrCurrency
from guiju.errors import ComplianceDenied, GuijuError, NotIndexed, SourceUnreachable

CODE = "GB 1-2000"
NOW = 10_000_000.0


def _record(code=CODE, source="p", status="current"):
    return Record(code, status, "标准", "2000-01-01", source, publish_date="1999-01-01", raw="证据")


class Stub:
    def __init__(self, name="p", rows=None, error=None):
        self.name, self.error = name, error
        self.rows = [] if rows is None else rows
        self.calls = 0

    def lookup(self, code):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.rows)


class MemoryCache:
    def __init__(self, clock, ttl=DEFAULT_TTL):
        self.clock, self.ttl = clock, ttl
        self.entries = {}
        self.calls = []
        self.fail = None

    def get(self, code, *, namespace=CACHE_NAMESPACE):
        self.calls.append("get")
        if self.fail:
            raise self.fail
        entry = self.entries.get((namespace, code))
        return entry if entry is not None and entry.fresh(self.ttl, self.clock()) else None

    def get_any(self, code, *, namespace=CACHE_NAMESPACE):
        self.calls.append("get_any")
        if self.fail:
            raise self.fail
        return self.entries.get((namespace, code))

    def put(self, code, records, *, namespace=CACHE_NAMESPACE):
        self.calls.append("put")
        if self.fail:
            raise self.fail
        self.entries[namespace, code] = CacheEntry(self.clock(), tuple(records))


def _make_native(now_holder, source=None, cache=None, **kwargs):
    source = source if source is not None else Stub(rows=[_record()])
    registry = Registry()
    registry.register(Route("p", ("GB",), lambda: source))
    cache = cache if cache is not None else MemoryCache(lambda: now_holder[0])
    native = NativeCurrency(registry, cache, clock=lambda: now_holder[0], **kwargs)
    namespace = registry.namespace_for(CODE, source)
    return native, source, cache, namespace


def _no_cache_native(now_holder, source=None, **kwargs):
    """真正无后端的 native（默认 cache=None，不再被 _make_native 塞 MemoryCache）。"""
    source = source if source is not None else Stub(rows=[_record()])
    registry = Registry()
    registry.register(Route("p", ("GB",), lambda: source))
    native = NativeCurrency(registry, clock=lambda: now_holder[0], **kwargs)
    namespace = registry.namespace_for(CODE, source)
    return native, source, namespace


class FakeStore:
    def __init__(self, hit=None):
        self.hit = hit
        self.saved = []

    def cached_currency(self, key):
        return self.hit

    def cache_currency(self, key, payload):
        self.saved.append((key, payload))




class FakeClient:
    def __init__(self, body=b""):
        self.body = body
        self.closed = False

    def get(self, url, *, accept=None):
        return self.body

    def post_form(self, url, data, *, accept=None):
        return self.body

    def close(self):
        self.closed = True


# ---------- L1：裸 DB31 进上海路由，DB31/… 仍被拒 ----------

class L1BareDb31Test(unittest.TestCase):
    def test_bare_db31_accepted_as_shanghai(self):
        for code in ("DB3108-2020", "DB31-08-2020", "DB3108", "DB31/T 08-2020", "DB31/T08-2020"):
            self.assertTrue(is_shanghai_code(code), code)
            self.assertTrue(_valid_query(normalize_code(code)), code)  # _valid_query 契约：吃规范化后的 key

    def test_db31_slash_forms_still_rejected(self):
        for code in ("DB31/373-2010", "DB31/373", "DB31/Z1", "DB31/TX1", "DB31X"):
            self.assertFalse(is_shanghai_code(code), code)

    def test_default_registry_routes_bare_db31_to_shzjw(self):
        registry = default_registry()
        adapter = registry.match("DB3108-2020")
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.name, "shzjw")

    def test_default_registry_db31_slash_matches_nothing(self):
        # DB31/… 被上海路由拒绝（code_filter）且裸 DB 路由也排除（owns_shanghai_namespace）：
        # 结论是无源可答，不能偷偷送去外源 DB。
        registry = default_registry()
        self.assertIsNone(registry.match("DB31/373"))


# ---------- L2a/L2b：CACHE_OFF=1 环境开关 ----------

class L2CacheOffTest(unittest.TestCase):
    def test_native_currency_cache_off_returns_no_cache_adapter(self):
        with mock.patch.dict(currency_pkg.os.environ, {"GUIJU_CURRENCY_CACHE_OFF": "1"}):
            adapter = native_currency()
        self.assertIsInstance(adapter, NativeCurrency)
        self.assertIsNone(adapter.cache)

    def test_native_currency_cache_off_outranks_explicit_dir(self):
        # 钉住文档语义：GUIJU_CURRENCY_CACHE_OFF=1 优先级最高，
        # 连 GUIJU_CURRENCY_CACHE=<目录> / 参数级目录都不能绕过它。
        with mock.patch.dict(currency_pkg.os.environ,
                             {"GUIJU_CURRENCY_CACHE_OFF": "1", "GUIJU_CURRENCY_CACHE": "/tmp/x"}):
            adapter = native_currency(cache_dir="/tmp/x")
        self.assertIsInstance(adapter, NativeCurrency)
        self.assertIsNone(adapter.cache)

    def test_native_currency_cache_off_zero_keeps_cache(self):
        with mock.patch.dict(currency_pkg.os.environ, {"GUIJU_CURRENCY_CACHE_OFF": "0", "GUIJU_HOME": "/tmp/guiju-test-home"}):
            adapter = native_currency()
        self.assertIsNotNone(adapter.cache)



# ---------- L2c：memo 命中原样重放五元组，不重查 ----------

class L2MemoReplayTest(unittest.TestCase):
    def setUp(self):
        _cache.clear()
        self.addCleanup(_cache.clear)

    def test_memo_hit_replays_original_tuple_and_skips_inner(self):
        inner = Stub(rows=[_record()])
        cached = CachedCurrency(inner)
        first = cached.lookup(CODE)
        self.assertFalse(cached.from_cache)
        self.assertFalse(cached.stale)
        self.assertIsNone(cached.upstream_error)
        second = cached.lookup(CODE)
        self.assertEqual(inner.calls, 1)          # memo 命中，不重查
        self.assertEqual(second, first)
        # 钉住原契约：重放的是首查时的原始五元组（from_cache 仍为 False）。
        self.assertFalse(cached.from_cache)
        self.assertFalse(cached.stale)

    def test_memo_hit_replays_stale_upstream_error_tuple(self):
        store = FakeStore(hit=('[{"code":"GB 1-2000","status":"current"}]', "2020-01-01 00:00:00"))
        inner = Stub(error=SourceUnreachable("down"))
        cached = CachedCurrency(inner, store=store)
        rows = cached.lookup(CODE)
        self.assertTrue(cached.from_cache)
        self.assertTrue(cached.stale)
        self.assertIsNotNone(cached.upstream_error)
        rows2 = cached.lookup(CODE)
        self.assertEqual(inner.calls, 1)
        self.assertEqual(rows2, rows)
        self.assertTrue(cached.stale)
        self.assertIs(cached.upstream_error.__class__, SourceUnreachable)


# ---------- L2d：hint 指明 native 用法 ----------

class L2MisconfigHintTest(unittest.TestCase):
    def test_hint_explains_native_forms(self):
        bad = MisconfiguredCurrency("cmd", "二进制不存在")
        try:
            bad.lookup(CODE)
        except SourceUnreachable as exc:
            hint = getattr(exc, "hint", "") or str(exc)
        else:
            self.fail("MisconfiguredCurrency.lookup 必须 SourceUnreachable（不静默换源）")
        self.assertIn("native", hint)
        self.assertIn("off", hint)


# ---------- L3：_load 时钟回拨年龄口径 ----------

class L3LoadClockRollbackTest(unittest.TestCase):
    def _load(self, fetched_at):
        store = FakeStore(hit=('[{"code":"GB 1-2000","status":"current"}]', fetched_at))
        cached = CachedCurrency(Stub(), store=store)
        return cached._load("GB 1-2000")

    def test_future_fetched_at_over_hour_returns_no_age(self):
        hit = self._load("2999-01-01 00:00:00")
        self.assertIsNotNone(hit)
        self.assertIsNone(hit[1])   # 年龄不可信 → None，不伪装"刚写入"

    def test_small_negative_skew_clamps_to_zero(self):
        from datetime import datetime, timedelta
        soon = datetime.now() + timedelta(minutes=5)
        hit = self._load(soon.strftime("%Y-%m-%d %H:%M:%S"))
        self.assertIsNotNone(hit)
        self.assertEqual(hit[1], 0.0)  # 1h 内漂移钳 0，不误报陈旧

    def test_past_timestamp_reports_positive_age(self):
        hit = self._load("2020-01-01 00:00:00")
        self.assertIsNotNone(hit)
        self.assertIsNotNone(hit[1])
        self.assertGreater(hit[1], 0)

    def test_malformed_timestamp_returns_no_age(self):
        hit = self._load("not-a-date")
        self.assertIsNotNone(hit)
        self.assertIsNone(hit[1])


# ---------- L4：native admit 只在入口/put 前，不随 guard 计费 ----------

class L4AdmitAccountingTest(unittest.TestCase):
    def setUp(self):
        self.now = [NOW]

    def test_lookup_charges_admit_at_entry_and_pre_put(self):
        # 钉住真实契约（native.py L143-144/175-176 注释）：
        # 上游未命中的一次 lookup 恰计 2 次 admit（入口 + put 前复验），不随 _guard 重复计费。
        admits = []
        native, _source, _cache, _ = _make_native(self.now, admit=lambda code: admits.append(code))
        native.lookup(CODE)
        self.assertEqual(admits, [CODE, CODE])
        native.lookup(CODE)  # 缓存命中：只入口 admit，没有 put 前那次
        self.assertEqual(admits, [CODE, CODE, CODE])

    def test_guard_itself_never_charges_admit(self):
        # _guard 被调用多次但只查 cancelled；已计费的 admit 不应再被守卫重放。
        guards = []
        admits = []
        def cancelled():
            return (guards.append(1), False)[1]
        native, _source, _, _ = _make_native(self.now, cancelled=cancelled,
                                            admit=lambda code: admits.append(code))
        native.lookup(CODE)
        self.assertGreater(len(guards), 2)   # 守卫确实跑了多次
        self.assertEqual(len(admits), 2)     # 但 admit 不随守卫线性膨胀

    def test_cancelled_query_never_reaches_admit(self):
        admits = []
        native, source, _, _ = _make_native(self.now, cancelled=lambda: True,
                                            admit=lambda code: admits.append(code))
        with self.assertRaises(QueryCancelled):
            native.lookup(CODE)
        self.assertEqual(admits, [])   # 已取消的查询不许占准入名额
        self.assertEqual(source.calls, 0)


# ---------- L5：jtst 空页证据告警 + pagination/close ----------

class L5JtstTest(unittest.TestCase):
    def test_empty_result_page_no_warning(self):
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            records = parse_jtst(b"<html><body><table></table></body></html>")
        self.assertEqual(records, [])
        self.assertFalse(any("jtst" in str(w.message) for w in seen))

    def test_rows_present_but_unparseable_warns(self):
        html = '<table><tr><td><span class="other">no code</span></td></tr></table>'
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            records = parse_jtst(html.encode())
        self.assertEqual(records, [])
        self.assertTrue(any("jtst" in str(w.message) and "0 条" in str(w.message) for w in seen))

    def test_pagination_honest_and_close(self):
        client = FakeClient(b"<table></table>")
        jtst = JtstCurrency(client)
        self.assertEqual(jtst.pagination, {"fetched_pages": 0, "total": None, "has_more": None})
        jtst.lookup("JTG 1")
        self.assertEqual(jtst.pagination["fetched_pages"], 1)
        self.assertIsNone(jtst.pagination["total"])      # HTML 页无总数：None 而非猜 0
        self.assertIsNone(jtst.pagination["has_more"])   # 无法判定不猜 False
        jtst.close()
        self.assertTrue(client.closed)

    def test_context_manager_closes(self):
        client = FakeClient(b"")
        with JtstCurrency(client):
            pass
        self.assertTrue(client.closed)


# ---------- L6：csres/stdsamr/dbba 同型修复 ----------

class L6CsresTest(unittest.TestCase):
    def test_data_rows_but_zero_records_warns(self):
        # parse_csres 按 GBK 解码：title 标记必须用 GBK 字节，utf-8 的"编号："会被解成乱码
        html = ('<table><tr title="编号：GB 1"><td>a</td><td>b</td><td>c</td></tr>'
                '<tr title="编号：GB 2"><td>a</td><td>b</td><td>c</td></tr></table>')
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            records = parse_csres(html.encode("gbk", "replace"))
        self.assertEqual(records, [])
        self.assertTrue(any("csres" in str(w.message) for w in seen))

    def test_structured_rows_without_marker_warns(self):
        html = "<table><tr><td>a</td><td>b</td><td>c</td><td>d</td></tr></table>"
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            records = parse_csres(html.encode())
        self.assertEqual(records, [])
        self.assertTrue(any("csres" in str(w.message) for w in seen))

    def test_truly_empty_page_no_warning(self):
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            records = parse_csres(b"<html><body></body></html>")
        self.assertEqual(records, [])
        self.assertFalse(any("csres" in str(w.message) for w in seen))

    def test_pagination_and_close(self):
        client = FakeClient(b"<html></html>")
        csres = CsresCurrency(client)
        csres.lookup("GB 1")
        self.assertEqual(csres.pagination["fetched_pages"], 1)
        self.assertIsNone(csres.pagination["has_more"])
        csres.close()
        self.assertTrue(client.closed)
        client2 = FakeClient(b"")
        with CsresCurrency(client2):
            pass
        self.assertTrue(client2.closed)


class L6StdSamrTest(unittest.TestCase):
    def test_total_exceeds_page_warns(self):
        payload = json.dumps({"total": 25, "rows": [
            {"C_STD_CODE": "GB 1-2000", "C_C_NAME": "标准", "STATE": "现行",
             "ISSUE_DATE": "2000-01-01", "ACT_DATE": "2000-06-01"}]})
        client = FakeClient(payload.encode())
        samr = StdSamrCurrency(client)
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            rows = samr.lookup("GB 1-2000")
        self.assertEqual(samr.pagination["total"], 25)
        self.assertTrue(samr.pagination["has_more"])
        self.assertTrue(any("std_samr" in str(w.message) for w in seen))
        self.assertEqual(len(rows), 1)

    def test_total_zero_no_warning(self):
        client = FakeClient(json.dumps({"total": 0, "rows": []}).encode())
        samr = StdSamrCurrency(client)
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            rows = samr.lookup("GB 9999")
        self.assertEqual(rows, [])
        self.assertFalse(any("std_samr" in str(w.message) for w in seen))

    def test_total_positive_but_empty_records_warns(self):
        client = FakeClient(json.dumps({"total": 3, "rows": []}).encode())
        samr = StdSamrCurrency(client)
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            samr.lookup("GB 1")
        self.assertTrue(any("0 条" in str(w.message) for w in seen))

    def test_close_and_context(self):
        client = FakeClient(b"")
        samr = StdSamrCurrency(client)
        samr.close()
        self.assertTrue(client.closed)


class L6DbbaTest(unittest.TestCase):
    def test_total_exceeds_page_warns(self):
        payload = json.dumps({"total": 30, "records": [
            {"code": "DB 1-2020", "chName": "标准", "status": "现行",
             "reviseStdCodes": "", "actDate": 1, "issueDate": 1, "fzDate": 0}]})
        client = FakeClient(payload.encode())
        dbba = DbbaCurrency(client)
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            rows = dbba.lookup("DB 1")
        self.assertEqual(dbba.pagination["total"], 30)
        self.assertTrue(dbba.pagination["has_more"])
        self.assertTrue(any("dbba" in str(w.message) for w in seen))
        self.assertEqual(len(rows), 1)

    def test_total_positive_empty_records_warns(self):
        client = FakeClient(json.dumps({"total": 2, "records": []}).encode())
        dbba = DbbaCurrency(client)
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            dbba.lookup("DB 9")
        self.assertTrue(any("0 条" in str(w.message) for w in seen))

    def test_no_json_accept_header_and_close(self):
        seen_accept = []

        class C(FakeClient):
            def post_form(self, url, data, *, accept=None):
                seen_accept.append(accept)
                return json.dumps({"total": 0, "records": []}).encode()

        dbba = DbbaCurrency(C())
        dbba.lookup("DB 1")
        self.assertEqual(seen_accept, [None])  # dbba 禁带 Accept: application/json
        dbba.close()
        self.assertTrue(dbba.client.closed)


# ---------- L7：prune_expired 幽灵确认（防回引） ----------

class L7NoPruneExpiredTest(unittest.TestCase):
    def test_store_layer_has_no_prune_expired(self):
        import guiju.store.currency as store_currency
        self.assertFalse(hasattr(store_currency, "prune_expired"))


# ---------- L8：fallback admit 每 lookup 一次；缓存键不规范化 ----------

class L8FallbackAdmitTest(unittest.TestCase):
    def test_admit_called_once_per_lookup(self):
        admits = []
        fb = FallbackCurrency("x", Stub(name="p", rows=[_record()]),
                              Stub(name="b", rows=[_record()]),
                              admit=lambda code: admits.append(code))
        fb.lookup(CODE)
        fb.lookup(CODE)
        self.assertEqual(admits, [CODE, CODE])

    def test_admit_called_before_backup_too(self):
        admits = []
        fb = FallbackCurrency("x", Stub(error=SourceUnreachable("down")),
                              Stub(name="b", rows=[_record()]),
                              admit=lambda code: admits.append(code))
        rows = fb.lookup(CODE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(admits, [CODE])

    def test_cancelled_lookup_skips_admit(self):
        admits = []
        fb = FallbackCurrency("x", Stub(), cancelled=lambda: True,
                              admit=lambda code: admits.append(code))
        with self.assertRaises(QueryCancelled):
            fb.lookup(CODE)
        self.assertEqual(admits, [])

    def test_default_registry_accepts_admit_without_wiring(self):
        admits = []
        registry = default_registry(admit=lambda code: admits.append(code))
        adapter = registry.match(CODE)
        self.assertIsNotNone(adapter)          # 能构建，不要求真实 admit 去向
        self.assertIsNone(registry.match("XX 999"))


class L8CacheKeyNoNormalizationTest(unittest.TestCase):
    def setUp(self):
        _cache.clear()
        self.addCleanup(_cache.clear)

    def test_raw_code_addressing_keeps_variants_distinct(self):
        # 钉住原契约：memo/缓存键用原样编号，'GB 1-2000' 与 'gb1-2000' 是两个键。
        inner = Stub(rows=[_record()])
        cached = CachedCurrency(inner)
        cached.lookup("GB 1-2000")
        cached.lookup("gb1-2000")
        self.assertEqual(inner.calls, 2)   # 第二个写法不命中第一个的 memo

    def test_memory_cache_layer_sees_raw_code(self):
        cache = MemoryCache(lambda: NOW)
        native, _source, _, _ = _make_native([NOW], cache=cache)
        native.lookup("GB 1-2000")
        keys = [code for (_ns, code) in cache.entries]
        self.assertEqual(keys, ["GB 1-2000"])  # 原样落键，未大写/去空格


class R4NoCurrencyRecordChainTest(unittest.TestCase):
    """34 no_currency_record 是 GuijuError 直属分支，不再挂在 31 not_indexed 下；
    NoCurrencySource 旧类名保留，assertRaises 继续生效。"""

    def test_chain(self):
        self.assertTrue(issubclass(NoCurrencyRecord, GuijuError))
        self.assertTrue(issubclass(NoCurrencySource, NoCurrencyRecord))
        self.assertFalse(issubclass(NoCurrencyRecord, NotIndexed))  # 34 与 31 分家

    def test_exit_and_reason(self):
        self.assertEqual(NoCurrencyRecord.exit, 34)
        self.assertEqual(NoCurrencySource.exit, 34)
        self.assertEqual(NoCurrencyRecord.reason, "no_currency_record")
        self.assertNotEqual(NoCurrencyRecord.exit, NotIndexed.exit)

    def test_raised_from_unrouted_code_and_still_catchable_by_old_name(self):
        cache = mock.Mock()
        with self.assertRaises(NoCurrencySource) as ctx:   # 旧名兼容路径
            NativeCurrency(Registry(), cache).query("XX 999")
        self.assertEqual(ctx.exception.exit, 34)
        cache.get.assert_not_called()
        cache.get_any.assert_not_called()
        with self.assertRaises(NoCurrencyRecord):          # 新基类同样可捕
            NativeCurrency(Registry()).query("XX 999")


class R4FormPostRegistryDrivenTest(unittest.TestCase):
    """_FORM_HOSTS 常量已删：放行与否只看 Route(hosts=…, form_post=True)。"""

    def test_default_registry_flags_ccsn_and_dbba_only(self):
        reg = default_registry()
        self.assertTrue(reg.form_post_for_host("www.ccsn.org.cn"))
        self.assertTrue(reg.form_post_for_host("dbba.sacinfo.org.cn"))
        for host in ("www.csres.com", "std.samr.gov.cn", "jtst.mot.gov.cn",
                     "bzjg.zjw.sh.gov.cn", "unlisted.example.org"):
            with self.subTest(host=host):
                self.assertFalse(reg.form_post_for_host(host))

    def test_injected_route_with_form_post_is_honored(self):
        # 判定由 Route 数据驱动而非写死：手造一条 form_post=True 的 Route 即被采信。
        reg = Registry()
        reg.register(Route("x", (), None, hosts=("www.csres.com",), form_post=True))
        self.assertTrue(reg.form_post_for_host("www.csres.com"))
        self.assertFalse(reg.form_post_for_host("other.example.org"))
        reg2 = Registry()
        reg2.register(Route("y", (), None, hosts=("www.csres.com",)))  # form_post 缺省 False
        self.assertFalse(reg2.form_post_for_host("www.csres.com"))

    def test_registered_form_host_passes_gate_and_posts(self):
        client = HttpClient()
        with mock.patch.object(client, "_request", return_value=b"{}") as req:
            client.post_form("https://www.ccsn.org.cn/newweb/api/StandardQuery.assx/queryStandard",
                             {"key": "GB 50011"})
        self.assertEqual(req.call_args.kwargs["method"], "POST")

    def test_non_form_host_keeps_original_deny_and_get_unaffected(self):
        client = HttpClient()
        # 非 form_post 端点：仍走拒发分支（GET 行为不变，不打上游即可验证）
        with self.assertRaises(ComplianceDenied):
            client.post_form("https://www.csres.com/s.jsp", {"key": "GB 50011"})
        with self.assertRaises(ComplianceDenied):
            client.post_form("https://bzjg.zjw.sh.gov.cn/JGBStandardWebApi/external/localStandard/list",
                             {"key": "DG/TJ08-2007"})

    def test_endpoint_routes_do_not_steal_code_matches(self):
        # 端点 Route 的 prefixes=()，不得让任何编号匹配到它（factory=None 否则 match 会崩）。
        reg = default_registry()
        for code in ("GB50011", "DB44/T792", "CJJ1-2008"):
            with self.subTest(code=code):
                adapter = reg.match(code)
                self.assertIsNotNone(adapter)
                self.assertNotIn(adapter.name if hasattr(adapter, "name") else "", ("ccsn", "dbba"))
        self.assertIsNone(reg.match("XX 999"))


if __name__ == "__main__":
    unittest.main()
