"""fix_20261112 回归：IDN 域名的 MCP 端点在构造期归一化为 punycode。

背景：DEFAULT_MCP_URL 曾用中文域名 guiju.守夜人.中国；http.client 写 Host 头按
latin-1 编码，非 ASCII 主机在第一次请求前必抛 UnicodeEncodeError（被吞成
SourceUnreachable），整个 GUIJU_CURRENCY=mcp 路径必死。修复：常量存 punycode，
同时 McpCurrency 构造器统一归一化，覆盖用户自定义 URL。
"""

from urllib.parse import urlsplit

from guiju.currency import DEFAULT_MCP_URL, McpCurrency, _idna_url


def test_default_url_is_punycode():
    assert DEFAULT_MCP_URL.isascii()
    assert urlsplit(DEFAULT_MCP_URL).hostname == "guiju.xn--gmqx6we1b.xn--fiqs8s"


def test_mcp_currency_normalizes_idn_host():
    cur = McpCurrency("https://guiju.守夜人.中国/mcp")
    assert cur.url == DEFAULT_MCP_URL
    assert cur.url.isascii()
    parts = urlsplit(cur.url)
    assert parts.hostname == "guiju.xn--gmqx6we1b.xn--fiqs8s"
    assert parts.scheme == "https"
    assert parts.path == "/mcp"


def test_idna_url_preserves_port_path_query():
    out = _idna_url("https://守夜人.中国:8443/mcp?x=1#frag")
    parts = urlsplit(out)
    assert parts.hostname == "xn--gmqx6we1b.xn--fiqs8s"
    assert parts.port == 8443
    assert parts.path == "/mcp"
    assert parts.query == "x=1"
    assert parts.fragment == "frag"


def test_idna_url_passes_through_ascii_and_ip():
    for url in (
        "http://127.0.0.1:18083/mcp",
        "https://example.com/mcp",
        DEFAULT_MCP_URL,
    ):
        assert _idna_url(url) == url
        assert McpCurrency(url).url == url


def test_idna_url_uppercase_and_userinfo():
    out = _idna_url("https://user:pw@GUIJU.守夜人.中国/mcp")
    parts = urlsplit(out)
    assert parts.username == "user"
    assert parts.password == "pw"
    assert parts.hostname == "guiju.xn--gmqx6we1b.xn--fiqs8s"
