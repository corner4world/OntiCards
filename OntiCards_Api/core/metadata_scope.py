"""
@File: metadata_scope.py
@Description: Alembic autogenerate 的表范围控制（当前阶段只认 init.sql 里声明过的表）
@Create: 2026-10-20

背景
----
models/ 下已经写了 7 张治理表（governance_tasks / governance_task_actions /
governance_shadow_tables ...），但它们还没进 init.sql。如果不加限制，
每次 `flask db migrate` 都会生成这 7 张表的 create_table，把真正关心的变更淹没掉。
本阶段的目标是「先把 init.sql 里的 28 张表三线闭环」，所以先按 init.sql 收口。

做法
----
不改 migrations/env.py。Flask-Migrate 生成的 env.py 里有：

    conf_args = current_app.extensions['migrate'].configure_args
    context.configure(..., **conf_args)

也就是说 `Migrate(app, db, **kwargs)` 的 kwargs 会被原样透传给
`context.configure()`。所以在 extensions/ext_migrate.py 里写：

    Migrate(app, db, include_object=include_object)

即可生效，env.py 保持原样。

include_object 只影响 autogenerate 的比较范围，不会改动 db.metadata，
因此不影响运行时的 ORM 查询。

切换
----
做「新增测试表」的迁移练习时，把下面的 SCOPE_ENABLED 改成 False，
否则新建的测试表不在 init.sql 里，`flask db migrate` 会看不见它。
（另一种做法是先把测试表写进 init.sql，更符合「init.sql 即事实」的原则。）
"""
import os
import re

# 是否启用「只比较 init.sql 中的表」。
# 做新增表的迁移练习时置为 False；练完记得改回来。
SCOPE_ENABLED = True

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INIT_SQL_PATH = os.path.join(_PROJECT_ROOT, "init.sql")

# 与 scripts/three_way_check.py 保持同一套解析规则：
# 本项目 init.sql 混合了 CREATE TABLE "public"."x" / CREATE TABLE IF NOT EXISTS "x" 等写法
_CREATE_TABLE_RE = re.compile(
    r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:"?public"?\s*\.\s*)?"?([A-Za-z0-9_]+)"?',
    re.IGNORECASE,
)

_cached_tables = None


def init_sql_tables():
    """解析 init.sql，返回其中声明的表名集合（结果缓存一次）。"""
    global _cached_tables
    if _cached_tables is None:
        try:
            with open(INIT_SQL_PATH, encoding="utf-8") as f:
                sql = f.read()
            _cached_tables = {m.group(1).lower() for m in _CREATE_TABLE_RE.finditer(sql)}
        except OSError:
            # 读不到就退化为「不做限制」，避免因为脚本缺失导致迁移失灵
            _cached_tables = set()
    return _cached_tables


def include_object(obj, name, type_, reflected, compare_to):
    """Alembic 的 include_object 钩子。

    返回 False 的对象不会参与 autogenerate 比较。

    判定分两个方向（关键）：
      - **删除方向**（reflected=True 且 compare_to is None）：
        「库里存在、metadata 里没有」→ 一律放行，生成 drop。
        不能按 init.sql 白名单过滤，否则「从 init.sql 删掉表名」这个动作
        会把这条表一并移出白名单，导致 drop_table 被静默吃掉，
        `flask db migrate` 永远返回 No changes。
      - **新增/修改方向**：只放行 init.sql 里声明过的表（白名单），
        挡住 governance_* 这类暂不纳管的表。

    其他对象（列/索引/约束/外键）：随所属表一起处理，这里统一放行，
    由表的 include_object 结果决定它们的可见性。
    """
    if not SCOPE_ENABLED:
        return True
    if type_ != "table":
        return True

    # ---- 删除方向：库里存在、模型里没有 ----
    # reflected=True 表示这个对象是从数据库反射出来的；
    # compare_to is None 表示 target metadata 里找不到对应物 → 这是要删掉的东西。
    if reflected and compare_to is None:
        return True

    allowed = init_sql_tables()
    if not allowed:                       # init.sql 没解析出来，不做限制
        return True
    return str(name).lower() in allowed
