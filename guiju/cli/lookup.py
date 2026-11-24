"""兼容 shim：B4 起解析编排在 guiju.application.resolve。

保留此模块是为了不打断既有 CLI 命令与外部测试的 import；1.2.0 稳定版前再评估删除。
不要在这里新增业务逻辑。
"""

import sys

from ..application import resolve as _resolve

# 真 alias 到同一模块对象：旧代码 patch "guiju.cli.lookup._by_code" 时，
# 修改的就是 application.resolve 的 globals，不会再出现"patch 静默失效"。
sys.modules[__name__] = _resolve
