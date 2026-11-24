"""兼容 alias：cite 编排已迁到 guiju.application.cite。

旧测试与外部 import 的 render/resolve/_check_currency patch 点仍指向同一模块对象。
argparse 定义留在命令行层。
"""

import sys

from ...application import cite as _cite


def _add_parser(sub, parents) -> None:
    p = sub.add_parser("cite", parents=parents, help="生成标准引用串（含权威状态校验）")
    p.add_argument("ref", help="编号 / 规范名 / 公告ID / 公告URL")
    p.add_argument("clause", nargs="?", default=None, help="条款号，如 5.2.1")
    p.add_argument("--strict", action="store_true", help="存在未核实项或被替代时非零退出")
    p.add_argument("--offline", action="store_true", help="不访问时效服务")
    p.set_defaults(func=_cite.run)


_cite.add_parser = _add_parser
sys.modules[__name__] = _cite
