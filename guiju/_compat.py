"""Python 3.10 兼容层。

只放标准库版本差异，不放业务逻辑。`StrEnum` 是 3.11 才进入 `enum` 的；
本项目声明支持 3.10，因此在这里给一个最小等价物（成员既是 str，`str()` 返回 value）。
"""

from __future__ import annotations

import sys
from enum import Enum

if sys.version_info >= (3, 11):  # pragma: no cover - 3.11+
    from enum import StrEnum
else:  # pragma: no cover - 3.10

    class StrEnum(str, Enum):  # type: ignore[no-redef]
        """3.10 上的最小等价物。"""

        def __str__(self) -> str:
            return str(self.value)


__all__ = ["StrEnum"]
