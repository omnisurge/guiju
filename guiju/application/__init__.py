"""应用用例层。

只做编排：读取入口显式传入的 AppContext / 端口（store、currency、sources、out_dir），
调用 domain / ports 能力，返回可投影的数据；不在本层隐式构造适配器或读取 GUIJU_HOME。
"""

from .context import AppContext

__all__ = ["AppContext"]
