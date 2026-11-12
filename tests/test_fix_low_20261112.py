"""Lead round-3 Low 批的回归测试：每个条目对应一个独立小修。

覆盖：
  1. attachment._is_pdf 不再把名字里带 "pdf" 的 zip/doc 当 PDF
  2. codes 编号正则支持 1 位序号（CJJ 1-1990）
  3. search 负 --limit 按 bad_usage(2) 拒绝而非静默截尾
  4. NeedsHuman hint 不再写死 mohurd.py
  5. localstatus 前缀收紧：DBJ50/DGJ11 等外地编号不误判上海
  6. mohurd 不再自带 _CATEGORY_RULES/classify_title 副本
  7. untitled 主干带 ann_id，不同公告不再撞名
  8. autosync 锁内 notify 用最新 empty 而非锁外快照
  9. post_json 收到 HTML 错误页抛 SourceUnreachable 而非裸 ValueError
 10. 416 不再盲接受：HEAD 长度不符则删 .part 重下一次
 11. lookup 三小修：_dedupe 空键、窗口过滤保留无日期、名称分级匹配
"""

from __future__ import annotations

import argparse
import json
import tempfile
import urllib.error
from pathlib import Path
from unittest import mock

import pytest

from guiju import compliance
from guiju.domain.attachment import Attachment, _is_pdf
from guiju.domain import codes, naming
from guiju import files, localstatus
from guiju.errors import BadUsage, NeedsHuman, SourceUnreachable


# ---------- 1. _is_pdf ----------

def test_is_pdf_by_url_extension() -> None:
    assert _is_pdf(Attachment(url="https://x.gov.cn/f/abc.PDF?x=1")) is True
    assert _is_pdf(Attachment(url="https://x.gov.cn/f/abc.pdf#page=2")) is True


def test_is_pdf_by_label_marker() -> None:
    assert _is_pdf(Attachment(url="https://x.gov.cn/dl/1", label="规范正文.pdf")) is True


def test_is_pdf_rejects_pdf_substring_traps() -> None:
    # 名字里带 "pdf" 但不是 PDF 的（真实案例：PDF版.zip / xxpdfyy）
    assert _is_pdf(Attachment(url="https://x.gov.cn/f/x.zip", label="规范PDF版.zip")) is False
    assert _is_pdf(Attachment(url="https://x.gov.cn/f/xxpdfyy.doc")) is False


# ---------- 2. 1 位序号 ----------

def test_codes_parse_single_digit_serial() -> None:
    # 直接打编号正则：1 位序号（实测 CJJ 1-1990）必须命中
    m = codes._CODE_RE.search("CJJ 1-1990")
    assert m is not None and m.group(2) == "1" and m.group(3) == "1990"
    assert codes.normalize_key("CJJ 1-1990") == "CJJ1-1990"
    # 回归：常规多位序号不受影响
    m2 = codes._CODE_RE.search("GB 55037-2022")
    assert m2 is not None and m2.group(2) == "55037"


# ---------- 3. search 负 limit ----------

def test_search_negative_limit_is_bad_usage() -> None:
    from guiju.cli.commands import search

    args = argparse.Namespace(keywords=["防火"], limit=-1, all=False)
    with pytest.raises(BadUsage):
        search.run(mock.Mock(), args)


# ---------- 4. NeedsHuman hint 泛化 ----------

def test_needs_human_hint_not_mohurd_only() -> None:
    err = NeedsHuman("boom")
    assert "mohurd.py" not in err.hint or "shzjw.py" in err.hint
    assert "sources/" in err.hint


# ---------- 5. localstatus 前缀 ----------

def test_localstatus_shanghai_prefixes() -> None:
    for ref in ("DG/TJ08-2007-2026", "DBJT08-109-2017", "DGJ08-88-2021", "DB31/T 1531-2024", "DBJ08-214-2021"):
        assert localstatus.is_local_code(ref), ref


def test_localstatus_rejects_foreign_prefixes() -> None:
    for ref in ("DBJ50-023-2020", "DGJ11-01-2020", "DB32/T 1-2020", "DB11/T 1-2020"):
        assert not localstatus.is_local_code(ref), ref


# ---------- 6. mohurd 去重：分类规则唯一来源 domain.announcement ----------

def test_mohurd_uses_domain_classify_title() -> None:
    from guiju.domain import announcement
    from guiju.sources import mohurd

    assert mohurd.classify_title is announcement.classify_title
    assert not hasattr(mohurd, "_CATEGORY_RULES")
    assert not hasattr(mohurd, "_NAME_RE")


# ---------- 7. untitled 撞名 ----------

def test_untitled_stem_includes_ann_id() -> None:
    assert naming.library_stem(None, "", 1) != naming.library_stem(None, "", 2)
    assert naming.library_stem(None, "") == "untitled"  # 无 ann_id 时维持旧名


def test_find_cached_untitled_uses_ann_id(tmp_path: Path) -> None:
    content = b"pdf-body-for-ann-7"
    digest = files.sha256_bytes(content)
    name = naming.library_name(None, "", digest, ann_id=7)
    (tmp_path / name).write_bytes(content)
    # 正确 ann_id 命中；别的 ann_id / 裸 untitled 都不得误中
    assert files.find_cached(tmp_path, None, "", ann_id=7) is not None
    assert files.find_cached(tmp_path, None, "", ann_id=8) is None
    assert files.find_cached(tmp_path, None, "") is None


# ---------- 8. autosync notify 用锁内最新 empty ----------

def _store() -> "object":
    from guiju.store.sqlite import Store

    store = Store(Path(tempfile.mkdtemp()) / "c.db")
    return store


def test_autosync_notify_uses_fresh_empty(monkeypatch) -> None:
    from guiju import autosync

    store = _store()
    # 锁外判定时空目录；锁内复查已被"别的进程"补满 → 提示应为增量而非首次同步
    counts = iter([0, 5, 5, 5, 5])
    monkeypatch.setattr(store, "count_announcements", lambda: next(counts))
    monkeypatch.setattr(store, "catalog_age_days", lambda: 9.5)
    monkeypatch.setattr("guiju.syncengine.crawl", lambda s: {"new": 2})
    msgs: list[str] = []
    autosync.evaluate(store, command="search", notify=msgs.append)
    assert msgs and "增量同步" in msgs[0] and "首次同步" not in msgs[0]


# ---------- 9. post_json：HTML 错误页 → SourceUnreachable ----------

def test_post_json_html_body_is_source_unreachable(monkeypatch) -> None:
    monkeypatch.setattr(
        compliance, "_post_once",
        lambda url, data, **kw: b"<html><body>403 Forbidden</body></html>",
    )
    with pytest.raises(SourceUnreachable) as exc:
        compliance.post_json("https://x.gov.cn/api", {"a": 1})
    assert exc.value.exit == 20
    assert "JSON" in str(exc.value)


# ---------- 10. 416：HEAD 校验，不符则重下 ----------

class _Resp:
    def __init__(self, status=200, body=b"data", ctype="application/pdf"):
        self.status = status
        self.headers = {"Content-Type": ctype, "Content-Length": str(len(body))}
        self._body = body
        self._pos = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        if self._pos >= len(self._body):
            return b""
        chunk = self._body[self._pos : self._pos + (n if n > 0 else len(self._body))]
        self._pos += len(chunk)
        return chunk


def _http416() -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x.gov.cn/f.pdf", 416, "Range", {}, None)


def test_416_accepts_when_remote_size_matches(monkeypatch, tmp_path: Path) -> None:
    part = tmp_path / "x.part"
    part.write_bytes(b"12345")
    calls = []

    def open_side(req, timeout=0):
        calls.append(req.headers.get("Range"))
        raise _http416()

    monkeypatch.setattr(compliance, "_opener", mock.Mock(open=open_side))
    monkeypatch.setattr(compliance, "check_url", lambda url: url)
    monkeypatch.setattr(compliance, "_throttle", lambda host: None)
    monkeypatch.setattr(compliance, "_remote_length", lambda url, **kw: 5)
    compliance._download_once("https://x.gov.cn/f.pdf", part, start=5, referer=None, timeout=10, chunk_size=4)
    assert part.read_bytes() == b"12345"          # 未删未重下
    assert calls == ["bytes=5-"]                   # 只有最初一次 GET


def test_416_restarts_when_remote_size_mismatch(monkeypatch, tmp_path: Path) -> None:
    part = tmp_path / "x.part"
    part.write_bytes(b"corrupted-longer-than-remote")
    calls = []

    def open_side(req, timeout=0):
        calls.append(req.headers.get("Range"))
        if req.headers.get("Range"):
            raise _http416()
        return _Resp(status=200, body=b"fresh")

    monkeypatch.setattr(compliance, "_opener", mock.Mock(open=open_side))
    monkeypatch.setattr(compliance, "check_url", lambda url: url)
    monkeypatch.setattr(compliance, "_throttle", lambda host: None)
    monkeypatch.setattr(compliance, "_remote_length", lambda url, **kw: 5)  # remote=5 < local
    compliance._download_once("https://x.gov.cn/f.pdf", part, start=28, referer=None, timeout=10, chunk_size=4)
    assert part.read_bytes() == b"fresh"           # 删掉重下
    assert calls[0] == "bytes=28-" and calls[1] is None  # 第二次无 Range


def test_416_accept_when_head_answers_nothing(monkeypatch, tmp_path: Path) -> None:
    part = tmp_path / "x.part"
    part.write_bytes(b"done")
    monkeypatch.setattr(compliance, "_opener", mock.Mock(open=lambda req, timeout=0: (_ for _ in ()).throw(_http416())))
    monkeypatch.setattr(compliance, "check_url", lambda url: url)
    monkeypatch.setattr(compliance, "_throttle", lambda host: None)
    monkeypatch.setattr(compliance, "_remote_length", lambda url, **kw: None)
    compliance._download_once("https://x.gov.cn/f.pdf", part, start=4, referer=None, timeout=10, chunk_size=4)
    assert part.read_bytes() == b"done"


# ---------- 11. lookup 三小修 ----------

def test_dedupe_keeps_distinct_empty_key_rows() -> None:
    from guiju.cli.lookup import _dedupe

    rows = [
        {"code_key": "", "id": 1},
        {"code_key": "", "id": 2},
        {"code_key": "", "url": "u3"},
        {"code_key": "GB55037-2022", "id": 4},
        {"code_key": "GB55037-2022", "id": 5},
    ]
    out = _dedupe(rows)
    assert [r.get("id") for r in out] == [1, 2, None, 4]


def test_version_window_keeps_undated_hits() -> None:
    from guiju.cli.lookup import _filter_by_version_window

    class H:
        def __init__(self, d):
            self.entry = {"published_on": d}

    hits = [H("2015-01-01"), H("2026-01-01"), H(None), H("")]
    out = _filter_by_version_window(hits, "2018-10-01")
    dates = [h.entry["published_on"] for h in out]
    assert "2026-01-01" not in dates          # 晚于实施日：有证据，滤掉
    assert "2015-01-01" in dates
    assert dates.count(None) + dates.count("") == 2  # 无日期：无证据，保留


def test_name_match_level_tiers() -> None:
    from guiju.cli.lookup import _best_name_hits, _name_match_level

    assert _name_match_level("建筑设计防火规范", "建筑设计防火规范") == 3
    assert _name_match_level("《建筑设计防火规范》", "建筑设计防火规范") == 2
    assert _name_match_level("建筑设计防火规范修订", "建筑设计防火规范") == 1
    assert _name_match_level("城市给水工程", "建筑设计防火规范") == 0

    class H:
        def __init__(self, name):
            self.entry = {"name": name}

    hits = [H("建筑设计防火规范修订"), H("建筑设计防火规范"), H("完全不相关")]
    best = _best_name_hits(hits, "建筑设计防火规范")
    assert [h.entry["name"] for h in best] == ["建筑设计防火规范"]

    # 没有精确命中时降级用"仅互含"，保持旧行为
    hits2 = [H("建筑设计防火规范修订"), H("完全不相关")]
    best2 = _best_name_hits(hits2, "建筑设计防火规范")
    assert [h.entry["name"] for h in best2] == ["建筑设计防火规范修订"]
