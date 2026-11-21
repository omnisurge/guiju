"""合规护栏 —— 白名单必须对每一次跳转生效。"""

import unittest
import urllib.request

from guiju import compliance
from guiju.errors import ComplianceDenied
from guiju.net.block import BlockDetector
from guiju.net.policy import BodyPolicy
from guiju.net.redirect import RedirectGuard


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
        handler = RedirectGuard(BodyPolicy(check_url_fn=compliance.check_url), compliance._CompatThrottle(), BlockDetector())
        req = urllib.request.Request("https://www.mohurd.gov.cn/old")
        with self.assertRaises(ComplianceDenied):
            handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example.com/x")

    def test_redirect_relative_url_resolves_against_origin(self):
        handler = RedirectGuard(BodyPolicy(check_url_fn=compliance.check_url), compliance._CompatThrottle(), BlockDetector())
        req = urllib.request.Request("https://www.mohurd.gov.cn/a/b")
        # 相对跳转应被解析为同域；不能只用 except Exception: pass 把回归吞掉。
        result = handler.redirect_request(req, None, 302, "Found", {}, "/c/d")
        self.assertIsNotNone(result)
        self.assertEqual(result.full_url, "https://www.mohurd.gov.cn/c/d")


if __name__ == "__main__":
    unittest.main()
