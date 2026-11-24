"""应用用例层（B4）。

只做编排：读取显式传入的上下文/端口，调用 domain / ports 能力，返回可投影的数据。
入口（CLI/MCP）负责创建 Store、currency、out_dir。迁移期部分旧入口仍允许省略
currency/store 的兼容兜底（status/resolve/cite-check），B4 完成时应删除这些兜底，
应用层不再读取 GUIJU_HOME 环境变量来隐式构造实现。
"""

from .context import AppContext

__all__ = ["AppContext"]
