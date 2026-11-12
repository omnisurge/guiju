"""最长前缀元数据路由；上海已确认编号由唯一官方源优先接管。"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

from .cache import CACHE_NAMESPACE
from .csres import CsresCurrency
from .fallback import FallbackCurrency
from .httpx import HttpClient
from .model import Currency


def has_code_prefix(code: str, prefix: str) -> bool:
    if not prefix or not code.startswith(prefix):
        return False
    rest = code[len(prefix):]
    return not rest or not ("A" <= rest[0] <= "Z" or "a" <= rest[0] <= "z")


@dataclass(frozen=True)
class Route:
    source: str
    prefixes: tuple[str, ...]
    factory: Callable[[], Currency] | None
    disabled: bool = False
    cache_namespace: str = ""
    normalizer: Callable[[str], str] | None = None
    code_filter: Callable[[str], bool] | None = None
    # hosts 声明本路由覆盖的查询端点；form_post=True 时这些 host 接受 POST 表单，其余走 GET。
    hosts: tuple[str, ...] = ()
    form_post: bool = False


class Registry:
    def __init__(self) -> None:
        self._routes: list[Route] = []

    def register(self, route: Route) -> None:
        self._routes.append(route)

    def _route(self, code: str) -> Route | None:
        # 不支持的零宽/控制字符不能把上海编号伪装后送进宽泛 DB 外源。
        if any(unicodedata.category(char).startswith("C") and not char.isspace() for char in code):
            return None
        normalized = code.strip().upper()
        best: Route | None = None
        longest = -1
        for route in self._routes:
            if route.disabled or route.factory is None:
                continue
            if route.code_filter is not None and not route.code_filter(code):
                continue
            candidate = route.normalizer(code) if route.normalizer is not None else normalized
            for prefix in route.prefixes:
                prefix = route.normalizer(prefix) if route.normalizer is not None else prefix.strip().upper()
                if len(prefix) > longest and has_code_prefix(candidate, prefix):
                    best, longest = route, len(prefix)
        return best

    def match(self, code: str) -> Currency | None:
        route = self._route(code)
        return route.factory() if route is not None and route.factory is not None else None

    def sources(self) -> list[str]:
        return [route.source for route in self._routes]

    def form_post_for_host(self, host: str) -> bool:
        # 端点级 Route（hosts 非空、prefixes=()、factory=None）声明 host 接受表单 POST；
        # 未注册 host 返回 False，调用方维持原 GET/拒发分支。
        return any(not route.disabled and route.form_post and host in route.hosts
                   for route in self._routes)

    def namespace_for(self, code: str, adapter: Currency) -> str:
        route = self._route(code)
        if route is None:
            raise ValueError("无路由不能建立缓存命名空间")
        identity = route.cache_namespace or getattr(adapter, "cache_namespace", "")
        if not identity:
            identity = f"{type(adapter).__module__}.{type(adapter).__qualname__}:{adapter.name}"
        return json.dumps([CACHE_NAMESPACE, route.source, route.prefixes, identity],
                          ensure_ascii=True, separators=(",", ":"))


def default_registry(client: HttpClient | None = None, *, cancelled: Callable[[], bool] | None = None,
                     admit: Callable[[str], None] | None = None) -> Registry:
    # 延迟导入只为避免兼容门面在启动旧 CLI 时装配所有原生源；这里没有网络动作。
    from .ccsn import CcsnCurrency
    from .dbba import DbbaCurrency
    from .jtst import JtstCurrency
    from .shzjw import (
        SHANGHAI_PREFIXES,
        ShzjwCurrency,
        is_shanghai_code,
        normalize_code,
        owns_shanghai_namespace,
    )
    from .stdsamr import StdSamrCurrency

    registry = Registry()
    registry.register(Route("csres", ("GB", "JGJ", "CJJ"), lambda: FallbackCurrency(
        "csres", CsresCurrency(client), CcsnCurrency(client), StdSamrCurrency(client), cancelled=cancelled, admit=admit)))
    registry.register(Route("csres", ("JTG",), lambda: FallbackCurrency(
        "csres", CsresCurrency(client), JtstCurrency(client), cancelled=cancelled, admit=admit)))
    registry.register(Route("csres", ("DB",), lambda: FallbackCurrency(
        "csres", CsresCurrency(client), DbbaCurrency(client), cancelled=cancelled, admit=admit),
        code_filter=lambda code: not owns_shanghai_namespace(code)))
    # "DB31" 单独列前缀：裸 DB31（不带 /T）也走上海路由，由 is_shanghai_code 细判；
    # 不并入 SHANGHAI_PREFIXES，避免改变该常量在 owns_shanghai_namespace 处的语义。
    registry.register(Route("shzjw", (*SHANGHAI_PREFIXES, "DB31"),
                            lambda: ShzjwCurrency(client, cancelled=cancelled),
                            normalizer=normalize_code, code_filter=is_shanghai_code))
    # 端点级路由：prefixes=()/factory=None，不参与编号匹配，只声明这些 host 接受表单 POST；
    # httpx.post_form 据此把关（替代已删除的 _FORM_HOSTS）。
    registry.register(Route("ccsn", (), None, hosts=("www.ccsn.org.cn",), form_post=True))
    registry.register(Route("dbba", (), None, hosts=("dbba.sacinfo.org.cn",), form_post=True))
    return registry
