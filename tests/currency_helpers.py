"""原生时效测试专用内存响应：不启动 HTTP 服务，不连接 socket。"""

import http.client
import io

URL = "http://www.csres.com/s.jsp?keyword=GB50011&pageNum=1"


class FakeResponse(io.BytesIO):
    def __init__(self, body=b"<html></html>", *, status=200, url=URL, headers=None):
        super().__init__(body)
        self.status = status
        self.url = url
        self.headers = {"Content-Type": "text/html; charset=gbk"} if headers is None else headers
        self.read_sizes = []

    def geturl(self):
        return self.url

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


def real_response(wire):
    """使用标准库真实 HTTPResponse，保留 read(amt) / length / chunked 语义。"""
    class MemorySocket:
        def makefile(self, mode):
            return io.BytesIO(wire)

    resp = http.client.HTTPResponse(MemorySocket())
    resp.begin()
    resp.msg = resp.reason  # 与 urllib.do_open 一致；HTTPResponse 默认 msg 是 headers。
    resp.geturl = lambda: URL
    return resp
