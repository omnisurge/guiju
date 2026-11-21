"""One redirect handler for both body and metadata policies."""

from __future__ import annotations

import http.client
import io
import urllib.parse
import urllib.request

from ..errors import ComplianceDenied, SourceUnreachable
from .block import BlockDetector
from .endpoints import SHANGHAI_HOST
from .headers import header_int
from .policy import MetadataPolicy


class RedirectGuard(urllib.request.HTTPRedirectHandler):
    """Validate every hop and throttle the target host before following it."""

    max_redirections = 5

    def __init__(self, policy, throttle, detector: BlockDetector | None = None) -> None:
        self.policy = policy
        self.throttle = throttle
        self.detector = detector or BlockDetector()

    def _validate(self, req, newurl: str) -> tuple[str, str]:
        target = urllib.parse.urljoin(req.full_url, newurl)
        host = self.policy.check_redirect(req, target)
        return target, host

    def http_error_302(self, req, fp, code, msg, headers):
        try:
            target = headers.get("Location") or headers.get("URI")
            if not target:
                raise SourceUnreachable(f"重定向未提供目标：{req.full_url}")
            target = urllib.parse.urljoin(req.full_url, target)
            self.policy.check_redirect(req, target)
            if isinstance(self.policy, MetadataPolicy):
                self.policy.check_headers(headers, req.full_url, redirect=True)
            if fp is not None:
                declared = header_int(fp.headers.get("Content-Length")) if hasattr(fp, "headers") else None
                head = fp.read(65536)
                if declared is not None and declared <= 65536 and len(head) < declared:
                    raise SourceUnreachable(f"重定向响应读取失败（响应截断）：{req.full_url}")
                self.detector.check(head, headers.get("Content-Type", ""),
                                    reject_binary=isinstance(self.policy, MetadataPolicy))
        except (OSError, http.client.HTTPException) as exc:
            raise SourceUnreachable(f"重定向响应读取失败：{req.full_url}") from exc
        finally:
            if fp is not None:
                fp.close()
        with io.BytesIO() as empty:
            return super().http_error_302(req, empty, code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target, host = self._validate(req, newurl)
        if (isinstance(self.policy, MetadataPolicy) and host == SHANGHAI_HOST
                and (req.get_method() != "POST" or code not in (307, 308))):
            raise ComplianceDenied("上海查询不跟随会丢失 JSON 条件的重定向")
        self.throttle.wait(host)
        if req.get_method() == "POST" and code in (307, 308):
            kept = {k: v for k, v in req.headers.items() if k.lower() != "content-length"}
            return urllib.request.Request(
                target, data=req.data, headers=kept, method="POST",
                origin_req_host=req.origin_req_host, unverifiable=True,
            )
        return super().redirect_request(req, fp, 307 if code == 308 else code, msg, headers, target)
