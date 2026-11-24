"""兼容 alias：pull 编排已迁到 guiju.application.pull。

保留本模块名和全部旧符号（render/resolve/_pull_one/_batch 等），让旧测试与
外部 import 的 patch 点仍然作用于同一模块对象。argparse 定义留在命令行层。
"""

import sys

from ...application import pull as _pull


def _add_parser(sub, parents) -> None:
    p = sub.add_parser("pull", parents=parents, help="下载规范正文（单条或批量，幂等）")
    p.add_argument("ref", nargs="?", default=None, help="编号 / 规范名 / 公告ID / 公告URL")
    p.add_argument("--list", default=None, help="批量清单文件（每行一个 ref，- 表示 stdin）")
    p.add_argument("--out", default=None, help="下载目录（默认 ~/.guiju/library）")
    p.add_argument("--readable", action="store_true", help="用人类可读文件名（默认带 sha256 指纹；同名文件在就跳过、不校验内容）")
    p.add_argument("--force", action="store_true", help="已存在也重新下载")
    p.add_argument("--source", default="mohurd")
    p.set_defaults(func=_pull.run)


_pull.add_parser = _add_parser
sys.modules[__name__] = _pull
