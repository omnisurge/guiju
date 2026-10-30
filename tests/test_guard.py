"""合规护栏 —— 白名单必须对每一次跳转生效。"""

import unittest
import urllib.request

from guiju import compliance
from guiju.errors import ComplianceDenied


class WhitelistTest(unittest.TestCase):
    def test_allows_gov(self):
        for url in (
            "https://www.mohurd.gov.cn/gongkai/zc/wjk/art/2026/x.html",
            "http://www.gov.cn/a.pdf",
            "https://zjw.sh.gov.cn/cmsres/a.pdf",
        ):
            self.assertEqual(compliance.check_url(url), url)

    def test_rejects_non_gov(self):
        for url in (
            "https://www.jianbiaoku.com/a.pdf",
            "https://gov.cn.evil.com/a.pdf",
            "ftp://www.gov.cn/a.pdf",
            "https://www.mohurd.gov.cn.evil.com/x",
        ):
            with self.assertRaises(ComplianceDenied, msg=url):
                compliance.check_url(url)


class RedirectTest(unittest.TestCase):
    def test_redirect_to_foreign_host_is_denied(self):
        handler = compliance._GuardedRedirect()
        req = urllib.request.Request("https://www.mohurd.gov.cn/old")
        with self.assertRaises(ComplianceDenied):
            handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example.com/x")

    def test_redirect_relative_url_resolves_against_origin(self):
        handler = compliance._GuardedRedirect()
        req = urllib.request.Request("https://www.mohurd.gov.cn/a/b")
        # 相对跳转应被解析为同域，从而通过白名单（随后由基类构造/拒绝，不作为断言对象）
        try:
            handler.redirect_request(req, None, 302, "Found", {}, "/c/d")
        except ComplianceDenied:  # pragma: no cover - 相对跳转不应触发合规拒绝
            self.fail("同域相对跳转被误判为非白名单")
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main()
