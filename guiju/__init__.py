"""规矩 guiju —— 以规范为准绳，让依据随手可取。

导入本包只暴露版本号，绝不在这里 import cli / sqlite（那会让 import 变成副作用）。
"""

from ._version import __version__

__all__ = ["__version__"]
