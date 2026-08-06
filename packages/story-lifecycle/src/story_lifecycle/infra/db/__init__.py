"""DB 数据访问层子包（设计15 阶段B）。

历史：单文件 models.py 3111 行。拆分后 models.py 保留为门面（re-export），
``from ..infra.db import models as db`` 零改动可用。新代码可直接 import 子模块。
"""

from . import models as db

__all__ = ["db"]
