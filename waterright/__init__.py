"""跨区域水权与生态预留台（仅标准库）。

层次划分：
- materials：资料层，负责登记资料的读取、校验与示例资料；
- rules：判定层，只做额度、区间、挤占与干旱分配的纯计算；
- storage：保存层，负责 SQLite 结构与 SQL；
- service：编排三层形成业务用例，由 app.py（页面/HTTP）调用。
"""

from .materials import DomainError
from .service import DeskService, seed_demo

Database = DeskService

__all__ = ["DeskService", "Database", "DomainError", "seed_demo"]
