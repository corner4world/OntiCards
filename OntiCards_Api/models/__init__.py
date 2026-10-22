# -*- coding: utf-8 -*-
"""
@File: __init__.py
@Description: 模型统一注册入口 —— 自动扫描并导入本目录下所有模型模块
@Create: 2026-10-21

背景
----
本文件原先是空的，模型只能靠 controller 逐层 import 才会注册进 db.metadata。
后果是：某个模型文件只要没人 import，SQLAlchemy 就当这张表不存在。
（历史上出现过 5 张表凭空消失：governance_approval.py 声明 2 张、
governance_writeback_batch/column/grant.py 各 1 张，全都没进 metadata）

改为自动扫描后，模型文件只要放进 models/ 目录就会自动注册，
不需要记得去别处补 import —— 从机制上消除「漏注册」。

用法
----
两种写法都可以，原有代码无需改动：

    from models.users import User      # 原有写法，继续有效
    from models import User            # 新增：直接从包导入

对迁移的影响
------------
注册范围变全，但 Alembic 的 autogenerate 不受影响：
core/metadata_scope.py 的 include_object 按「init.sql 里出现过的表名」过滤，
不在 init.sql 里的表依然不会进入迁移脚本。

约定
----
1. 纯工具模块放进 _EXCLUDED_MODULES，不会被当作模型模块导入
2. 模型类不要在模块顶层相互 import（需要跨模型调用时，在函数体内延迟 import，
   参考 models/governance_report.py:258 的写法）
"""

import importlib
import pkgutil

from extensions.ext_database import db

# 不作为模型模块导入的文件（纯工具函数，不含 db.Model 子类）
_EXCLUDED_MODULES = {"utils"}

__all__ = ["db"]

for _info in pkgutil.iter_modules(__path__):          # noqa: F821
    _name = _info.name
    if _name.startswith("_") or _name in _EXCLUDED_MODULES:
        continue

    _mod = importlib.import_module("{}.{}".format(__name__, _name))

    # 把该模块内定义的模型类提升到包命名空间，支持 `from models import User`
    for _cls_name, _cls in list(vars(_mod).items()):
        if (
            isinstance(_cls, type)
            and issubclass(_cls, db.Model)
            and _cls is not db.Model
            and _cls.__module__ == _mod.__name__
            and not _cls_name.startswith("_")
        ):
            globals()[_cls_name] = _cls
            __all__.append(_cls_name)
