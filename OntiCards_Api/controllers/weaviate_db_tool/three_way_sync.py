# -*- coding: utf-8 -*-
"""
@File: three_way_sync.py
@Description: 三线对齐「修复」—— 让 init.sql / 模型 / 数据库真正一致
@Create: 2026-10-20

和 three_way_check.py 的关系
----------------------------
- three_way_check.py ：只读体检，列出差异
- three_way_sync.py  ：按基准把差异真正修掉（改模型源码 / 补 init.sql / 刷数据库）

基准
----
默认以 **init.sql** 为基准（--from init），把模型和数据库补齐。
也支持 --from model：把模型上的注释/索引写回 init.sql 并刷到数据库。

用法（PyCharm 直接右键 Run，无需参数 = 预览）
---------------------------------------------
    python scripts/three_way_sync.py                  # 预览，不写任何文件
    python scripts/three_way_sync.py --apply          # 真正落盘
    python scripts/three_way_sync.py --apply --target model   # 只改模型
    python scripts/three_way_sync.py --apply --target sql     # 只补 init.sql
    python scripts/three_way_sync.py --apply --target db      # 只刷数据库
    python scripts/three_way_sync.py --from model --apply     # 反向：以模型为基准
    python scripts/three_way_sync.py --prune --apply          # 连「多余的一方」一起删（谨慎）

安全边界（重要）
----------------
默认只做**幂等且安全**的操作：
    模型：加/改 comment、加 Index、加 UniqueConstraint、补外键 name 与级联、对齐类型声明
    SQL ：追加缺失的 COMMENT ON / CREATE INDEX IF NOT EXISTS
    库  ：COMMENT ON、CREATE INDEX IF NOT EXISTS、不存在的约束才 ADD

以下操作**默认不执行**，只写进 scripts/生成的修复SQL.sql 供人工确认：
    DROP INDEX / DROP CONSTRAINT（需 --prune）
    ALTER COLUMN TYPE（可能隐式转换失败或丢精度，需配合 USING）
    SET / DROP NOT NULL（可能拒绝现有脏数据）

输出
----
1) 控制台：每项改动逐条打印
2) scripts/三线同步报告.md
3) scripts/生成的修复SQL.sql（含「待人工确认」部分）
"""
import argparse
import os
import re
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
for _p in (ROOT, SCRIPTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.chdir(ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

INIT_SQL_PATH = os.path.join(ROOT, "init.sql")
MODELS_DIR = os.path.join(ROOT, "models")
REPORT_MD = os.path.join(SCRIPTS, "三线同步报告.md")
SQL_OUT = os.path.join(SCRIPTS, "生成的修复SQL.sql")

MODEL_IGNORED = {"alembic_version"}


# ================================================================ 源码工具
def scan_balanced(src, i, open_ch="(", close_ch=")"):
    """从 open_ch 所在位置起，返回匹配的 close_ch 下标；找不到返回 -1。"""
    depth, quote, n = 0, None, len(src)
    while i < n:
        ch = src[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def py_str(s):
    """生成 Python 字符串字面量，中文友好，避免引号冲突。"""
    s = str(s)
    if "'" in s and '"' not in s:
        return '"' + s + '"'
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def sql_str(s):
    """生成 SQL 字符串字面量（单引号转义为两个单引号）。"""
    return "'" + str(s).replace("'", "''") + "'"


def q(name):
    return '"%s"' % name


# ============================================================ 模型文件定位
class ModelFile(object):
    """一个模型文件的读写包装：所有修改先收集为 (start, end, text)，最后统一应用。"""

    def __init__(self, path):
        self.path = path
        # newline="" 保持原样读写，避免把 CRLF 文件整体改成 LF 污染 git diff
        self.src = open(path, encoding="utf-8", newline="").read()
        self._fixed = self.src
        self.nl = "\r\n" if "\r\n" in self.src else "\n"
        self.edits = []          # [(start, end, text, desc)]

    # ---------- 编辑收集
    def replace(self, start, end, text, desc):
        self.edits.append((start, end, text, desc))

    def insert(self, pos, text, desc):
        self.edits.append((pos, pos, text, desc))

    # ---------- 应用
    def apply(self):
        if not self.edits:
            return False
        src = self._fixed
        for start, end, text, _d in sorted(self.edits, key=lambda x: (-x[0], x[1])):
            src = src[:start] + text + src[end:]
        with open(self.path, "w", encoding="utf-8", newline="") as f:
            f.write(src)
        self.src = src
        self._fixed = src
        return True

    # ---------- 定位
    def class_range(self, table):
        """返回该表所属类体的 [start, end)（按 __tablename__ 定位）。"""
        m = re.search(
            r'(?m)^(?P<ind>[ \t]*)__tablename__\s*=\s*["\']%s["\']' % re.escape(table),
            self.src)
        if not m:
            return None
        start = m.start()
        # 类边界：向上找最近的 class 行，向下找下一个 class 行
        cls = None
        for c in re.finditer(r"(?m)^class\s+", self.src):
            if c.start() <= start:
                cls = c.start()
            else:
                end = c.start()
                break
        else:
            end = len(self.src)
        if cls is None:
            return None
        return {"start": cls, "end": end, "indent": m.group("ind"), "tname_at": start}

    def table_args(self, rng):
        """找 __table_args__，返回 None 或 {kind, outer_start, outer_end, items, dict_pos}。"""
        seg = self.src[rng["start"]:rng["end"]]
        m = re.search(r"__table_args__\s*=\s*", seg)
        if not m:
            return None
        base = rng["start"]
        after = base + m.end()
        ch = self.src[after:after + 1]
        if ch == "{":
            close = scan_balanced(self.src, after, "{", "}")
            return {"kind": "dict", "outer_start": after, "outer_end": close, "items": [],
                    "dict_pos": (after, close)}
        if ch == "(":
            close = scan_balanced(self.src, after, "(", ")")
            body = self.src[after + 1:close]
            items, dict_pos, pos = [], None, 0
            for piece in split_toplevel(body):
                # body.find(piece, pos) 保证同一段文本重复出现也能定位到正确位置
                idx = body.find(piece, pos)
                if idx < 0:
                    continue
                pos = idx + len(piece)
                s, e = after + 1 + idx, after + 1 + pos
                if piece.lstrip().startswith("{"):
                    dict_pos = (s, e)
                else:
                    items.append((s, e))
            return {"kind": "tuple", "outer_start": after, "outer_end": close,
                    "items": items, "dict_pos": dict_pos}
        return None

    def ensure_table_args(self, rng, exprs):
        """没有 __table_args__ 时，在 __tablename__ 之后新建（dict 必须放最后）。"""
        ind, nl = rng["indent"], self.nl
        body = "".join("%s%s    %s," % (nl, ind, e) for e in exprs)
        text = "%s__table_args__ = (%s%s%s)" % (ind, body, nl, ind)
        line = self.src.find(nl, rng["tname_at"])
        line_end = (line + len(nl)) if line >= 0 else len(self.src)
        return line_end, text

    def column_call(self, rng, col):
        """定位 [属性名] = Column( ... ) 的调用 span。"""
        seg = self.src[rng["start"]:rng["end"]]
        rx = re.compile(
            r'(?m)^(?P<ind>[ \t]*)(?P<q>["\']?)%s(?P=q)\s*=\s*'
            r'(?P<mod>[A-Za-z_][\w.]*\.)?(?:Column|column)\s*\(' % re.escape(col))
        m = rx.search(seg)
        if not m:
            return None
        base = rng["start"]
        open_idx = base + m.end() - 1
        close_idx = scan_balanced(self.src, open_idx)
        if close_idx < 0:
            return None
        return {"call": open_idx, "a_start": open_idx + 1, "a_end": close_idx,
                "arg": self.src[open_idx + 1:close_idx]}


def split_toplevel(s, sep=","):
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
        elif ch in "([{":
            depth += 1
            buf.append(ch)
        elif ch in ")]}":
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


def build_model_map():
    """表名 -> ModelFile（可能一个文件多个表）。"""
    mapping = {}
    for fn in sorted(os.listdir(MODELS_DIR)):
        if not fn.endswith(".py") or fn == "__init__.py":
            continue
        path = os.path.join(MODELS_DIR, fn)
        try:
            with open(path, encoding="utf-8") as f:
                src = f.read()
        except OSError:
            continue
        for m in re.finditer(r'__tablename__\s*=\s*["\']([^"\']+)["\']', src):
            mapping.setdefault(m.group(1).lower(), path)
    return mapping


_FILES = {}


def get_model_file(path):
    if path not in _FILES:
        _FILES[path] = ModelFile(path)
    return _FILES[path]


# ============================================================ 修改模型的方法
def set_kwarg(mf, a_start, arg_text, key, value_expr):
    """给调用参数列表设置 key=value（没有则追加）。"""
    rx = re.compile(r"\b%s\s*=\s*(['\"])((?:\\.|(?!\1).)*)\1" % key, re.S)
    m = rx.search(arg_text)
    if m:
        s = a_start + m.start(2) - 1
        e = a_start + m.end(2) + 1
        mf.replace(s, e, value_expr, "改 %s" % key)
        return True
    # 追加：找最后一个非空白字符之前的位置
    body = arg_text.rstrip()
    pos = a_start + len(arg_text.rstrip())
    # 原文可能以逗号结尾
    mf.insert(pos, ", %s=%s" % (key, value_expr), "新增 %s" % key)
    return True


def _prev_nonspace(src, pos):
    j = pos - 1
    while j >= 0 and src[j] in " \t\r\n":
        j -= 1
    return src[j] if j >= 0 else ""


def add_to_table_args(mf, rng, exprs):
    """把 exprs 加到 __table_args__。

    采用「重建整块」而不是局部插值：dict 元素必须落在最后（否则 SQLAlchemy 抛
    ArgumentError），重建能同时保证顺序正确和缩进统一。
    """
    ta = mf.table_args(rng)
    if ta is None:
        pos, text = mf.ensure_table_args(rng, exprs)
        mf.insert(pos, text, "新建 __table_args__")
        return True

    ind, nl = rng["indent"], mf.nl

    def fmt(t):
        """把一段元素原文压成单行结构：首行原样，续行加深缩进。"""
        ls = [ln.strip() for ln in t.split("\n") if ln.strip()]
        if not ls:
            return None
        return ls[0] + "".join("%s%s        %s" % (nl, ind, ln) for ln in ls[1:])

    if ta["kind"] == "dict":
        old = mf.src[ta["outer_start"]:ta["outer_end"] + 1]
        parts = exprs + [fmt(old)]
    else:
        parts = [fmt(mf.src[s:e]) for s, e in ta["items"]]
        parts = [x for x in parts if x] + exprs
        if ta["dict_pos"]:
            d = fmt(mf.src[ta["dict_pos"][0]:ta["dict_pos"][1]])
            if d:
                parts.append(d)
    body = "".join("%s%s    %s," % (nl, ind, x) for x in parts)
    new = "(%s%s%s)" % (body, nl, ind)
    mf.replace(ta["outer_start"], ta["outer_end"] + 1, new, "重建 __table_args__")
    return True


def export_prefix(src_file):
    """判断文件里是用 db.Index 还是裸 Index。"""
    if re.search(r"\bdb\.(Index|UniqueConstraint|Column)\b", src_file.src):
        return "db."
    return ""


def ensure_import(mf, stmt):
    """按需插入 import 行，插入点取「最后一个顶层 import/from 行之后」，避免踩 __future__。"""
    key = stmt.split(" import ")[-1].strip()
    if re.search(r"^.*\bimport\b.*\b%s\b" % re.escape(key), mf.src, re.M):
        return False
    lines = list(re.finditer(r"(?m)^(?:from|import)\s+[^\n]*$", mf.src))
    nl = mf.nl
    if lines:
        last = lines[-1]
        mf.insert(last.end(), nl + stmt, "新增 import %s" % key)
    else:
        mf.insert(0, stmt + nl, "新增 import %s" % key)
    return True


def idx_expr(prefix, name, v):
    cols = ", ".join(py_str(c) for c in v["columns"])
    extra = ", unique=True" if v.get("unique") else ""
    return "%sIndex(%s%s%s)" % (prefix, py_str(name), ", " + cols if cols else "", extra)


def uc_expr(prefix, name, cols):
    parts = [py_str(c) for c in cols]
    if name:
        parts.append("name=%s" % py_str(name))
    return "%sUniqueConstraint(%s)" % (prefix, ", ".join(parts))


def read_init_sql():
    with open(INIT_SQL_PATH, encoding="utf-8") as f:
        return f.read()


def sql_create_table(table):
    """从 init.sql 抽出该表的 CREATE TABLE 原文（最贴近事实，比自己拼 DDL 可靠）。"""
    sql = read_init_sql()
    rx = re.compile(
        r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:"?public"?\s*\.\s*)?"?([A-Za-z0-9_]+)"?\s*\(',
        re.I)
    for m in rx.finditer(sql):
        if m.group(1).lower() != table.lower():
            continue
        end = scan_balanced(sql, m.end() - 1)
        if end < 0:
            continue
        return sql[m.start():end + 1]
    return None


def sql_column_def(table, col):
    """从 init.sql 抽出某列的原始定义片段，用于 ALTER TABLE ADD COLUMN。"""
    sql = read_init_sql()
    rx = re.compile(
        r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:"?public"?\s*\.\s*)?"?([A-Za-z0-9_]+)"?\s*\(',
        re.I)
    for m in rx.finditer(sql):
        if m.group(1).lower() != table.lower():
            continue
        end = scan_balanced(sql, m.end() - 1)
        if end < 0:
            continue
        body = re.sub(r"--[^\n]*", "", sql[m.end():end])
        for item in split_toplevel(body):
            mm = re.match(r'\s*"?([A-Za-z0-9_]+)"?\s+', item)
            if mm and mm.group(1).lower() == col.lower():
                return " ".join(item.split())
    return None


# ============================================================== 主同步流程
def main():
    ap = argparse.ArgumentParser(description="三线对齐修复")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只预览）")
    ap.add_argument("--from", dest="source", choices=["init", "model"], default="init",
                    help="基准方，默认 init（以 init.sql 为准）")
    ap.add_argument("--target", default="all", choices=["all", "model", "sql", "db"])
    ap.add_argument("--prune", action="store_true", help="允许删除「多余的一方」（索引/约束）")
    args = ap.parse_args()

    import three_way_check as chk

    i_t, i_cc, i_tc, i_ix, i_uc, i_fk = chk.parse_init_sql()
    m_t, m_cc, m_tc, m_ix, m_uc, m_fk = chk.load_models()
    d_t, d_cc, d_tc, d_ix, d_uc, d_fk = chk.load_db()

    model_map = build_model_map()
    # 口径比体检脚本宽一档：init.sql ∩ 模型。
    # 因为「init.sql 有、库还没有」的表本脚本会直接补建，补完就是三方都有。
    all_common = set(i_t) & set(m_t)
    in_scope = sorted(all_common - MODEL_IGNORED)

    base_name = "init.sql" if args.source == "init" else "模型"
    other = "模型" if args.source == "init" else "init.sql"
    B = {"col_c": i_cc, "tbl_c": i_tc, "ix": i_ix, "uc": i_uc, "fk": i_fk, "t": i_t} \
        if args.source == "init" else \
        {"col_c": m_cc, "tbl_c": m_tc, "ix": m_ix, "uc": m_uc, "fk": m_fk, "t": m_t}

    lines = ["# 三线同步报告", ""]
    def out(s=""):
        lines.append(s)

    out("- 生成时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    out("- 基准：**%s**（把 %s 与数据库补齐）" % (base_name, other))
    out("- 模式：**%s**" % ("写入" if args.apply else "预览（未改动任何文件）"))
    out("- 考核范围：init.sql ∩ 模型 = **%d** 张表（缺的表会先补建）" % len(in_scope))
    out()

    plan_model = []      # 改模型源码的动作（延迟到收集完统一写）
    plan_sql = []        # 追加到 init.sql 的语句
    plan_db = []         # 立即执行的安全 DDL
    manual = []          # 需人工确认的 DDL（只写进 SQL 文件）

    actioned = 0

    def pf(desc):
        nonlocal actioned
        actioned += 1
        print("  %s" % desc)

    # -------------------------------------------- 0. 缺表 / 缺列（直接用 init.sql 原文）
    out("## 零、缺失的表与列")
    out()
    missing_tables = [t for t in sorted(set(i_t) - set(d_t))
                      if t in all_common and t not in MODEL_IGNORED]
    for t in missing_tables:
        stmt = sql_create_table(t)
        out("- 数据库缺表 `%s`（将从 init.sql 抽取 CREATE TABLE 原文执行）" % t)
        if stmt:
            plan_db.append((stmt.rstrip().rstrip(";") + ";", "建表 %s" % t))
        else:
            manual.append("-- ⚠ 缺表 %s：init.sql 中未能抽出 CREATE TABLE，需人工" % t)
    pending_create = set(missing_tables)
    missing_cols = []
    for t in in_scope:
        if t in pending_create:
            continue                      # 整体补建的表不逐列补，否则报 DuplicateColumn
        dc = d_t.get(t, {})
        for c in sorted(set(B["t"].get(t, {})) - set(dc)):
            missing_cols.append((t, c))
    for t, c in missing_cols:
        out("- 数据库缺列 `%s.%s`" % (t, c))
        if args.source != "init":
            manual.append("-- ⚠ 补列 %s.%s 请自行处理" % (t, c))
            continue
        d = sql_column_def(t, c)
        if d and "REFERENCES" not in d.upper():
            plan_db.append(("ALTER TABLE %s ADD COLUMN %s;" % (q(t), d),
                            "补列 %s.%s" % (t, c)))
        else:
            manual.append("-- ⚠ 补列 %s.%s 需要人工确认（含外键或无法抽取原定义）" % (t, c))
    if not missing_tables and not missing_cols:
        out("无 ✅")
    out()

    # ---------------------------------------------------- 1. 表注释
    out("## 一、表注释")
    out()
    tbl_rows = []
    for t in in_scope:
        base_txt = B["tbl_c"].get(t)
        if not base_txt:
            continue
        if m_tc.get(t) != base_txt:
            tbl_rows.append((t, "模型", base_txt, m_tc.get(t)))
        if d_tc.get(t) != base_txt:
            tbl_rows.append((t, "数据库", base_txt, d_tc.get(t)))
            plan_db.append(('COMMENT ON TABLE %s IS %s;' % (q(t), sql_str(base_txt)),
                            "表注释 %s" % t))
    if not tbl_rows:
        out("无需改动 ✅")
    else:
        for t, side, want, got in tbl_rows:
            out("- `%s` 的**%s**侧：应为 %s，当前为 %s" % (t, side, want, got))
        out()
        out("> 模型侧的表注释需要写入 `__table_args__` 的 comment，见下方「模型改动」。")
    out()

    # ---------------------------------------------------- 2. 列注释
    col_rows = []
    for t in in_scope:
        # 待补建的表，其列在库里还不存在，比对时不能跟库取交集
        cols_iter = (set(i_t[t]) & set(m_t[t])) if t in pending_create \
            else (set(i_t[t]) & set(m_t[t]) & set(d_t.get(t, {})))
        for c in sorted(cols_iter):
            base_txt = B["col_c"].get((t, c))
            if not base_txt:
                continue
            if m_cc.get((t, c)) != base_txt:
                col_rows.append((t, c, "模型", base_txt, m_cc.get((t, c))))
            if d_cc.get((t, c)) != base_txt:
                col_rows.append((t, c, "数据库", base_txt, d_cc.get((t, c))))
                plan_db.append((
                    'COMMENT ON COLUMN %s.%s IS %s;' % (q(t), q(c), sql_str(base_txt)),
                    "列注释 %s.%s" % (t, c)))
    out("## 二、列注释（%d 处）" % len(col_rows))
    out()
    if not col_rows:
        out("无需改动 ✅")
    else:
        out("| 表.列 | 侧 | 应为 | 当前 |")
        out("|---|---|---|---|")
        for t, c, side, want, got in col_rows[:80]:
            out("| %s.%s | %s | %s | %s |" % (t, c, side, want, got))
        if len(col_rows) > 80:
            out("| ... 其余 %d 处省略 | | | |" % (len(col_rows) - 80))
    out()

    # ---------------------------------------------------- 3. 索引
    out("## 三、索引")
    out()
    ix_missing = []      # (name, side)
    ix_extra = []        # (name, owner, side)
    for n in sorted(set(B["ix"]) | set(m_ix) | set(d_ix)):
        owner = (B["ix"].get(n) or m_ix.get(n) or d_ix.get(n))["table"]
        if owner not in in_scope:
            continue
        if n in B["ix"]:
            if args.source == "init" and n not in m_ix:
                ix_missing.append((n, "模型"))
            if n not in d_ix:
                ix_missing.append((n, "数据库"))
            if args.source == "model" and n not in i_ix:
                ix_missing.append((n, "init.sql"))
        else:
            side = []
            if n in d_ix:
                side.append("数据库")
            if n in m_ix:
                side.append("模型")
            ix_extra.append((n, owner, "/".join(side)))
    if not ix_missing and not ix_extra:
        out("无需改动 ✅")
    else:
        for n, side in ix_missing:
            v = B["ix"][n]
            out("- 缺 `%s`（%s）→ %s" % (n, side, n))
            if side == "数据库":
                u = "UNIQUE " if v["unique"] else ""
                plan_db.append((
                    'CREATE %sINDEX IF NOT EXISTS %s ON %s (%s);'
                    % (u, q(n), q(v["table"]), ", ".join(q(c) for c in v["columns"])),
                    "索引 %s" % n))
            elif side == "init.sql":
                u = "UNIQUE " if v["unique"] else ""
                plan_sql.append(
                    'CREATE %sINDEX IF NOT EXISTS %s ON "public".%s (%s);'
                    % (u, q(n), q(v["table"]), ", ".join(q(c) for c in v["columns"])))
        for n, owner, side in ix_extra:
            out("- 多余 `%s`（表 %s，存在于 %s）：基准里没有" % (n, owner, side))
            stmt = "DROP INDEX IF EXISTS %s;" % q(n)
            if args.prune and "数据库" in side:
                plan_db.append((stmt, "删除多余索引 %s" % n))
                out("  → 已加入 DROP 计划（--prune）")
            else:
                manual.append(
                    "-- ⚠ 基准(%s)中没有索引 %s（表 %s，存在于 %s），请自行判断是否删除：\n"
                    "--   DROP INDEX IF EXISTS %s;" % (base_name, n, owner, side, q(n)))
                out("  → 已写入人工确认清单（加 --prune 可直接删）")
    out()

    # ---------------------------------------------------- 4. 唯一约束
    out("## 四、唯一约束")
    out()
    uc_missing = []
    for k in sorted(set(B["uc"]) - set(m_uc)):
        if B["uc"][k]["table"] not in in_scope:
            continue
        if args.source == "init":
            uc_missing.append((k, "模型"))
    for k in sorted(set(B["uc"]) - set(d_uc)):
        if B["uc"][k]["table"] not in in_scope:
            continue
        uc_missing.append((k, "数据库"))
    if args.source == "model":
        for k in sorted(set(B["uc"]) - set(i_uc)):
            if B["uc"][k]["table"] not in in_scope:
                continue
            uc_missing.append((k, "init.sql"))
    if not uc_missing:
        out("无需改动 ✅")
    else:
        for k, side in uc_missing:
            v = B["uc"][k]
            out("- 缺 %s(%s) → %s 侧" % (v["table"], ",".join(v["columns"]), side))
            if side == "数据库":
                nm = v.get("name") or ("uq_%s_%s" % (v["table"], "_".join(v["columns"])))
                plan_db.append((
                    'ALTER TABLE %s ADD CONSTRAINT %s UNIQUE (%s);'
                    % (q(v["table"]), q(nm), ", ".join(q(c) for c in v["columns"])),
                    "唯一约束 %s" % nm))
            elif side == "init.sql":
                nm = v.get("name") or ("uq_%s_%s" % (v["table"], "_".join(v["columns"])))
                plan_sql.append(
                    'ALTER TABLE "public".%s ADD CONSTRAINT %s UNIQUE (%s);'
                    % (q(v["table"]), q(nm), ", ".join(q(c) for c in v["columns"])))
    out()

    # ---------------------------------------------------- 5. 外键
    out("## 五、外键")
    out()
    fk_missing = []
    for k in sorted(set(B["fk"]) - set(m_fk)):
        if B["fk"][k]["table"] in in_scope:
            fk_missing.append((k, "模型", B["fk"][k]))
    for k in sorted(set(B["fk"]) - set(d_fk)):
        if B["fk"][k]["table"] not in in_scope:
            continue
        if args.source == "init":
            fk_missing.append((k, "数据库", B["fk"][k]))
    if args.source == "model":
        for k in sorted(set(B["fk"]) - set(i_fk)):
            if B["fk"][k]["table"] in in_scope:
                fk_missing.append((k, "init.sql", B["fk"][k]))
    if not fk_missing:
        out("无需改动 ✅")
    else:
        for k, side, v in fk_missing:
            if side == "模型":
                out("- 模型缺外键 %s(%s) -> %s(%s)：**需人工补 ForeignKey**（脚本不自动加外键）"
                    % (v["table"], ",".join(v["columns"]), v["ref_table"],
                       ",".join(v["ref_columns"])))
            else:
                out("- 缺外键 %s(%s) -> %s(%s) [%s 侧]：已生成 ALTER TABLE 语句，需人工确认后执行"
                    % (v["table"], ",".join(v["columns"]), v["ref_table"],
                       ",".join(v["ref_columns"]), side))
                nm = v.get("name") or ("fk_%s_%s" % (v["table"], "_".join(v["columns"])))
                tail = ""
                if v["ondelete"]:
                    tail += " ON DELETE %s" % v["ondelete"]
                if v["onupdate"]:
                    tail += " ON UPDATE %s" % v["onupdate"]
                stmt = ('ALTER TABLE %s ADD CONSTRAINT %s FOREIGN KEY (%s) REFERENCES %s (%s)%s;'
                        % (q(v["table"]), q(nm), ", ".join(q(c) for c in v["columns"]),
                           q(v["ref_table"]), ", ".join(q(c) for c in v["ref_columns"]), tail))
                (plan_db if side == "数据库" else plan_sql).append((stmt, "外键 %s" % nm))
    out()

    # ---------------------------------------------------- 6. 列类型 / 可空
    out("## 六、列类型与可空性")
    out()
    type_rows, null_rows = [], []
    for t in in_scope:
        bc, mc, dc = B["t"][t], m_t[t], d_t.get(t, {})
        cols_iter = (set(mc) & set(bc)) if t in pending_create else (set(mc) & set(dc) & set(bc))
        for c in sorted(cols_iter):
            want = bc[c]
            if mc[c]["type"] != want["type"]:
                type_rows.append((t, c, want["type"], mc[c]["type"],
                                  dc[c]["type"] if c in dc else "缺(待建表)"))
            if mc[c]["nullable"] != want["nullable"]:
                null_rows.append((t, c, want["nullable"], mc[c]["nullable"]))
            if c not in dc:                      # 整表待补建，列还没落库
                continue
            if dc[c]["type"] != want["type"]:
                raw = _raw_pg_type(want["type"])
                wm = re.match(r"^character varying\((\d+)\)$", want["type"])
                cm = re.match(r"^character varying\((\d+)\)$", dc[c]["type"])
                if wm and cm and int(wm.group(1)) > int(cm.group(1)):
                    # varchar 加长不会丢数据，属于幂等安全操作
                    plan_db.append((
                        "ALTER TABLE %s ALTER COLUMN %s TYPE varchar(%s);"
                        % (q(t), q(c), wm.group(1)),
                        "加宽 %s.%s 到 %s" % (t, c, wm.group(1))))
                else:
                    manual.append(
                        "-- ⚠ 数据库 %s.%s 类型为 %s，基准为 %s，需自行确认 USING 后执行：\n"
                        "--   ALTER TABLE %s ALTER COLUMN %s TYPE %s USING %s::%s;"
                        % (t, c, dc[c]["type"], want["type"], q(t), q(c), raw, q(c), raw))
    if not type_rows and not null_rows:
        out("无需改动 ✅")
    else:
        if type_rows:
            out("### 类型（模型侧需对齐）")
            out()
            out("| 表.列 | 基准 | 模型 | 数据库 |")
            out("|---|---|---|---|")
            for r in type_rows:
                out("| %s.%s | %s | %s | %s |" % r)
            out()
        if null_rows:
            out("### 可空性（模型侧需对齐）")
            out()
            out("| 表.列 | 基准 | 模型 |")
            out("|---|---|---|")
            for t, c, want, got in null_rows:
                out("| %s.%s | %s | %s |" % (t, c, want, got))
            out()
    out()

    # ============================================ 执行：改模型源码
    out("## 七、模型文件改动")
    out()
    print()
    print("=" * 66)
    print("基准：%s ｜ 模式：%s" % (base_name, "写入" if args.apply else "预览"))
    print("=" * 66)

    # 表注释 -> 模型
    for t in in_scope:
        want = B["tbl_c"].get(t)
        if not want or m_tc.get(t) == want:
            continue
        path = model_map.get(t)
        if not path or t in MODEL_IGNORED:
            continue
        mf = get_model_file(path)
        rng = mf.class_range(t)
        if not rng:
            continue
        ta = mf.table_args(rng)
        expr = "{'comment': %s}" % py_str(want)
        if ta is None:
            pos, text = mf.ensure_table_args(rng, [expr])
            mf.insert(pos, text, "表 %s 注释" % t)
        elif ta["kind"] == "dict":
            cm = re.search(r"['\"]comment['\"]\s*:\s*(['\"])((?:\\.|(?!\1).)*)\1",
                           mf.src[ta["outer_start"]:ta["outer_end"]])
            if cm:
                s = ta["outer_start"] + cm.start(2) - 1
                e = ta["outer_start"] + cm.end(2) + 1
                mf.replace(s, e, py_str(want), "表 %s 注释(改值)" % t)
            else:
                mf.insert(ta["outer_end"], ", 'comment': %s" % py_str(want),
                          "表 %s 注释" % t)
        else:
            if ta["dict_pos"]:
                ds, de = ta["dict_pos"]
                seg = mf.src[ds:de]
                cm = re.search(r"['\"]comment['\"]\s*:\s*(['\"])((?:\\.|(?!\1).)*)\1", seg)
                if cm:
                    mf.replace(ds + cm.start(2) - 1, ds + cm.end(2) + 1, py_str(want),
                               "表 %s 注释(改值)" % t)
                else:
                    mf.insert(de - 1, "'comment': %s, " % py_str(want), "表 %s 注释" % t)
            else:
                add_to_table_args(mf, rng, [expr])
        plan_model.append("%s -> 表注释" % t)

    # 列注释 -> 模型
    for t, c, side, want, _got in col_rows:
        if side != "模型":
            continue
        path = model_map.get(t)
        if not path:
            continue
        mf = get_model_file(path)
        rng = mf.class_range(t)
        if not rng:
            continue
        cc = mf.column_call(rng, c)
        if not cc:
            plan_model.append("%s.%s -> 列注释（未定位到 Column 定义，需人工）" % (t, c))
            continue
        set_kwarg(mf, cc["a_start"], cc["arg"], "comment", py_str(want))
        plan_model.append("%s.%s -> 列注释" % (t, c))

    # 索引 -> 模型
    for n, side in ix_missing:
        if side != "模型":
            continue
        v = B["ix"][n]
        path = model_map.get(v["table"])
        if not path:
            continue
        mf = get_model_file(path)
        rng = mf.class_range(v["table"])
        if not rng:
            continue
        prefix = export_prefix(mf)
        if not prefix:
            ensure_import(mf, "from sqlalchemy import Index")
        add_to_table_args(mf, rng, [idx_expr(prefix, n, v)])
        plan_model.append("%s -> 索引 %s" % (v["table"], n))

    # 唯一约束 -> 模型
    for k, side in uc_missing:
        if side != "模型":
            continue
        v = B["uc"][k]
        path = model_map.get(v["table"])
        if not path:
            continue
        mf = get_model_file(path)
        rng = mf.class_range(v["table"])
        if not rng:
            continue
        prefix = export_prefix(mf)
        if not prefix:
            ensure_import(mf, "from sqlalchemy import UniqueConstraint")
        add_to_table_args(mf, rng, [uc_expr(prefix, v.get("name"), v["columns"])])
        plan_model.append("%s -> 唯一约束 %s" % (v["table"], ",".join(v["columns"])))

    # 列类型 -> 模型（只做安全映射）
    for t, c, want, _got, _db in type_rows:
        path = model_map.get(t)
        if not path:
            continue
        mf = get_model_file(path)
        rng = mf.class_range(t)
        if not rng:
            continue
        cc = mf.column_call(rng, c)
        if not cc:
            continue
        applied = rewrite_type(mf, cc, want)
        if applied:
            plan_model.append("%s.%s -> 类型 %s" % (t, c, want))
        else:
            plan_model.append("%s.%s -> 类型 %s（无安全映射，需人工）" % (t, c, want))

    # 可空性 -> 模型
    for t, c, want, _got in null_rows:
        path = model_map.get(t)
        if not path:
            continue
        mf = get_model_file(path)
        rng = mf.class_range(t)
        if not rng:
            continue
        cc = mf.column_call(rng, c)
        if not cc:
            continue
        set_kwarg(mf, cc["a_start"], cc["arg"], "nullable", str(bool(want)))
        plan_model.append("%s.%s -> nullable=%s" % (t, c, bool(want)))

    if plan_model:
        for d in plan_model:
            out("- %s" % d)
            pf(d)
        if args.apply and args.target in ("all", "model"):
            for path, mf in _FILES.items():
                if mf.apply():
                    print("  已写入 %s" % os.path.relpath(path, ROOT))
        elif args.apply:
            print("  （--target=%s，跳过写模型）" % args.target)
    else:
        out("无需改动 ✅")
    out()

    # ============================================ 执行：补 init.sql
    out("## 八、init.sql 追加")
    out()
    if plan_sql:
        for s in plan_sql:
            out("```sql")
            out(s)
            out("```")
        if args.apply and args.target in ("all", "sql"):
            src = open(INIT_SQL_PATH, encoding="utf-8").read()
            block = ("\n\n-- ===== 由 three_way_sync.py 于 %s 补充 =====\n"
                     % datetime.now().strftime("%Y-%m-%d %H:%M:%S")) \
                + "\n".join(s if s.endswith(";") else s + ";" for s in plan_sql) + "\n"
            with open(INIT_SQL_PATH, "a", encoding="utf-8", newline="") as f:
                f.write(block)
            print("  init.sql 已追加 %d 条语句" % len(plan_sql))
    else:
        out("无需改动 ✅")
    out()

    # ============================================ 执行：刷数据库
    out("## 九、数据库改动")
    out()
    db_ok = db_fail = 0
    if plan_db:
        if args.apply and args.target in ("all", "db"):
            from app import app
            from extensions.ext_database import db
            from sqlalchemy import text
            with app.app_context():
                for stmt, desc in plan_db:
                    try:
                        db.session.execute(text(stmt))
                        db.session.commit()
                        db_ok += 1
                        pf("[DB] %s" % desc)
                    except Exception as exc:  # noqa: BLE001
                        db.session.rollback()
                        db_fail += 1
                        print("  [DB失败] %s -> %s" % (desc, str(exc).split("\n")[0]))
        out("- 待执行 **%d** 条（幂等安全）：%s"
            % (len(plan_db),
               ("已执行，成功 %d / 失败 %d" % (db_ok, db_fail))
               if (args.apply and args.target in ("all", "db")) else "预览，未执行"))
        out()
        for stmt, _d in plan_db[:60]:
            out("```sql\n%s\n```" % stmt)
        if len(plan_db) > 60:
            out("（其余 %d 条见 SQL 文件）" % (len(plan_db) - 60))
    else:
        out("无需改动 ✅")
    out()

    # ============================================ 人工确认项
    out("## 十、需人工确认的操作（脚本不自动执行）")
    out()
    if manual:
        for m in manual:
            out("```sql\n%s\n```" % m)
    else:
        out("无 ✅")
    out()

    # 写 SQL 文件
    sql_text = [
        "-- 由 scripts/three_way_sync.py 生成于 %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "-- 基准：%s" % base_name,
        "",
        "-- ===================== 可安全重复执行的 DDL =====================",
    ]
    sql_text += [s if s.endswith(";") else s + ";" for s, _d in plan_db]
    sql_text += ["", "-- ===================== 需人工确认后再执行 ====================="]
    sql_text += [m if m.endswith(";") else m + ";" for m in manual]
    with open(SQL_OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(sql_text) + "\n")
    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print()
    print("=" * 66)
    print("模型改动 %d 项 | init.sql 追加 %d 条 | 数据库 DDL %d 条 | 人工确认 %d 条"
          % (len(plan_model), len(plan_sql), len(plan_db), len(manual)))
    print("=" * 66)
    print("报告：%s" % REPORT_MD)
    print("SQL ：%s" % SQL_OUT)
    if not args.apply:
        print()
        print("（预览模式，未写入任何文件。确认无误后加 --apply）")
    return 0


def _raw_pg_type(norm):
    """归一化类型 -> 可用于 ALTER COLUMN TYPE 的 PG 写法。"""
    table = {
        "jsonb": "jsonb", "json": "json",
        "timestamp with time zone": "timestamptz",
        "timestamp without time zone": "timestamp",
        "text": "text", "uuid": "uuid", "boolean": "boolean",
    }
    if norm.startswith("array<"):
        return table.get(norm[6:-1], norm[6:-1]) + "[]"
    m = re.match(r"^(.*?)\((\d+)\)$", norm)
    if m:
        return "%s(%s)" % (m.group(1), m.group(2))
    return table.get(norm, norm)


def rewrite_type(mf, cc, want_norm):
    """改写列的类型表达式。只处理安全且确定的映射。"""
    import re as _re
    arg, a_start = cc["arg"], cc["a_start"]
    m = _re.match(r"\s*([A-Za-z_][\w.]*\.)?"
                  r"(JSON|DateTime|TIMESTAMP|DATETIME|String|VARCHAR|ARRAY)\b", arg, _re.I)
    if not m:
        return False
    mod = m.group(1) or ""
    fn = m.group(2)
    start = a_start + m.start()
    after = _re.match(r"\s*", arg[m.end():]).group(0)
    j = m.end() + len(after)
    end = a_start + j
    if j < len(arg) and arg[j] == "(":
        close = scan_balanced(arg, j)
        if close > 0:
            end = a_start + close + 1
    if fn.upper() == "JSON":
        new = "JSONB"
        ensure_import(mf, "from sqlalchemy.dialects.postgresql import JSONB")
    elif fn.upper() in ("DateTime", "TIMESTAMP", "DATETIME"):
        if want_norm == "timestamp with time zone":
            new = "%sDateTime(timezone=True)" % mod
        elif want_norm == "timestamp without time zone":
            new = "%sDateTime()" % mod
        else:
            return False
    elif fn.upper() == "ARRAY":
        if want_norm == "array<text>":
            new = "%sARRAY(db.Text)" % mod
        else:
            return False
    elif fn.upper() in ("STRING", "VARCHAR"):
        mm = _re.match(r"^character varying\((\d+)\)$", want_norm)
        if not mm:
            return False
        new = "%sString(%s)" % (mod, mm.group(1))
    else:
        return False
    mf.replace(start, end, new, "类型 -> %s" % want_norm)
    return True


if __name__ == "__main__":
    sys.exit(main())
