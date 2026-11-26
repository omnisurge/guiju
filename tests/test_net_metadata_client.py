"""元数据 HTTP 韧性与护栏：所有响应/时钟均为内存 fake，不访问上游。"""

import http.client
import io
import unittest
import urllib.error
import urllib.request
from unittest import mock

from guiju._version import __version__
from guiju.currency import jsonutil
from guiju.errors import ComplianceDenied, NeedsHuman, SourceUnreachable
from guiju.net import metadata as net_metadata
from guiju.net.endpoints import CHINABUILDING_CONCISE_PATH, CHINABUILDING_HOST
from guiju.net.metadata import MAX_RESPONSE_BYTES, UA, MetadataClient
from guiju.net.policy import BodyPolicy, MetadataPolicy
from guiju.net.throttle import Throttle
from tests.currency_helpers import URL, FakeResponse, real_response
from tests.net_harness import build_metadata_opener, metadata_redirect


def status_error(status, body=b"temporary error"):
    return urllib.error.HTTPError(URL, status, "upstream error", {}, io.BytesIO(body))


class MetadataClientTest(unittest.TestCase):
    def setUp(self):
        self.throttle = mock.Mock()
        self.client = MetadataClient(throttle=self.throttle, transport=mock.Mock())
        self.sleep = mock.patch.object(net_metadata.time, "sleep").start()
        self.no_socket = mock.patch("socket.create_connection", side_effect=AssertionError("不应真实联网")).start()
        self.addCleanup(mock.patch.stopall)

    def test_200_uses_ascii_versioned_ua_timeout_and_closes_response(self):
        resp = FakeResponse(b"<html>ok</html>")
        self.client.transport.open.return_value = resp
        self.assertEqual(self.client.get(URL), b"<html>ok</html>")
        req = self.client.transport.open.call_args.args[0]
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(req.get_header("User-agent"), UA)
        self.assertTrue(UA.isascii())
        self.assertIn("guiju/" + __version__, UA)
        self.assertEqual(req.get_header("Accept"), "text/html,application/xhtml+xml")
        self.assertEqual(self.client.transport.open.call_args.kwargs, {"timeout": 25.0})
        self.assertEqual(resp.read_sizes, [MAX_RESPONSE_BYTES + 1, MAX_RESPONSE_BYTES + 1 - 15])
        self.assertTrue(resp.closed)
        self.sleep.assert_not_called()

    def test_timeout_is_configurable_but_must_be_positive_and_finite(self):
        self.assertEqual(MetadataClient(timeout=0.1).timeout, 0.1)
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                MetadataClient(timeout=timeout)
        with MetadataClient() as client:
            self.assertIsInstance(client, MetadataClient)
            client.close()

    def test_500_then_200_retries_once(self):
        err = status_error(500)
        self.client.transport.open.side_effect = [err, FakeResponse()]
        self.assertEqual(self.client.get(URL), b"<html></html>")
        self.assertEqual(self.client.transport.open.call_count, 2)
        self.sleep.assert_called_once_with(0.5)
        self.assertTrue(err.closed)
        self.assertEqual(self.throttle.wait.call_count, 2)

    def test_502_exhausts_exactly_three_attempts(self):
        self.client.transport.open.side_effect = [status_error(502) for _ in range(3)]
        with self.assertRaises(SourceUnreachable) as ctx:
            self.client.get(URL)
        self.assertIn("502", str(ctx.exception))
        self.assertEqual(ctx.exception.exit, 20)
        self.assertEqual(self.client.transport.open.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [mock.call(0.5), mock.call(1.5)])

    def test_429_without_challenge_is_bounded_retry(self):
        self.client.transport.open.side_effect = [status_error(429), FakeResponse()]
        self.assertEqual(self.client.get(URL), b"<html></html>")
        self.assertEqual(self.client.transport.open.call_count, 2)

    def test_other_4xx_does_not_retry_or_become_empty_result(self):
        for status in (400, 401, 403, 404):
            self.client.transport.open.reset_mock(side_effect=True)
            self.sleep.reset_mock()
            self.client.transport.open.side_effect = status_error(status)
            with self.subTest(status=status), self.assertRaises(SourceUnreachable):
                self.client.get(URL)
            self.client.transport.open.assert_called_once()
            self.sleep.assert_not_called()

    def test_transport_errors_retry_then_recover(self):
        self.client.transport.open.side_effect = [urllib.error.URLError("reset"), TimeoutError("slow"), FakeResponse()]
        self.assertEqual(self.client.get(URL), b"<html></html>")
        self.assertEqual(self.client.transport.open.call_count, 3)
        requests = [call.args[0] for call in self.client.transport.open.call_args_list]
        self.assertEqual(len({id(req) for req in requests}), 3)

    def test_transport_failure_exhaustion_raises_not_empty(self):
        self.client.transport.open.side_effect = urllib.error.URLError("offline")
        with self.assertRaises(SourceUnreachable) as ctx:
            self.client.get(URL)
        self.assertIn("offline", str(ctx.exception))
        self.assertEqual(self.client.transport.open.call_count, 3)

    def test_200_body_read_failure_does_not_refetch(self):
        resp = FakeResponse()
        resp.read = mock.Mock(side_effect=http.client.IncompleteRead(b"partial", 100))
        self.client.transport.open.return_value = resp
        with self.assertRaises(SourceUnreachable) as ctx:
            self.client.get(URL)
        self.assertIn("读取失败", str(ctx.exception))
        self.client.transport.open.assert_called_once()
        self.sleep.assert_not_called()
        self.assertTrue(resp.closed)

    def test_real_content_length_truncation_is_not_empty_match(self):
        from guiju.currency.csres import CsresCurrency

        resp = real_response(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 1000\r\n\r\n<html>")
        self.client.transport.open.return_value = resp
        with self.assertRaises(SourceUnreachable) as ctx:
            CsresCurrency(self.client).lookup("GB50011")
        self.assertEqual(ctx.exception.exit, 20)
        self.assertIn("响应截断", str(ctx.exception))
        self.client.transport.open.assert_called_once()
        self.sleep.assert_not_called()
        self.assertTrue(resp.closed)

    def test_real_complete_content_length_and_chunked_bodies(self):
        for wire, expected in (
            (b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 4\r\n\r\ntest", b"test"),
            (b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nTransfer-Encoding: chunked\r\n\r\n"
             b"4\r\ntest\r\n0\r\n\r\n", b"test"),
        ):
            resp = real_response(wire)
            self.client.transport.open.return_value = resp
            with self.subTest(wire=wire):
                self.assertEqual(self.client.get(URL), expected)
                self.assertTrue(resp.closed)

    def test_real_truncated_chunked_body_is_unreachable(self):
        resp = real_response(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nTransfer-Encoding: chunked\r\n\r\n10\r\nshort")
        self.client.transport.open.return_value = resp
        with self.assertRaises(SourceUnreachable):
            self.client.get(URL)
        self.client.transport.open.assert_called_once()
        self.assertTrue(resp.closed)

    def test_short_reads_are_joined_until_eof_within_size_limit(self):
        class ShortResponse(FakeResponse):
            def read(self, size=-1):
                return super().read(min(size, 3))

        self.client.transport.open.return_value = ShortResponse(b"<html>complete</html>")
        self.assertEqual(self.client.get(URL), b"<html>complete</html>")

    def test_error_body_read_failure_keeps_http_classification(self):
        err = status_error(403)
        err.read = mock.Mock(side_effect=OSError("cannot read error page"))
        self.client.transport.open.side_effect = err
        with self.assertRaises(SourceUnreachable) as ctx:
            self.client.get(URL)
        self.assertIn("403", str(ctx.exception))
        self.client.transport.open.assert_called_once()
        self.assertTrue(err.closed)

    def test_non_200_success_status_is_not_parsed(self):
        for status in (204, 206):
            self.client.transport.open.reset_mock(side_effect=True)
            resp = FakeResponse(status=status)
            self.client.transport.open.return_value = resp
            with self.subTest(status=status), self.assertRaises(SourceUnreachable):
                self.client.get(URL)
            self.assertEqual(resp.read_sizes, [])
            self.assertTrue(resp.closed)
            self.client.transport.open.assert_called_once()

    def test_challenge_200_detected_in_both_utf8_and_gbk_without_retry(self):
        for encoding in ("utf-8", "gbk"):
            self.client.transport.open.reset_mock(side_effect=True)
            self.client.transport.open.return_value = FakeResponse("<html>请输入验证码</html>".encode(encoding))
            with self.subTest(encoding=encoding), self.assertRaises(ComplianceDenied) as ctx:
                self.client.get(URL)
            self.assertEqual(ctx.exception.exit, 40)
            self.client.transport.open.assert_called_once()
            self.sleep.assert_not_called()

    def test_challenge_http_error_never_retries_even_429_or_503(self):
        for status in (403, 429, 503):
            self.client.transport.open.reset_mock(side_effect=True)
            self.client.transport.open.side_effect = status_error(status, "人机验证".encode("gbk"))
            with self.subTest(status=status), self.assertRaises(ComplianceDenied):
                self.client.get(URL)
            self.client.transport.open.assert_called_once()
            self.sleep.assert_not_called()

    def test_non_metadata_urls_are_rejected_before_request(self):
        for url in (
            "https://evil.example/s.jsp", "http://csres.com.evil.example/s.jsp",
            "http://www.csres.com/standard/1.pdf", "https://www.mohurd.gov.cn/a.pdf",
            "file:///tmp/test", "ftp://www.csres.com/s.jsp", "http://www.csres.com:8080/s.jsp",
            "http://user:pass@www.csres.com/s.jsp", "http://www.csres.com:bad/s.jsp",
            "http://[bad/s.jsp", URL + "#fragment",
        ):
            with self.subTest(url=url), self.assertRaises(ComplianceDenied):
                self.client.get(url)
        self.client.transport.open.assert_not_called()

    def test_attachment_or_binary_type_is_refused_before_reading(self):
        for headers in (
            {"Content-Type": "application/pdf"}, {"Content-Type": "application/octet-stream"},
            {"Content-Type": "text/html", "Content-Disposition": 'attachment; filename="code.pdf"'}, {},
        ):
            resp = FakeResponse(b"%PDF-body", headers=headers)
            self.client.transport.open.return_value = resp
            with self.subTest(headers=headers), self.assertRaises(ComplianceDenied):
                self.client.get(URL)
            self.assertEqual(resp.read_sizes, [])
            self.assertTrue(resp.closed)
        self.sleep.assert_not_called()

    def test_binary_disguised_as_html_is_refused(self):
        for body in (b" %PDF-1.7", b"PK\x03\x04data", b"\xd0\xcf\x11\xe0data"):
            self.client.transport.open.return_value = FakeResponse(body)
            with self.subTest(body=body), self.assertRaises(ComplianceDenied):
                self.client.get(URL)
        self.sleep.assert_not_called()

    def test_response_size_is_bounded_and_not_retried(self):
        resp = FakeResponse(b"x" * (MAX_RESPONSE_BYTES + 10))
        self.client.transport.open.return_value = resp
        with self.assertRaises(NeedsHuman):
            self.client.get(URL)
        self.assertEqual(resp.read_sizes, [MAX_RESPONSE_BYTES + 1])
        self.client.transport.open.assert_called_once()
        self.assertTrue(resp.closed)

    def test_final_url_cannot_escape_approved_endpoint(self):
        self.client.transport.open.return_value = FakeResponse(url="https://evil.example/page")
        with self.assertRaises(ComplianceDenied):
            self.client.get(URL)
        self.client.transport.open.assert_called_once()


class RedirectAndThrottleTest(unittest.TestCase):
    def test_redirect_checks_every_target_before_following(self):
        throttle = mock.Mock()
        handler = metadata_redirect(throttle)
        req = urllib.request.Request(URL, headers={"User-Agent": UA})
        redirect = handler.redirect_request(req, None, 302, "Found", {}, "/s.jsp?keyword=GB1")
        self.assertEqual(redirect.full_url, "http://www.csres.com/s.jsp?keyword=GB1")
        throttle.wait.assert_called_once_with("www.csres.com")
        for target in ("https://evil.example/s.jsp", "/standard/1.pdf", "//evil.example/s.jsp"):
            with self.subTest(target=target), self.assertRaises(ComplianceDenied):
                handler.redirect_request(redirect, None, 302, "Found", {}, target)
        self.assertEqual(throttle.wait.call_count, 1)
        self.assertEqual(handler.max_redirections, 5)

    def test_csres_error_page_redirect_is_source_unreachable_for_fallback(self):
        handler = metadata_redirect()
        req = urllib.request.Request(URL, headers={"User-Agent": UA})
        for target in ("/error/noright.html", "/error/noright.html;jsessionid=abc.wwwcsres"):
            with self.subTest(target=target), self.assertRaises(SourceUnreachable):
                handler.redirect_request(req, None, 302, "Found", {}, target)

    def test_real_opener_redirect_reads_only_bounded_intermediate_head(self):
        for status in (301, 302, 303, 307, 308):
            first = real_response(
                f"HTTP/1.1 {status} Found\r\nLocation: /s.jsp?keyword=GB1\r\n"
                f"Content-Type: text/html\r\nContent-Length: {MAX_RESPONSE_BYTES + 100}\r\n\r\n".encode()
                + b"x" * (MAX_RESPONSE_BYTES + 100)
            )
            first.read = mock.Mock(wraps=first.read)
            last = real_response(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 4\r\n\r\ntest")
            replies = iter((first, last))

            class MemoryTransport(urllib.request.HTTPHandler):
                def http_open(self, req, replies=replies):
                    return next(replies)

            client = MetadataClient(throttle=Throttle(0), transport=build_metadata_opener(MemoryTransport()))
            with self.subTest(status=status), \
                    mock.patch("socket.create_connection", side_effect=AssertionError("不应真实联网")):
                self.assertEqual(client.get(URL), b"test")
                first.read.assert_called_once_with(65536)
                self.assertTrue(first.closed)

    def test_real_opener_redirect_attachment_is_denied_without_read_or_follow(self):
        body = b"%PDF-" + b"x" * (MAX_RESPONSE_BYTES + 100)
        first = real_response(
            b"HTTP/1.1 302 Found\r\nLocation: /s.jsp?keyword=GB1\r\nContent-Type: application/pdf\r\n"
            b"Content-Disposition: attachment; filename=code.pdf\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode() + body
        )
        first.read = mock.Mock(wraps=first.read)
        calls = []

        class MemoryTransport(urllib.request.HTTPHandler):
            def http_open(self, req):
                calls.append(req)
                return first

        client = MetadataClient(throttle=Throttle(0), transport=build_metadata_opener(MemoryTransport()))
        with mock.patch("socket.create_connection", side_effect=AssertionError("不应真实联网")), \
                self.assertRaises(ComplianceDenied):
            client.get(URL)
        self.assertEqual(len(calls), 1)
        first.read.assert_not_called()
        self.assertTrue(first.closed)

    def test_real_opener_redirect_challenge_or_disguised_binary_stops(self):
        for body in ("请输入验证码".encode("gbk"), "人机验证".encode(), b"%PDF-disguised"):
            first = real_response(
                b"HTTP/1.1 302 Found\r\nLocation: /s.jsp?keyword=GB1\r\nContent-Type: text/html\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode() + body
            )
            first.read = mock.Mock(wraps=first.read)
            calls = []

            class MemoryTransport(urllib.request.HTTPHandler):
                def http_open(self, req, calls=calls, first=first):
                    calls.append(req)
                    return first

            client = MetadataClient(throttle=Throttle(0), transport=build_metadata_opener(MemoryTransport()))
            with self.subTest(body=body), \
                    mock.patch("socket.create_connection", side_effect=AssertionError("不应真实联网")), \
                    self.assertRaises(ComplianceDenied):
                client.get(URL)
            self.assertEqual(len(calls), 1)
            first.read.assert_called_once_with(65536)
            self.assertTrue(first.closed)

    def test_real_opener_truncated_redirect_is_unreachable_not_followed(self):
        first = real_response(
            b"HTTP/1.1 302 Found\r\nLocation: /s.jsp?keyword=GB1\r\nContent-Type: text/html\r\n"
            b"Content-Length: 1000\r\n\r\n<html>"
        )
        calls = []

        class MemoryTransport(urllib.request.HTTPHandler):
            def http_open(self, req):
                calls.append(req)
                return first

        client = MetadataClient(throttle=Throttle(0), transport=build_metadata_opener(MemoryTransport()))
        with self.assertRaises(SourceUnreachable) as ctx:
            client.get(URL)
        self.assertIn("响应截断", str(ctx.exception))
        self.assertEqual(len(calls), 1)
        self.assertTrue(first.closed)

    def test_real_opener_redirect_loop_is_bounded_unreachable(self):
        calls = []

        class MemoryTransport(urllib.request.HTTPHandler):
            def http_open(self, req):
                calls.append(req)
                return real_response(
                    b"HTTP/1.1 302 Found\r\nLocation: /s.jsp?keyword=GB50011&pageNum=1\r\n"
                    b"Content-Type: text/html\r\nContent-Length: 0\r\n\r\n"
                )

        handler = metadata_redirect()
        client = MetadataClient(
            throttle=Throttle(0),
            transport=build_metadata_opener(MemoryTransport(), handler=handler),
        )
        with self.assertRaises(SourceUnreachable) as ctx:
            client.get(URL)
        self.assertEqual(ctx.exception.exit, 20)
        self.assertLessEqual(len(calls), handler.max_redirections + 1)

    def test_real_opener_missing_location_or_illegal_scheme_has_classified_error(self):
        for location, error in ((b"", SourceUnreachable), (b"Location: file:///tmp/no-read\r\n", ComplianceDenied)):
            first = real_response(
                b"HTTP/1.1 302 Found\r\n" + location + b"Content-Type: text/html\r\nContent-Length: 0\r\n\r\n"
            )
            first.read = mock.Mock(wraps=first.read)
            calls = []

            class MemoryTransport(urllib.request.HTTPHandler):
                def http_open(self, req, calls=calls, first=first):
                    calls.append(req)
                    return first

            client = MetadataClient(throttle=Throttle(0), transport=build_metadata_opener(MemoryTransport()))
            with self.subTest(location=location), \
                    mock.patch("socket.create_connection", side_effect=AssertionError("不应真实联网")), \
                    self.assertRaises(error):
                client.get(URL)
            self.assertEqual(len(calls), 1)
            first.read.assert_not_called()
            self.assertTrue(first.closed)

    def test_throttle_never_shortens_host_interval(self):
        ticks = iter([10.0, 10.0, 10.3, 11.2, 20.0, 20.0])
        sleeps: list[float] = []
        throttle = Throttle(clock=lambda: next(ticks), sleep=sleeps.append)
        throttle.wait("www.csres.com")
        throttle.wait("www.csres.com")
        throttle.wait("other-host")
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 0.9)
        self.assertEqual(throttle.min_interval, 1.2)


class M2MetadataClientTest(unittest.TestCase):
    CCSN = "https://www.ccsn.org.cn/newweb/api/StandardQuery.assx/queryStandard"
    SAMR = "https://std.samr.gov.cn/gb/search/gbQueryPage"
    DBBA = "https://dbba.sacinfo.org.cn/stdQueryList"

    def setUp(self):
        self.client = MetadataClient(throttle=Throttle(0), transport=mock.Mock())
        self.sleep = mock.patch.object(net_metadata.time, "sleep").start()
        mock.patch("socket.create_connection", side_effect=AssertionError("不应真实联网")).start()
        self.addCleanup(mock.patch.stopall)

    def response(self, url, body=b'{"rows":[]}', ctype="application/json"):
        return FakeResponse(body, url=url, headers={"Content-Type": ctype})

    def test_json_get_uses_fixed_metadata_endpoint_and_accept(self):
        url = self.SAMR + "?searchText=GB19001"
        self.client.transport.open.return_value = self.response(url)
        self.assertEqual(self.client.get(url, accept="application/json"), b'{"rows":[]}')
        req = self.client.transport.open.call_args.args[0]
        self.assertEqual(req.get_header("Accept"), "application/json")
        self.assertEqual(req.get_method(), "GET")

    def test_post_form_encodes_unicode_and_preserves_dbba_no_accept(self):
        import urllib.parse

        for url, accept in ((self.CCSN, "application/json"), (self.DBBA, None)):
            self.client.transport.open.return_value = self.response(url)
            self.client.post_form(url, {"key": "DB31/T 792-2015", "备注": "中文"}, accept=accept)
            req = self.client.transport.open.call_args.args[0]
            with self.subTest(url=url):
                self.assertEqual(req.get_method(), "POST")
                self.assertEqual(req.get_header("Accept"), accept)
                self.assertEqual(req.get_header("Content-type"), "application/x-www-form-urlencoded")
                self.assertEqual(urllib.parse.parse_qs(req.data.decode("ascii")),
                                 {"key": ["DB31/T 792-2015"], "备注": ["中文"]})
                self.assertEqual(req.get_header("User-agent"), UA)

    def test_post_retry_rebuilds_identical_query_body(self):
        self.client.transport.open.side_effect = [status_error(503), self.response(self.CCSN)]
        self.client.post_form(self.CCSN, {"gjz": "50011", "pageIndex": "1"})
        requests = [call.args[0] for call in self.client.transport.open.call_args_list]
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].data, requests[1].data)
        self.assertIsNot(requests[0], requests[1])
        self.assertTrue(all(req.get_method() == "POST" for req in requests))
        self.sleep.assert_called_once_with(0.5)

    def test_forms_cannot_be_sent_to_html_or_unapproved_endpoints(self):
        for url in (URL, "https://www.ccsn.org.cn/download", "https://evil.example/query"):
            with self.subTest(url=url), self.assertRaises(ComplianceDenied):
                self.client.post_form(url, {"key": "GB1"})
        self.client.transport.open.assert_not_called()

    def test_escaped_json_or_html_challenge_is_immediate_40(self):
        for body in (rb'{"error":"\u9a8c\u8bc1\u7801"}', b"<html>&#x9A8C;&#x8BC1;&#x7801;</html>"):
            self.client.transport.open.reset_mock()
            self.client.transport.open.return_value = self.response(self.DBBA, body)
            with self.subTest(body=body), self.assertRaises(ComplianceDenied):
                self.client.post_form(self.DBBA, {"key": "DB1"}, accept=None)
            self.client.transport.open.assert_called_once()
            self.sleep.assert_not_called()

    def test_json_headers_do_not_permit_attachments_or_unlisted_hosts(self):
        self.client.transport.open.return_value = FakeResponse(
            b"{}", url=self.SAMR, headers={"Content-Type": "application/json", "Content-Disposition": "attachment"})
        with self.assertRaises(ComplianceDenied):
            self.client.get(self.SAMR, accept="application/json")
        with self.assertRaises(ComplianceDenied):
            self.client.get("https://openstd.samr.gov.cn/gb/search/gbQueryPage", accept="application/json")

    def test_even_approved_cross_source_redirect_is_denied_before_body_read(self):
        handler = metadata_redirect()
        req = urllib.request.Request(URL)
        intermediate = FakeResponse(b"irrelevant")
        with self.assertRaises(ComplianceDenied):
            handler.http_error_302(req, intermediate, 302, "Found", {"Location": self.SAMR})
        self.assertEqual(intermediate.read_sizes, [])
        self.assertTrue(intermediate.closed)

    def test_final_response_cannot_claim_another_approved_source(self):
        self.client.transport.open.return_value = self.response(self.DBBA)
        with self.assertRaises(ComplianceDenied):
            self.client.get(self.SAMR, accept="application/json")

    def test_json_structure_helpers_are_strict_but_preserve_go_null_zero_values(self):
        self.assertEqual(jsonutil.json_rows({}, "rows", "test"), [])
        self.assertEqual(jsonutil.json_rows({"rows": [None]}, "rows", "test"), [{}])
        self.assertEqual(jsonutil.json_string({"x": None}, "x", "test"), "")
        self.assertEqual(jsonutil.json_int({}, "x", "test"), 0)
        for raw in (b"[]", b"null", b"{bad", b'{"x":NaN}', b"\xff"):
            with self.subTest(raw=raw), self.assertRaises(NeedsHuman):
                jsonutil.decode_json("test", raw)
        for value in (True, 1.5, "2", 2**63):
            with self.subTest(value=value), self.assertRaises(NeedsHuman):
                jsonutil.json_int({"x": value}, "x", "test")
        for value in (True, 1, []):
            with self.subTest(value=value), self.assertRaises(NeedsHuman):
                jsonutil.json_string({"x": value}, "x", "test")
        self.assertIsNone(jsonutil.json_flag({}, "x", "test"))
        self.assertIs(jsonutil.json_flag({"x": True}, "x", "test"), True)
        self.assertIs(jsonutil.json_flag({"x": 0}, "x", "test"), False)
        self.assertIs(jsonutil.json_flag({"x": 1}, "x", "test"), True)
        for value in ("true", 2, -1, 1.0):
            with self.subTest(flag=value), self.assertRaises(NeedsHuman):
                jsonutil.json_flag({"x": value}, "x", "test")

    def test_deep_json_source_shape_is_needs_human_not_recursion_error(self):
        from guiju.currency.ccsn import parse_ccsn

        # 3.10 的 JSON decoder 会先触发 RecursionError；3.13 可解码后由行结构判50。
        raw = b'{"List":' + b'[' * 2000 + b']' * 2000 + b'}'
        with self.assertRaises(NeedsHuman) as ctx:
            parse_ccsn(raw)
        self.assertEqual(ctx.exception.exit, 50)

    def test_real_https_opener_post_redirect_method_and_body_semantics(self):
        for status, method in ((301, "GET"), (302, "GET"), (303, "GET"), (307, "POST"), (308, "POST")):
            first = real_response(
                f"HTTP/1.1 {status} Found\r\nLocation: {self.CCSN}?next=1\r\n"
                "Content-Type: text/html\r\nContent-Length: 0\r\n\r\n".encode())
            last = real_response(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
            replies, requests = iter((first, last)), []

            class MemoryTransport(urllib.request.HTTPSHandler):
                def https_open(self, req, replies=replies, requests=requests):
                    requests.append(req)
                    response = next(replies)
                    response.geturl = lambda url=req.full_url: url
                    return response

            client = MetadataClient(throttle=Throttle(0), transport=build_metadata_opener(MemoryTransport()))
            with self.subTest(status=status):
                self.assertEqual(client.post_form(self.CCSN, {"gjz": "50011"}), b"{}")
                self.assertEqual(len(requests), 2)
                self.assertEqual(requests[1].get_method(), method)
                self.assertEqual(requests[1].data, requests[0].data if method == "POST" else None)
                self.assertEqual(requests[1].get_header("Accept"), "application/json")
                self.assertEqual(requests[1].get_header("Content-type"),
                                 "application/x-www-form-urlencoded" if method == "POST" else None)
                self.assertTrue(first.closed)
                self.assertTrue(last.closed)


class ShanghaiJsonTransportTest(unittest.TestCase):
    URL = "https://bzjg.zjw.sh.gov.cn:9175/JGBStandardWebApi/external/localStandard/list"

    def setUp(self):
        self.client = MetadataClient(throttle=Throttle(0), transport=mock.Mock())
        mock.patch.object(net_metadata.time, "sleep").start()
        mock.patch("socket.create_connection", side_effect=AssertionError("不应真实联网")).start()
        self.addCleanup(mock.patch.stopall)

    def test_fixed_json_query_has_correct_body_origin_referer_and_ascii_ua(self):
        import json

        response = FakeResponse(b'{"data":{"list":[],"total":0}}', url=self.URL,
                                headers={"Content-Type": "application/json; charset=utf-8"})
        self.client.transport.open.return_value = response
        payload = {"condition": "上海地标", "isAbolish": False, "standardType": 0, "pageNum": 1}
        self.client.post_json(self.URL, payload)
        req = self.client.transport.open.call_args.args[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(json.loads(req.data.decode("utf-8")), payload)
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(req.get_header("Origin"), "https://bzjg.zjw.sh.gov.cn:9175")
        self.assertEqual(req.get_header("Referer"), "https://bzjg.zjw.sh.gov.cn:9175/JGBStandardWeb/pc/")
        self.assertEqual(req.get_header("Accept"), "application/json")
        self.assertEqual(req.get_header("User-agent"), UA)
        self.assertTrue(req.get_header("User-agent").isascii())
        self.assertTrue(response.closed)

    def test_no_port_scheme_file_or_foreign_endpoint_expansion(self):
        for url in (self.URL.replace(":9175", ""), self.URL.replace(":9175", ":443"),
                    self.URL.replace("https:", "http:"), self.URL.replace("/external/localStandard/list", "/api/files/webDownload"),
                    "https://bzjg.zjw.sh.gov.cn:9175/JGBStandardWeb/pc/", M2MetadataClientTest.CCSN):
            with self.subTest(url=url), self.assertRaises(ComplianceDenied):
                self.client.post_json(url, {})
        self.client.transport.open.assert_not_called()

    def test_shanghai_json_endpoint_cannot_be_called_as_form_or_get(self):
        with self.assertRaises(ComplianceDenied):
            self.client.post_form(self.URL, {"condition": "DG/TJ08"})
        with self.assertRaises(ComplianceDenied):
            self.client.get(self.URL)
        self.client.transport.open.assert_not_called()

    def test_json_post_503_retry_replays_identical_body(self):
        self.client.transport.open.side_effect = [status_error(503), FakeResponse(
            b"{}", url=self.URL, headers={"Content-Type": "application/json"})]
        self.client.post_json(self.URL, {"condition": "DG/TJ08-2007"})
        reqs = [call.args[0] for call in self.client.transport.open.call_args_list]
        self.assertEqual(len(reqs), 2)
        self.assertEqual(reqs[0].data, reqs[1].data)
        self.assertEqual(reqs[0].get_header("Content-type"), "application/json")

    def test_json_challenge_or_pdf_is_40_without_retry(self):
        for body, ctype in ((rb'{"error":"\u9a8c\u8bc1\u7801"}', "application/json"),
                            (b"%PDF-1.7", "application/json"), (b"binary", "application/pdf")):
            self.client.transport.open.reset_mock()
            self.client.transport.open.return_value = FakeResponse(body, url=self.URL, headers={"Content-Type": ctype})
            with self.subTest(body=body), self.assertRaises(ComplianceDenied):
                self.client.post_json(self.URL, {})
            self.client.transport.open.assert_called_once()

    def test_real_redirects_preserve_json_only_for_307_308(self):
        for status in (301, 302, 303, 307, 308):
            first = real_response(
                f"HTTP/1.1 {status} Found\r\nLocation: {self.URL}\r\nContent-Type: application/json\r\n"
                "Content-Length: 0\r\n\r\n".encode())
            last = real_response(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
            replies, requests = iter((first, last)), []

            class MemoryTransport(urllib.request.HTTPSHandler):
                def https_open(self, req, replies=replies, requests=requests):
                    requests.append(req)
                    response = next(replies)
                    response.geturl = lambda url=req.full_url: url
                    return response

            client = MetadataClient(throttle=Throttle(0), transport=build_metadata_opener(MemoryTransport()))
            with self.subTest(status=status):
                if status in (307, 308):
                    self.assertEqual(client.post_json(self.URL, {"condition": "DG/TJ08-2007"}), b"{}")
                    self.assertEqual(len(requests), 2)
                    self.assertEqual(requests[0].data, requests[1].data)
                    self.assertEqual(requests[1].get_method(), "POST")
                    self.assertEqual(requests[1].get_header("Content-type"), "application/json")
                    self.assertEqual(requests[1].get_header("Origin"), "https://bzjg.zjw.sh.gov.cn:9175")
                else:
                    with self.assertRaises(ComplianceDenied):
                        client.post_json(self.URL, {})
                    self.assertEqual(len(requests), 1)
                self.assertTrue(first.closed)

    def test_redirects_cannot_go_to_other_source_port_or_download(self):
        handler = metadata_redirect()
        req = urllib.request.Request(self.URL, data=b"{}", method="POST")
        for target in (M2MetadataClientTest.SAMR, self.URL.replace(":9175", ":443"),
                       self.URL.replace("/external/localStandard/list", "/api/files/webDownload")):
            response = FakeResponse(b"not read", url=self.URL)
            with self.subTest(target=target), self.assertRaises(ComplianceDenied):
                handler.http_error_302(req, response, 307, "Found", {"Location": target})
            self.assertEqual(response.read_sizes, [])
            self.assertTrue(response.closed)


class ChinaBuildingTransportTest(unittest.TestCase):
    SEARCH_URL = f"https://{CHINABUILDING_HOST}/zbooklib/search?SiteID=1&contentType=book&SearchKey=22G101-1"
    DETAIL_URL = f"https://{CHINABUILDING_HOST}/zbooklib/book/detail/show?SiteID=1&bookID=153493"
    CONCISE_URL = f"https://{CHINABUILDING_HOST}{CHINABUILDING_CONCISE_PATH}?SiteID=1&sublibID=2118&currentSublibInnerCode=0029"

    def setUp(self):
        mock.patch.object(net_metadata.time, "sleep").start()
        mock.patch("socket.create_connection", side_effect=AssertionError("不应真实联网")).start()
        self.addCleanup(mock.patch.stopall)

    def _opener_client(self, responder, *, probes=None):
        class MemoryHTTPS(urllib.request.HTTPSHandler):
            def https_open(self, req):
                resp = responder(req)
                final_url = getattr(resp, "_final_url", req.full_url)
                resp.geturl = lambda url=final_url: url
                return resp

        sink = probes.append if probes is not None else None
        return MetadataClient(
            throttle=Throttle(0),
            transport=build_metadata_opener(MemoryHTTPS()),
            probe_sink=sink,
        )

    @staticmethod
    def _http_html(body: bytes, *, status: int = 200, reason: str = "OK", extra_headers: str = ""):
        wire = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"{extra_headers}\r\n"
        ).encode("ascii") + body
        return real_response(wire)

    def test_approved_https_endpoints_work_and_record_probes(self):
        probes = []
        client = self._opener_client(lambda req: self._http_html(b"<html>ok</html>"), probes=probes)
        for url in (self.SEARCH_URL, self.DETAIL_URL, self.CONCISE_URL):
            with self.subTest(url=url):
                self.assertEqual(client.get(url), b"<html>ok</html>")
        self.assertEqual([p.outcome for p in probes], ["ok", "ok", "ok"])

    def test_unapproved_scheme_port_paths_and_methods_are_denied(self):
        probes = []
        client = self._opener_client(lambda req: self._http_html(b"<html>ok</html>"), probes=probes)
        for url in (
            self.SEARCH_URL.replace("https://", "http://"),
            f"https://{CHINABUILDING_HOST}:8443/zbooklib/search?SiteID=1",
            f"https://{CHINABUILDING_HOST}/zbooklib/advSearchResult?SearchKey=22G101-1",
            f"https://{CHINABUILDING_HOST}/zbooklib/book/download?bookID=153493",
            f"https://{CHINABUILDING_HOST}/a.pdf",
        ):
            with self.subTest(url=url), self.assertRaises(ComplianceDenied):
                client.get(url)
        with self.assertRaises(ComplianceDenied):
            client.post_form(self.SEARCH_URL, {"SearchKey": "22G101-1"})
        with self.assertRaises(ComplianceDenied):
            client.post_json(self.SEARCH_URL, {"SearchKey": "22G101-1"})
        with self.assertRaises(ComplianceDenied):
            BodyPolicy.default().check_url(self.DETAIL_URL)
        self.assertTrue(all(p.outcome == "blocked" for p in probes))

    def test_attachment_binary_and_cross_host_redirect_are_denied(self):
        client = self._opener_client(
            lambda req: self._http_html(b"%PDF-1.7", extra_headers="Content-Disposition: attachment\r\n")
        )
        with self.assertRaises(ComplianceDenied):
            client.get(self.DETAIL_URL)

        redirect_client = self._opener_client(
            lambda req: self._http_html(
                b"", status=302, reason="Found",
                extra_headers=f"Location: {M2MetadataClientTest.SAMR}\r\n",
            )
        )
        with self.assertRaises(ComplianceDenied):
            redirect_client.get(self.SEARCH_URL)

    def test_concise_catalogue_allows_3mb_up_to_8mb_while_search_and_detail_stay_2mb(self):
        probes = []
        three_mb = b"<html>" + b"a" * (3_000_000 - 13) + b"</html>"
        nine_mb = b"<html>" + b"b" * (8_100_000) + b"</html>"

        client = self._opener_client(lambda req: self._http_html(three_mb), probes=probes)
        self.assertEqual(len(client.get(self.CONCISE_URL)), 3_000_000)
        self.assertEqual(probes[-1].outcome, "ok")

        for url in (self.SEARCH_URL, self.DETAIL_URL):
            with self.subTest(url=url), self.assertRaises(NeedsHuman) as ctx:
                client.get(url)
            self.assertEqual(ctx.exception.exit, 50)
            self.assertEqual(probes[-1].outcome, "too_large")

        oversize_client = self._opener_client(lambda req: self._http_html(nine_mb), probes=probes)
        with self.assertRaises(NeedsHuman) as ctx:
            oversize_client.get(self.CONCISE_URL)
        self.assertEqual(ctx.exception.exit, 50)
        self.assertEqual(probes[-1].outcome, "too_large")

        # 从 search 重定向到 conciseCatalogueList 不允许放宽初始 2 MB 上限。
        def escalate(req):
            if "/search" in req.full_url:
                return self._http_html(
                    b"", status=302, reason="Found",
                    extra_headers=f"Location: {CHINABUILDING_CONCISE_PATH}?SiteID=1&sublibID=2118&currentSublibInnerCode=0029\r\n",
                )
            return self._http_html(three_mb)

        redirect_client = self._opener_client(escalate, probes=probes)
        with self.assertRaises(NeedsHuman):
            redirect_client.get(self.SEARCH_URL)
        self.assertEqual(probes[-1].outcome, "too_large")
        self.assertEqual(MetadataPolicy().max_bytes_for(self.CONCISE_URL), 8_000_000)
        self.assertEqual(MetadataPolicy().max_bytes_for(self.SEARCH_URL), 2_000_000)


if __name__ == "__main__":
    unittest.main()
