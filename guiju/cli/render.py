"""兼容 alias：输出投影已上移到 guiju.report。

保留本模块名是为了不打断既有 CLI/测试 import；对象身份与 guiju.report 相同。
"""

import sys

from .. import report as _report

sys.modules[__name__] = _report
