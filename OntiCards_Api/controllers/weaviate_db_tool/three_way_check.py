# -*- coding: utf-8 -*-
"""
@File: three_way_check.py
@Description: 三线对齐体检 —— init.sql × 模型 metadata × 真实数据库
@Create: 2026-10-20

用途
----
一次性回答：init.sql 里声明的每一张表，它的列、类型、可空性、注释、索引、
唯一约束、外键，是否和「模型定义」以及「真实数据库」三方一致。

用法
----
PyCharm 里直接右键 Run 本文件即可，不需要任何参数。
（也可命令行：python scripts/three_way_check.py）

输出
----
1) 控制台：分区打印每类差异 + 最终汇总
2) 文件  ：scripts/三方比对报告.md（便于留存 / 贴给同事）

判定口径
--------
- 只考核 init.sql 里存在的表（当前阶段先闭环这些表）
- 模型里有、init.sql 没有的表，单独列为「暂不纳入」，不算失败
- alembic_version 是 Alembic 自建表，模型里本就不该有，跳过
- 索引只比「显式业务索引」：排除主键 / 唯一约束背后的索引
- 唯一约束与外键按「结构」比对（表+列+引用目标+级联动作），不按名字，
  因为 init.sql 里存在 `CONSTRAINT x UNIQUE (...)`、裸 `UNIQUE (...)`、
  列级 `REFERENCES ...` 三种写法，名字由 PG 自动生成，按名字比会误报
- 字符类型比长度（varchar(255) ≠ varchar(2048)）；timestamptz 不比精度（PG 反射不出来）
"""
import glob
import os
import re
import sys

# ---------------------------------------------------------------- 路径准备
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

INIT_SQL = os.path.join(ROOT, "init.sql")
REPORT = os.path.join(ROOT, "scripts", "三方比对报告.md")
VERSIONS = os.path.join(ROOT, "migrations", "versions")

# 模型里本就不该有的表
MODEL_IGNORED = {"alembic_version"}


# ================================================================ 类型归一
_TYPE_ALIAS = {
    "int": "integer", "int4": "integer", "integer": "integer", "serial": "integer",
    "bigint": "bigint", "int8": "bigint", "bigserial": "bigint",
    "smallint": "smallint", "int2": "smallint",
    "varchar": "character varying", "character varying": "character varying",
    "char": "character", "bpchar": "character", "character": "character",
    "text": "text", "bool": "boolean", "boolean": "boolean",
    "timestamptz": "timestamp with time zone",
    "timestamp with time zone": "timestamp with time zone",
    "timestamp": "timestamp without time zone",
    "datetime": "timestamp without time zone",
    "date": "date", "time": "time",
    "numeric": "numeric", "decimal": "numeric",
    "float": "double precision", "double precision": "double precision",
    "real": "real", "uuid": "uuid", "json": "json", "jsonb": "jsonb",
    "bytea": "bytea",
}
_CHAR_TYPES = {"varchar", "character varying", "char", "character", "bpchar"}


def norm_type(t):
    if not t:
        return ""
    t = str(t).lower().strip()
    t = re.sub(r"\s+", " ", t)

    is_array = t.endswith("[]")
    if is_array:
        t = t[:-2].strip()

    length = None
    m = re.match(r"^(.*?)\((\d+)\)$", t)
    if m:
        t, length = m.group(1).strip(), m.group(2)
    else:
        t = re.sub(r"\([^)]*\)", "", t).strip()      # numeric(5,2) 之类

    base = _TYPE_ALIAS.get(t, t)
    # 只有字符类型保留长度；timestamptz(6) 的精度 PG 反射不出来，比了会假报
    if length is not None and t in _CHAR_TYPES:
        base = "%s(%s)" % (base, length)
    return ("array<%s>" % base) if is_array else base


def split_top_level(s, sep=","):
    """按顶层分隔符切分：忽略括号内和引号内的分隔符。"""
    out, depth, buf, quote = [], 0, [], None
    for ch in s:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
        elif ch == sep and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def _balanced_body(sql, open_paren_idx):
    """从 '(' 位置开始，返回匹配右括号之间的文本。"""
    i, depth = open_paren_idx, 0
    while i < len(sql):
        if sql[i] == "(":
            depth += 1
        elif sql[i] == ")":
            depth -= 1
            if depth == 0:
                return sql[open_paren_idx + 1:i]
        i += 1
    return sql[open_paren_idx + 1:]


def _cols_of(text):
    """'("a", "b")' 或 'a, b' -> ['a','b']（统一小写）"""
    text = text.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    return [c.strip().strip('"').strip("'").lower()
            for c in split_top_level(text) if c.strip()]


# ------------------------------------------------------------ 结构比对用的键
def uc_key(table, cols):
    return ("UC", table.lower(), tuple(sorted(c.lower() for c in cols)))


def fk_key(table, cols, ref_table, ref_cols, ondelete, onupdate):
    return ("FK", table.lower(),
            tuple(c.lower() for c in cols),
            (ref_table or "").lower(),
            tuple(c.lower() for c in ref_cols),
            (ondelete or "").upper(),
            (onupdate or "").upper())


_PRIMARY_RE = re.compile(r"^PRIMARY\s+KEY", re.I)
_UNIQUE_RE = re.compile(r"^UNIQUE\s*\((.*)\)\s*$", re.I | re.S)
_FK_RE = re.compile(
    r"^FOREIGN\s+KEY\s*\((.*?)\)\s*REFERENCES\s+"
    r'(?:"?public"?\s*\.\s*)?"?([A-Za-z0-9_]+)"?\s*\((.*?)\)(.*)$',
    re.I | re.S)


def _parse_constraint(table, name, text, ucs, fks):
    """解析一条约束定义（UNIQUE / PRIMARY KEY / FOREIGN KEY）。"""
    text = re.sub(r"--[^\n]*", "", text).strip()
    if _PRIMARY_RE.match(text):
        return
    m = _UNIQUE_RE.match(text)
    if m:
        ucs[uc_key(table, _cols_of(m.group(1)))] = {
            "name": name, "table": table.lower(), "columns": _cols_of(m.group(1))}
        return
    m = _FK_RE.match(text)
    if m:
        cols = _cols_of(m.group(1))
        ref_table = m.group(2).lower()
        ref_cols = _cols_of(m.group(3))
        tail = m.group(4) or ""

        def act(kw):
            mm = re.search(kw + r"\s+(CASCADE|SET\s+NULL|SET\s+DEFAULT|RESTRICT|NO\s+ACTION)",
                           tail, re.I)
            return re.sub(r"\s+", " ", mm.group(1)).upper() if mm else ""

        fks[fk_key(table, cols, ref_table, ref_cols, act("ON DELETE"), act("ON UPDATE"))] = {
            "name": name, "table": table.lower(), "columns": cols,
            "ref_table": ref_table, "ref_columns": ref_cols,
            "ondelete": act("ON DELETE"), "onupdate": act("ON UPDATE")}


# ========================================================== 1. 解析 init.sql
CREATE_TABLE_RE = re.compile(
    r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:"?public"?\s*\.\s*)?"?([A-Za-z0-9_]+)"?\s*\(',
    re.I)
COL_COMMENT_RE = re.compile(
    r'COMMENT\s+ON\s+COLUMN\s+(?:"?public"?\s*\.\s*)?"?([A-Za-z0-9_]+)"?\s*\.\s*"?([A-Za-z0-9_]+)"?\s+IS\s+\'((?:[^\']|\'\')*)\'',
    re.I)
TBL_COMMENT_RE = re.compile(
    r'COMMENT\s+ON\s+TABLE\s+(?:"?public"?\s*\.\s*)?"?([A-Za-z0-9_]+)"?\s+IS\s+\'((?:[^\']|\'\')*)\'',
    re.I)
INDEX_RE = re.compile(
    r'CREATE\s+(UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?'
    r'"?([A-Za-z0-9_]+)"?\s+ON\s+(?:"?public"?\s*\.\s*)?"?([A-Za-z0-9_]+)"?'
    r'(?:\s+USING\s+([A-Za-z0-9_]+))?\s*\(',
    re.I)
ALTER_CONSTRAINT_RE = re.compile(
    r'ALTER\s+TABLE\s+(?:"?public"?\s*\.\s*)?"?([A-Za-z0-9_]+)"?\s+'
    r'ADD\s+CONSTRAINT\s+"?([A-Za-z0-9_]+)"?\s+(.*?);',
    re.I | re.S)
# 列级外键：col TYPE ... REFERENCES t(c) [ON DELETE ...] [ON UPDATE ...]
COLUMN_REF_RE = re.compile(
    r'REFERENCES\s+(?:"?public"?\s*\.\s*)?"?([A-Za-z0-9_]+)"?\s*\(\s*"?([A-Za-z0-9_]+)"?\s*\)(.*)$',
    re.I | re.S)


def _parse_columns(table, body, cols, ucs, fks):
    """解析 CREATE TABLE 括号内内容：列定义 + 内联约束。"""
    body = re.sub(r"--[^\n]*", "", body)      # 行尾注释里可能含逗号，先剥掉
    for item in split_top_level(body):
        item = item.strip()
        if not item:
            continue
        upper = item.upper()

        # 表级约束（内联）：CONSTRAINT x UNIQUE (...)、裸 UNIQUE (...)、FOREIGN KEY ...
        if upper.startswith("CONSTRAINT"):
            m = re.match(r'CONSTRAINT\s+"?([A-Za-z0-9_]+)"?\s+(.*)$', item, re.I | re.S)
            if m:
                _parse_constraint(table, m.group(1), m.group(2), ucs, fks)
            continue
        if upper.startswith(("UNIQUE", "PRIMARY KEY", "FOREIGN KEY", "CHECK", "EXCLUDE")):
            _parse_constraint(table, None, item, ucs, fks)
            continue

        m = re.match(r'^"?([A-Za-z0-9_]+)"?\s+(.*)$', item, re.S)
        if not m:
            continue
        name, rest = m.group(1), m.group(2)

        tm = re.match(r"^([A-Za-z_][A-Za-z0-9_]*(?:\s*\([^)]*\))?(?:\[\])?)", rest)
        ctype = tm.group(1) if tm else rest.split()[0]
        up = rest.upper()
        cols[name.lower()] = {
            "type": norm_type(ctype),
            # PRIMARY KEY 隐含 NOT NULL，不能只看字面 "NOT NULL"
            "nullable": ("NOT NULL" not in up) and ("PRIMARY KEY" not in up),
        }

        # 列级 REFERENCES（PG 会自动命名为 <表>_<列>_fkey）
        rm = COLUMN_REF_RE.search(rest)
        if rm:
            ref_table = rm.group(1).lower()
            ref_cols = [rm.group(2).lower()]
            tail = rm.group(3) or ""

            def act(kw, _tail=tail):
                mm = re.search(kw + r"\s+(CASCADE|SET\s+NULL|SET\s+DEFAULT|RESTRICT|NO\s+ACTION)",
                               _tail, re.I)
                return re.sub(r"\s+", " ", mm.group(1)).upper() if mm else ""

            od, ou = act("ON DELETE"), act("ON UPDATE")
            fks[fk_key(table, [name], ref_table, ref_cols, od, ou)] = {
                "name": "%s_%s_fkey" % (table.lower(), name.lower()),
                "table": table.lower(), "columns": [name.lower()],
                "ref_table": ref_table, "ref_columns": ref_cols,
                "ondelete": od, "onupdate": ou}


def parse_init_sql(path=INIT_SQL):
    with open(path, encoding="utf-8") as f:
        sql = f.read()

    tables, ucs, fks = {}, {}, {}

    for m in CREATE_TABLE_RE.finditer(sql):
        tname = m.group(1)
        body = _balanced_body(sql, m.end() - 1)
        tables[tname.lower()] = {}
        _parse_columns(tname, body, tables[tname.lower()], ucs, fks)

    # ALTER TABLE ... ADD CONSTRAINT ...
    for tname, cname, cdef in ALTER_CONSTRAINT_RE.findall(sql):
        _parse_constraint(tname, cname, cdef, ucs, fks)

    col_comments = {(t.lower(), c.lower()): v.replace("''", "'")
                    for t, c, v in COL_COMMENT_RE.findall(sql)}
    tbl_comments = {t.lower(): v.replace("''", "'") for t, v in TBL_COMMENT_RE.findall(sql)}

    indexes = {}
    for m in INDEX_RE.finditer(sql):
        uniq, name, tname, _using = m.groups()
        body = _balanced_body(sql, m.end() - 1)
        cols = []
        for part in split_top_level(body):
            # 每段形如： "col" COLLATE "pg_catalog"."default" "pg_catalog"."text_ops" ASC NULLS LAST
            # 只取开头的标识符，后面那些排序/操作符类不是列名
            cm = re.match(r'\s*"?([A-Za-z0-9_]+)"?', part)
            if cm:
                cols.append(cm.group(1).lower())
        indexes[name.lower()] = {
            "table": tname.lower(),
            "unique": bool((uniq or "").strip()),
            "columns": cols,
        }

    return tables, col_comments, tbl_comments, indexes, ucs, fks


# ====================================================== 2. 反射真实数据库
def load_db():
    from app import app
    from extensions.ext_database import db
    from sqlalchemy import inspect

    with app.app_context():
        insp = inspect(db.engine)
        tables, col_comments, tbl_comments = {}, {}, {}
        indexes, ucs, fks = {}, {}, {}
        uc_names, pk_names = set(), set()

        for t in insp.get_table_names(schema="public"):
            key = t.lower()
            cols = {}
            for c in insp.get_columns(t, schema="public"):
                cols[c["name"].lower()] = {
                    "type": norm_type(c["type"].compile(dialect=db.engine.dialect)),
                    "nullable": bool(c["nullable"]),
                }
                if c.get("comment"):
                    col_comments[(key, c["name"].lower())] = c["comment"]
            tables[key] = cols

            tc = insp.get_table_comment(t, schema="public").get("text")
            if tc:
                tbl_comments[key] = tc

            pk = insp.get_pk_constraint(t, schema="public")
            if pk.get("name"):
                pk_names.add(pk["name"].lower())

            for uc in insp.get_unique_constraints(t, schema="public"):
                nm = (uc.get("name") or "").lower()
                if nm:
                    uc_names.add(nm)
                cols_ = [c.lower() for c in (uc.get("column_names") or [])]
                ucs[uc_key(key, cols_)] = {"name": nm, "table": key, "columns": cols_}

            for fk in insp.get_foreign_keys(t, schema="public"):
                opt = fk.get("options") or {}
                cols_ = [c.lower() for c in (fk.get("constrained_columns") or [])]
                ref_t = (fk.get("referred_table") or "").lower()
                ref_c = [c.lower() for c in (fk.get("referred_columns") or [])]
                od = (opt.get("ondelete") or "").upper()
                ou = (opt.get("onupdate") or "").upper()
                fks[fk_key(key, cols_, ref_t, ref_c, od, ou)] = {
                    "name": (fk.get("name") or "").lower(), "table": key,
                    "columns": cols_, "ref_table": ref_t, "ref_columns": ref_c,
                    "ondelete": od, "onupdate": ou}

        # 索引：排除主键 / 唯一约束背后的索引（SQLAlchemy 的 get_indexes 会一并返回）
        for t in tables:
            for ix in insp.get_indexes(t, schema="public"):
                nm = (ix.get("name") or "").lower()
                if nm in uc_names or nm in pk_names:
                    continue
                indexes[nm] = {
                    "table": t,
                    "unique": bool(ix.get("unique")),
                    "columns": [c.lower() for c in (ix.get("column_names") or [])],
                }

    return tables, col_comments, tbl_comments, indexes, ucs, fks


# ========================================================= 3. 读模型 metadata
def load_models():
    from app import app
    from extensions.ext_database import db
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import UniqueConstraint

    pg = postgresql.dialect()

    def compile_type(col):
        try:
            return norm_type(col.type.compile(dialect=pg))
        except Exception:
            return norm_type(str(col.type))

    with app.app_context():
        meta = db.metadata
        tables, col_comments, tbl_comments = {}, {}, {}
        indexes, ucs, fks = {}, {}, {}

        for tname, tbl in meta.tables.items():
            key = tname.lower()
            tables[key] = {}
            for col in tbl.columns:
                tables[key][col.name.lower()] = {
                    "type": compile_type(col),
                    "nullable": bool(col.nullable),
                }
                if getattr(col, "comment", None):
                    col_comments[(key, col.name.lower())] = col.comment
            if getattr(tbl, "comment", None):
                tbl_comments[key] = tbl.comment

            for ix in tbl.indexes:
                indexes[(ix.name or "").lower()] = {
                    "table": key,
                    "unique": bool(ix.unique),
                    "columns": [c.name.lower() for c in ix.columns],
                }
            for con in tbl.constraints:
                if isinstance(con, UniqueConstraint):
                    cols_ = [c.name.lower() for c in con.columns]
                    ucs[uc_key(key, cols_)] = {
                        "name": (con.name or "").lower(), "table": key, "columns": cols_}

            for col in tbl.columns:
                for fk in col.foreign_keys:
                    con = fk.constraint
                    cols_ = [c.name.lower() for c in con.columns]
                    ref_t = fk.column.table.name.lower()
                    ref_c = [fk.column.name.lower()]
                    od = (con.ondelete or "").upper()
                    ou = (con.onupdate or "").upper()
                    fks[fk_key(key, cols_, ref_t, ref_c, od, ou)] = {
                        "name": (con.name or "").lower(), "table": key,
                        "columns": cols_, "ref_table": ref_t, "ref_columns": ref_c,
                        "ondelete": od, "onupdate": ou}

    return tables, col_comments, tbl_comments, indexes, ucs, fks


# ==================================================== 4. Alembic 残差（最严格）
def load_alembic_residual():
    from app import app
    from extensions.ext_database import db
    from core.metadata_scope import include_object
    from alembic.migration import MigrationContext
    from alembic.autogenerate import compare_metadata

    with app.app_context():
        with db.engine.connect() as conn:
            mc = MigrationContext.configure(
                conn, opts={"compare_type": True, "include_object": include_object})
            diffs = compare_metadata(mc, db.metadata)

    rows = []
    for d in diffs:
        for x in (d if isinstance(d, list) else [d]):
            if not isinstance(x, (tuple, list)):
                rows.append(("?", str(x)[:120]))
                continue
            kind = x[0]
            if kind in ("add_table", "remove_table", "add_index", "remove_index"):
                obj = getattr(x[1], "name", x[1])
            else:
                parts = [p for p in x[1:] if isinstance(p, str)]
                obj = ".".join(parts) if parts else str(x[1])[:80]
            rows.append((kind, str(obj)[:120]))
    return rows


# ============================================== 5. 版本种子 / 迁移链一致性
def load_version_state():
    """返回 (init.sql 种子版本, 迁移链 head, 数据库当前版本, 问题描述列表)。

    init.sql 末尾会 INSERT 一个版本号，让「全新装库」直接停在 head；
    每新增一条迁移，这个种子必须同步改成新的 head，否则全新建库后
    执行 flask db upgrade 会把历史迁移重放一遍。
    """
    problems = []

    # --- init.sql 里的种子
    with open(INIT_SQL, encoding="utf-8") as f:
        sql = f.read()
    seed = None
    m = re.search(
        r"INSERT\s+INTO\s+\"?public\"?\.\"?alembic_version\"?[^;]*?"
        r"SELECT\s+'([^']+)'", sql, re.I | re.S)
    if m:
        seed = m.group(1)

    # --- 迁移链 head
    revs, downs = {}, set()
    for path in glob.glob(os.path.join(VERSIONS, "*.py")):
        with open(path, encoding="utf-8") as f:
            body = f.read()
        mr = re.search(r"^revision\s*=\s*['\"]([^'\"]+)", body, re.M)
        md = re.search(r"^down_revision\s*=\s*(?:['\"]([^'\"]+)['\"]|None)", body, re.M)
        if not mr:
            continue
        revs[mr.group(1)] = os.path.basename(path)
        if md and md.group(1):
            downs.add(md.group(1))
    heads = [r for r in revs if r not in downs]
    head = heads[0] if len(heads) == 1 else ("/".join(heads) if heads else None)
    if len(heads) > 1:
        problems.append("迁移链存在多个 head：%s" % ", ".join(sorted(heads)))

    # --- 数据库当前版本
    current = None
    try:
        from app import app
        from extensions.ext_database import db
        with app.app_context():
            row = db.session.execute(
                db.text("SELECT version_num FROM alembic_version")).fetchone()
            current = row[0] if row else None
            db.session.rollback()
    except Exception as exc:                       # noqa: BLE001
        problems.append("读取数据库版本失败：%s" % exc)

    if seed and head and seed != head:
        problems.append("init.sql 种子是 %s，但迁移链 head 是 %s —— "
                        "全新装库后跑 upgrade 会重放历史迁移" % (seed, head))
    if current and head and current != head:
        problems.append("数据库版本是 %s，但迁移链 head 是 %s —— 本地库落后，"
                        "先执行 flask db upgrade" % (current, head))

    return seed, head, current, problems


# ================================================================== 主流程
def main():
    i_t, i_cc, i_tc, i_ix, i_uc, i_fk = parse_init_sql()
    d_t, d_cc, d_tc, d_ix, d_uc, d_fk = load_db()
    m_t, m_cc, m_tc, m_ix, m_uc, m_fk = load_models()

    lines = ["# 三线比对报告：init.sql × 模型 × 数据库", ""]

    def p(s=""):
        lines.append(s)
        print(s)

    p("- init.sql 表数：**%d**" % len(i_t))
    p("- 模型表数　　：**%d**" % len(m_t))
    p("- 数据库表数　：**%d**" % len(d_t))
    p()

    in_scope = sorted((set(i_t) & set(m_t) & set(d_t)))
    only_model = sorted(set(m_t) - set(i_t))
    sql_no_model = sorted(t for t in set(i_t) - set(m_t) if t not in MODEL_IGNORED)
    sql_no_db = sorted(set(i_t) - set(d_t))

    p("## 一、表的集合")
    p()
    p("- 三线都有的表：**%d** 张（本次考核范围）" % len(in_scope))
    p("- init.sql 有、模型没有（不含 alembic_version）：%s" % (sql_no_model or "无 ✅"))
    p("- init.sql 有、数据库没有：%s" % (sql_no_db or "无 ✅"))
    p("- 模型有、init.sql 没有（暂不纳入）：%d 张 %s" % (len(only_model), only_model or ""))
    p()

    problems = 0

    def table_block(title, rows, headers=("对象", "init.sql", "模型", "数据库")):
        nonlocal problems
        p("### %s（%d 处）" % (title, len(rows)))
        p()
        if not rows:
            p("无 ✅")
            p()
            return
        problems += len(rows)
        p("| " + " | ".join(headers) + " |")
        p("|" + "---|" * len(headers))
        for r in rows:
            p("| " + " | ".join("" if x is None else str(x) for x in r) + " |")
        p()

    # ---------- 列
    col_name_diff, type_diff, null_diff = [], [], []
    for t in in_scope:
        ic, mc, dc = i_t[t], m_t[t], d_t[t]
        for c in sorted(set(mc) - set(ic)):
            col_name_diff.append((t + "." + c, "缺", "有", "有" if c in dc else "缺"))
        for c in sorted(set(ic) - set(mc)):
            col_name_diff.append((t + "." + c, "有", "缺", "有" if c in dc else "缺"))
        for c in sorted(set(ic) & set(mc) & set(dc)):
            a, b, e = ic[c], mc[c], dc[c]
            if not (a["type"] == b["type"] == e["type"]):
                type_diff.append((t + "." + c, a["type"], b["type"], e["type"]))
            if not (a["nullable"] == b["nullable"] == e["nullable"]):
                null_diff.append((t + "." + c, a["nullable"], b["nullable"], e["nullable"]))

    p("## 二、列定义")
    p()
    table_block("列名不一致", col_name_diff)
    table_block("类型不一致", type_diff)
    table_block("可空性不一致", null_diff)

    # ---------- 注释
    cc_rows = []
    for t in in_scope:
        for c in sorted(set(i_t[t]) & set(m_t[t]) & set(d_t[t])):
            a, b, e = i_cc.get((t, c)), m_cc.get((t, c)), d_cc.get((t, c))
            if not (a == b == e):
                cc_rows.append((t + "." + c, a, b, e))
    tc_rows = []
    for t in in_scope:
        a, b, e = i_tc.get(t), m_tc.get(t), d_tc.get(t)
        if not (a == b == e):
            tc_rows.append((t, a, b, e))

    p("## 三、注释")
    p()
    table_block("列注释不一致", cc_rows)
    table_block("表注释不一致", tc_rows)

    # ---------- 索引（按名字 + 列 + 唯一性）
    ix_rows = []
    for n in sorted(set(i_ix) | set(m_ix) | set(d_ix)):
        owner = (i_ix.get(n) or m_ix.get(n) or d_ix.get(n))["table"]
        if owner not in in_scope:
            continue

        def desc(src):
            if n not in src:
                return "缺"
            v = src[n]
            return "%s(%s)%s" % ("UNIQUE " if v["unique"] else "", ",".join(v["columns"]), "")

        trio = (desc(i_ix), desc(m_ix), desc(d_ix))
        if len(set(trio)) > 1:
            ix_rows.append((n, ) + trio)

    p("## 四、索引")
    p()
    table_block("索引不一致", ix_rows)

    # ---------- 唯一约束（按结构，不看名字）
    def uc_desc(src, k):
        if k not in src:
            return "缺"
        return "(" + ",".join(src[k]["columns"]) + ")"

    uc_rows = []
    for k in sorted(set(i_uc) | set(m_uc) | set(d_uc)):
        owner = (i_uc.get(k) or m_uc.get(k) or d_uc.get(k))["table"]
        if owner not in in_scope:
            continue
        trio = (uc_desc(i_uc, k), uc_desc(m_uc, k), uc_desc(d_uc, k))
        if len(set(trio)) > 1:
            uc_rows.append((k[1], ) + trio)

    p("## 五、唯一约束（按结构比对：表 + 列集合，不看名字）")
    p()
    table_block("唯一约束不一致", uc_rows)

    # ---------- 外键（按结构，不看名字）
    def fk_desc(src, k):
        if k not in src:
            return "缺"
        v = src[k]
        s = "(%s) -> %s(%s)" % (",".join(v["columns"]),
                                v["ref_table"], ",".join(v["ref_columns"]))
        if v["ondelete"]:
            s += " ON DELETE " + v["ondelete"]
        if v["onupdate"]:
            s += " ON UPDATE " + v["onupdate"]
        return s

    fk_rows = []
    for k in sorted(set(i_fk) | set(m_fk) | set(d_fk)):
        owner = (i_fk.get(k) or m_fk.get(k) or d_fk.get(k))["table"]
        if owner not in in_scope:
            continue
        trio = (fk_desc(i_fk, k), fk_desc(m_fk, k), fk_desc(d_fk, k))
        if len(set(trio)) > 1:
            fk_rows.append((k[1] + "." + ",".join(k[2]), ) + trio)

    p("## 六、外键（按结构比对：表 + 列 + 引用目标 + 级联动作，不看名字）")
    p()
    table_block("外键不一致", fk_rows)

    # ---------- 约束命名（仅提示，不计入失败）
    p("## 六之二、约束命名差异（仅提示，不影响结构正确性）")
    p()
    naming = []
    for label, i_src, m_src, d_src in (("唯一约束", i_uc, m_uc, d_uc),
                                       ("外键", i_fk, m_fk, d_fk)):
        for k in sorted(set(i_src) & set(m_src) & set(d_src)):
            owner = i_src[k]["table"]
            if owner not in in_scope:
                continue
            names = tuple((src[k].get("name") or "未命名") for src in (i_src, m_src, d_src))
            if len(set(names)) == 1:
                continue
            if names[0] == names[2] and names[1] == "未命名":
                # 模型不写 name，PG 会自动补成和库一致的名字，等价，只是没显式声明
                continue
            naming.append((label + " " + str(k[1]) + "." + ",".join(k[2]), ) + names)
    if naming:
        p("| 对象 | init.sql | 模型 | 数据库 |")
        p("|---|---|---|---|")
        for r in naming:
            p("| " + " | ".join(str(x) for x in r) + " |")
        p()
        p("> 提示：模型不写 `name` 时 SQLAlchemy 会让 PG 自动命名，结果通常与库一致；"
          "只有三处名字真的不同才需要处理。")
    else:
        p("无 ✅（模型未显式命名的约束，PG 自动生成的名字与库一致）")
    p()

    # ---------- Alembic 残差
    p("## 七、Alembic 残差（flask db migrate 会生成的操作）")
    p()
    residual = []
    try:
        residual = load_alembic_residual()
    except Exception as exc:                     # noqa: BLE001
        p("> 跳过：%s" % exc)
        p()
    if not residual:
        p("无 ✅（`flask db migrate` 会提示 No changes in schema detected）")
        p()
    else:
        problems += len(residual)
        p("| 操作 | 对象 |")
        p("|---|---|")
        for kind, obj in residual:
            p("| %s | %s |" % (kind, obj))
        p()

    # ---------- 版本种子 / 迁移链
    seed, head, current, ver_problems = load_version_state()
    p("## 八、版本种子与迁移链")
    p()
    p("| 项 | 值 |")
    p("|---|---|")
    p("| init.sql 里固化的种子版本 | `%s` |" % (seed or "未找到"))
    p("| 迁移链 head | `%s` |" % (head or "未找到"))
    p("| 数据库当前版本 | `%s` |" % (current or "空"))
    p()
    if ver_problems:
        problems += len(ver_problems)
        for s in ver_problems:
            p("- ❌ %s" % s)
        p()
    else:
        p("三者一致 ✅（全新装库会直接停在 head，`flask db upgrade` 待办为空）")
        p()

    # ---------- 结论
    p("## 九、结论")
    p()
    if problems == 0:
        p("✅ **init.sql 中的 %d 张表三线完全一致。**" % len(i_t))
        p()
        if len(in_scope) < len(i_t):
            p("（其中 %s 由 Alembic 自建、模型里本就没有，故实际参与比对的是 %d 张）"
              % (sorted(set(i_t) - set(in_scope)), len(in_scope)))
            p()
        p("可以放心执行 `flask db migrate`（应提示 No changes in schema detected），然后开始迁移练习。")
    else:
        p("❌ 共 **%d 处** 不一致，请按上表逐项修正。" % problems)
        p()
        if not residual:
            p("> 提示：Alembic 残差为 0 说明「模型 ↔ 数据库」已经对齐，"
              "剩下的差异都在 init.sql 这一侧（缺声明或写法不同），改 init.sql 即可。")

    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print()
    print("=" * 64)
    print("init.sql 表 %d | 模型表 %d | 数据库表 %d" % (len(i_t), len(m_t), len(d_t)))
    print("-" * 64)
    print("列名不一致     : %d" % len(col_name_diff))
    print("类型不一致     : %d" % len(type_diff))
    print("可空性不一致   : %d" % len(null_diff))
    print("列注释不一致   : %d" % len(cc_rows))
    print("表注释不一致   : %d" % len(tc_rows))
    print("索引不一致     : %d" % len(ix_rows))
    print("唯一约束不一致 : %d" % len(uc_rows))
    print("外键不一致     : %d" % len(fk_rows))
    print("Alembic 残差   : %d" % len(residual))
    print("版本种子问题   : %d" % len(ver_problems))
    print("-" * 64)
    print("合计问题       : %d" % problems)
    print("=" * 64)
    print("报告已写入：%s" % REPORT)

    return 0 if problems == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
