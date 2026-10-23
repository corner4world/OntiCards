"""
consistency_audit.py

OntiCards 用户级「数据源 · 表 · 数据卡片 · 向量」一致性校验与修复

两种模式
--------
    python consistency_audit.py                # 检查模式：只读，任何情况下都不写库
    python consistency_audit.py --fix          # 修复模式：按可自动修复的清单执行写操作

审计范围
--------
只覆盖 users 表里登记的用户，并且只扫描这些用户自己的向量空间
（users.weaviate_class_name 指向的 collection）。
向量库里其他项目占用的 collection 一概不扫描、不修改。

校验口径（每个用户的每个数据源，四者必须完全相等）
--------------------------------------------------
    表数量    datasource_infos.table_num        数据源自报的表数
    表结构    user_datasource_schemas 的记录数    该数据源实际登记的表
    数据卡片  datacards_datasource 的记录数       该数据源实际生成的卡片
    向量      该数据源在该用户向量空间中的记录数    向量库里的实际条数

    并且要求：每张表恰好一张卡片（卡片 doc_id = 表结构 id），
              每张卡片的 w_uuid 都能在该用户向量空间中找到。

用户级合计
----------
    该用户所有数据源的 表数量之和 = 表结构之和 = 卡片之和 = 其向量空间中的记录总数
    该用户向量空间里匹配不到任何数据卡片的向量 = 孤儿向量（只在本用户的空间范围内统计）

用法
----
    python consistency_audit.py                 # 检查（默认）
    python consistency_audit.py --fix           # 检查 + 修复
    python consistency_audit.py --fix --yes     # 跳过交互确认
    python consistency_audit.py --user <uuid>   # 只查某个用户
    python consistency_audit.py --json report.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import unicodedata
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

def _setup_console_encoding() -> None:
    """
    统一按 UTF-8 输出。
    PyCharm 终端、VSCode 终端、Windows Terminal 都是 UTF-8 的，
    如果按 GBK(936) 输出中文就会变成乱码，所以这里固定 utf-8。
    """
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_setup_console_encoding()

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from weaviate.client import WeaviateClient
from weaviate.connect import ConnectionParams


# ============================================================
# 配置区
# ============================================================
DATABASE_URL = os.getenv(
    "AUDIT_DATABASE_URL",
    "postgresql+psycopg2://postgres:master_pz123@8.134.183.233:55432/prod-ontiCards",
)
WEAVIATE_URL = os.getenv("WEAVIATE_URL", "http://8.134.183.233:8088")
WEAVIATE_GRPC_PORT = int(os.getenv("GRPC_PORT") or 50051)

USERS_TABLE = "users"
SOURCES_TABLE = "datasource_infos"
SCHEMAS_TABLE = "user_datasource_schemas"
DATACARDS_TABLE = "datacards_datasource"

# 与业务代码 database_schema_extractor.SCHEMA_AWARE_DB_TYPES 保持一致：
# 只有「按 schema 区分」的库才把 schema_name 纳入数据源→表的归属键，
# 其余库（MySQL/Trino/SQLite 等）只按 (user_id, connect_info_hash) 归属。
SCHEMA_AWARE_DB_TYPES = {"postgresql", "mssql", "oracle", "kingbase", "dm"}


# ============================================================
# 问题定义
# ============================================================
# code -> (级别, 标题, 是否可自动修复, 说明)
#   level: ERROR 需要处理 / WARN 需关注
ISSUE_DEFS: Dict[str, Tuple[str, str, bool, str]] = {
    "ORPHAN_VECTOR": ("ERROR", "孤儿向量：向量空间里有、但匹配不到该用户任何数据卡片", True,
                      "该向量存在于用户的向量空间中，但用户的所有数据卡片都没有引用它，"
                      "无法对应任何一张表，通常是数据源被删除后只清了数据库没清向量"),
    "CARD_NO_VECTOR": ("ERROR", "卡片缺向量：卡片的 w_uuid 在向量空间里找不到", False,
                       "数据卡片在数据库中存在，但它的向量不在该用户的向量空间中，需要重新向量化"),
    "DUP_CARD": ("ERROR", "重复卡片：同一张表有多张数据卡片", True,
                 "同一个 doc_id（同一张表）对应多条 datacards_datasource 记录，"
                 "通常���重复生成卡片导致"),
    "CARD_NO_TABLE": ("ERROR", "多余卡片：卡片对应的表已不存在", True,
                      "卡片的 doc_id 在 user_datasource_schemas 中查不到，"
                      "即这张表已经被删除，但卡片还留着"),
    "CARD_LOOSE_DS": ("ERROR", "游离卡片：卡片挂在一个已不存在的数���源上", True,
                      "卡片的 datasource_id 为空，或指向一条已被删除的 datasource_infos 记录"),
    "CARD_WRONG_DS": ("ERROR", "卡片归属错位：卡片挂错了数据源", False,
                      "卡片的 doc_id 是一张真实存在的表，但那张表属于另一个数据源，"
                      "需要人工确认应该挂在哪一边"),
    "TABLE_NO_CARD": ("ERROR", "表无卡片：表结构没有对应的数据卡片", False,
                      "user_datasource_schemas 里的这张表没有生成数据卡片，"
                      "需要重新生成卡片（含 LLM 调用），不可自动修复"),
    "VEC_ATTR": ("ERROR", "向量属性错位：向量上的数据源/表标识与卡片不符", True,
                 "向量对象的 datasource_id 或 doc_id 与它对应的数据卡片不一致，可以从卡片回填"),
    "TABLE_NUM": ("ERROR", "表数量失真：数据源自报的表数与实际表数不符", True,
                  "datasource_infos.table_num 是冗余计数，与 user_datasource_schemas 的实际记录数不一致"),
    "NO_COLLECTION": ("ERROR", "向量空间缺失：用户的向量空间 collection 不存在", False,
                      "users.weaviate_class_name 有值，但 Weaviate 里没有这个 collection，"
                      "该用户的向量校验已跳过"),
    "SHARED_CONN": ("WARN", "共享物理连接：同一用户的多个数据源指向同一物理库", False,
                    "这些数据源会命中同一批表结构，导致表数量被重复计算，"
                    "属于业务侧重复登记，不是脏数据"),
}


@dataclass
class Issue:
    code: str
    user_id: str
    username: str
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Issues:
    items: List[Issue] = field(default_factory=list)

    def add(self, code: str, user_id: str, username: str, **payload: Any) -> None:
        self.items.append(Issue(code=code, user_id=user_id, username=username, payload=payload))

    def of(self, code: str) -> List[Issue]:
        return [i for i in self.items if i.code == code]

    def count(self, code: str) -> int:
        return len(self.of(code))


# ============================================================
# 展示工具（中文按两列宽度对齐）
# ============================================================
def dwidth(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in str(s))


def pad(s: Any, width: int, align: str = "<") -> str:
    s = "" if s is None else str(s)
    w = dwidth(s)
    if w > width:
        out = ""
        for ch in s:
            cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
            if dwidth(out) + cw > width - 1:
                break
            out += ch
        s = out + "…"
        w = dwidth(s)
    fill = " " * max(0, width - w)
    return s + fill if align == "<" else fill + s


def cut(s: Any, width: int) -> str:
    s = "" if s is None else str(s)
    if dwidth(s) <= width:
        return s
    out = ""
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if dwidth(out) + cw > width - 1:
            break
        out += ch
    return out + "…"


def norm(v: Any) -> str:
    return "" if v is None else str(v).strip()


def class_key(name: Optional[str]) -> str:
    """Weaviate 会把 collection 名首字母转成大写，DB 里存的是小写，比对时忽略大小写。"""
    return norm(name).lower()


BAR = "=" * 100
SUB = "-" * 100


def header(title: str) -> None:
    print()
    print(BAR)
    print(title)
    print(BAR)


# ============================================================
# Weaviate 访问
# ============================================================
@contextmanager
def weaviate_client():
    client = WeaviateClient(
        connection_params=ConnectionParams.from_url(
            url=WEAVIATE_URL, grpc_port=WEAVIATE_GRPC_PORT
        )
    )
    client.connect()
    try:
        yield client
    finally:
        client.close()


def iter_objects(collection) -> Iterable[Any]:
    """兼容不同 weaviate 版本的迭代器"""
    if hasattr(collection, "iterator"):
        try:
            return collection.iterator(include_vector=False)
        except TypeError:
            return collection.iterator()
        except Exception:
            pass

    def _gen():
        after = None
        while True:
            resp = collection.query.fetch_objects(limit=200, after=after)
            objs = getattr(resp, "objects", None)
            if not objs:
                break
            for o in objs:
                yield o
            after = getattr(resp, "next_after", None) or getattr(resp, "nextAfter", None)
            if not after:
                break

    return _gen()


@dataclass
class Vector:
    uuid: str
    doc_id: str
    datasource_id: str


def fetch_vectors(client, collection_name: str) -> List[Vector]:
    """把一个 collection 里的全部对象读成轻量结构（uuid + doc_id + datasource_id）"""
    col = client.collections.get(collection_name)
    out: List[Vector] = []
    for obj in iter_objects(col):
        props = obj.properties or {}
        out.append(Vector(
            uuid=str(getattr(obj, "uuid", "") or ""),
            doc_id=norm(props.get("doc_id")),
            datasource_id=norm(props.get("datasource_id")),
        ))
    return out


# ============================================================
# 数据加载
# ============================================================
@dataclass
class Snapshot:
    users: List[dict] = field(default_factory=list)
    sources: List[dict] = field(default_factory=list)
    schemas: List[dict] = field(default_factory=list)
    cards: List[dict] = field(default_factory=list)
    all_source_ids: Set[str] = field(default_factory=set)


def load_snapshot(engine: Engine, only_user: Optional[str]) -> Snapshot:
    """
    三张表一次性读全量，然后在 Python 侧按用户过滤。
    这几张表目前都是千级数据量，一次读全量可以避免 SQL 里 UUID 数组绑定的
    各种边界问题（NULL、空数组、类型不一致）。
    """
    snap = Snapshot()
    with engine.begin() as c:
        snap.users = [
            {"id": str(r[0]), "username": r[1], "class_name": norm(r[2])}
            for r in c.execute(
                text(f"SELECT id::text, username, weaviate_class_name FROM {USERS_TABLE}")
            ).fetchall()
        ]
        snap.sources = [
            {
                "id": str(r[0]),
                "user_id": str(r[1]) if r[1] else None,
                "connect_name": r[2],
                "connect_info_hash": r[3],
                "schema_name": r[4],
                "table_num": int(r[5]) if r[5] is not None else 0,
                "db_type": r[6],
                "database_name": r[7],
            }
            for r in c.execute(
                text(f"""SELECT id::text, user_id::text, connect_name, connect_info_hash,
                                 schema_name, table_num, db_type, database_name
                          FROM {SOURCES_TABLE}""")
            ).fetchall()
        ]
        snap.schemas = [
            {
                "id": str(r[0]),
                "user_id": str(r[1]) if r[1] else None,
                "connect_info_hash": r[2],
                "schema_name": r[3],
                "table_name": r[4],
                "connect_name": r[5],
                "database_name": r[6],
                "db_type": r[7],
            }
            for r in c.execute(
                text(f"""SELECT id::text, user_id::text, connect_info_hash, schema_name,
                                 table_name, connect_name, database_name, db_type
                          FROM {SCHEMAS_TABLE}""")
            ).fetchall()
        ]
        snap.cards = [
            {
                "id": int(r[0]),
                "doc_id": norm(r[1]),
                "w_uuid": norm(r[2]),
                "user_id": str(r[3]) if r[3] else None,
                "datasource_id": str(r[4]) if r[4] else None,
                "table_name": r[5],
                "connect_name": r[6],
                "created_at": str(r[7]) if r[7] else None,
            }
            for r in c.execute(
                text(f"""SELECT id, doc_id, w_uuid, user_id::text, datasource_id::text,
                                 table_name, connect_name, created_at
                          FROM {DATACARDS_TABLE}""")
            ).fetchall()
        ]

    snap.all_source_ids = {s["id"] for s in snap.sources}

    if only_user:
        snap.users = [u for u in snap.users if u["id"] == only_user]
    return snap


# ============================================================
# 数据源 → 表结构 的归属
# ============================================================
def schema_lookup_keys(user_id: str, connect_info_hash: Optional[str],
                       db_type: Optional[str], connect_name: Optional[str],
                       database_name: Optional[str],
                       schema_name: Optional[str]) -> List[Tuple]:
    """
    生成「数据源 → 表结构」的查找键，与业务代码保持一致：
      - 有 connect_info_hash：按 (user_id, hash)，仅 schema-aware 的库再加 schema_name
      - 无 connect_info_hash（历史数据）：按 (user_id, db_type, connect_name, database_name, schema_name)
    """
    uid = norm(user_id)
    if norm(connect_info_hash):
        if (db_type or "").lower() in SCHEMA_AWARE_DB_TYPES and schema_name is not None:
            return [("H", uid, norm(connect_info_hash), norm(schema_name))]
        return [("HN", uid, norm(connect_info_hash))]
    return [("L", uid, norm(db_type), norm(connect_name),
             norm(database_name), norm(schema_name)),
            ("L2", uid, norm(db_type), norm(connect_name), norm(database_name))]


def build_schema_index(schemas: Sequence[dict]) -> Dict[Tuple, List[dict]]:
    """
    把表结构按多种查找键建索引，便于从数据源侧反查。
    H  = (user, hash, schema_name)      —— schema-aware 库精确匹配
    HN = (user, hash)                   —— 其余库按 hash 匹配全部 schema_name
    L  = (user, db_type, connect_name, database_name, schema_name)  —— 历史数据
    L2 = (user, db_type, connect_name, database_name)
    """
    idx: Dict[Tuple, List[dict]] = defaultdict(list)
    for sc in schemas:
        uid = norm(sc["user_id"])
        h = norm(sc["connect_info_hash"])
        sn = norm(sc["schema_name"])
        if h:
            idx[("H", uid, h, sn)].append(sc)
            idx[("HN", uid, h)].append(sc)
        idx[("L", uid, norm(sc["db_type"]), norm(sc["connect_name"]),
             norm(sc["database_name"]), sn)].append(sc)
        idx[("L2", uid, norm(sc["db_type"]), norm(sc["connect_name"]),
             norm(sc["database_name"]))].append(sc)
    return idx


def resolve_source_schemas(src: dict, index: Dict[Tuple, List[dict]]) -> List[dict]:
    keys = schema_lookup_keys(
        src["user_id"], src["connect_info_hash"], src["db_type"],
        src["connect_name"], src["database_name"], src["schema_name"],
    )
    rows: List[dict] = []
    seen: Set[str] = set()
    for key in keys:
        for sc in index.get(key, []):
            if sc["id"] not in seen:
                seen.add(sc["id"])
                rows.append(sc)
    return rows


# ============================================================
# 校验主体
# ============================================================
@dataclass
class SourceRow:
    datasource_id: str
    connect_name: str
    db_type: str
    table_num: int
    schemas: int
    cards: int
    vectors: int
    matched: bool = True
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "datasource_id": self.datasource_id,
            "connect_name": self.connect_name,
            "db_type": self.db_type,
            "table_num": self.table_num,
            "schemas": self.schemas,
            "cards": self.cards,
            "vectors": self.vectors,
            "matched": self.matched,
            "notes": self.notes,
        }


@dataclass
class UserRow:
    user_id: str
    username: str
    class_name: str
    collection_found: bool = True
    sources: List[SourceRow] = field(default_factory=list)
    total_tables: int = 0
    total_schemas: int = 0
    total_cards: int = 0
    total_vectors: int = 0
    orphan_vectors: int = 0
    loose_cards: int = 0
    matched: bool = True
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "class_name": self.class_name,
            "collection_found": self.collection_found,
            "sources": [s.as_dict() for s in self.sources],
            "total_tables": self.total_tables,
            "total_schemas": self.total_schemas,
            "total_cards": self.total_cards,
            "total_vectors": self.total_vectors,
            "orphan_vectors": self.orphan_vectors,
            "loose_cards": self.loose_cards,
            "matched": self.matched,
            "notes": self.notes,
        }


def audit_user(user: dict, snap: Snapshot, schema_index: Dict[Tuple, List[dict]],
               schema_owner: Dict[str, dict], uuid_owner: Dict[str, str],
               collection_name: str, vectors: Optional[List[Vector]],
               issues: Issues) -> UserRow:
    uid = user["id"]
    row = UserRow(user_id=uid, username=user["username"] or uid[:8],
                  class_name=user["class_name"])

    user_sources = [s for s in snap.sources if norm(s["user_id"]) == uid]
    user_source_ids = {s["id"] for s in user_sources}

    # ---- 卡片归属 ----
    # 正常归属：datasource_id 指向本用户的某个数据源
    # 异常归属：user_id 是本用户，但 datasource_id 为空 / 指向别处（游离卡片）
    user_cards: List[dict] = []
    for c in snap.cards:
        ds_id = norm(c["datasource_id"])
        if ds_id in user_source_ids:
            user_cards.append(c)
        elif norm(c["user_id"]) == uid:
            user_cards.append(c)

    cards_by_source: Dict[str, List[dict]] = defaultdict(list)
    loose_cards: List[dict] = []
    for c in user_cards:
        ds_id = norm(c["datasource_id"])
        if ds_id in user_source_ids:
            cards_by_source[ds_id].append(c)
        else:
            loose_cards.append(c)

    # ---- 向量按数据源归集 ----
    vectors_by_source: Dict[str, List[Vector]] = defaultdict(list)
    if vectors is not None:
        for v in vectors:
            if v.datasource_id:
                vectors_by_source[v.datasource_id].append(v)

    # ---- 用户所有卡片的 w_uuid 集合（孤儿向量的判定依据）----
    card_uuids: Set[str] = {c["w_uuid"] for c in user_cards if c["w_uuid"]}
    vector_by_uuid: Dict[str, Vector] = {v.uuid: v for v in (vectors or []) if v.uuid}
    username_by_uid: Dict[str, str] = {
        u["id"]: (u["username"] or u["id"][:8]) for u in snap.users
    }

    # ---- 共享物理连接检测 ----
    key_to_sources: Dict[Tuple, List[dict]] = defaultdict(list)
    for s in user_sources:
        keys = schema_lookup_keys(
            s["user_id"], s["connect_info_hash"], s["db_type"],
            s["connect_name"], s["database_name"], s["schema_name"],
        )
        key_to_sources[keys[0]].append(s)
    for key, group in key_to_sources.items():
        if len(group) > 1:
            issues.add("SHARED_CONN", uid, row.username,
                       datasource_ids=[g["id"] for g in group],
                       connect_names=[g["connect_name"] for g in group],
                       reason="这些数据源命中同一批表结构，表数量会被重复计算")

    # ---- 逐数据源校验 ----
    all_schema_ids: Set[str] = set()
    total_tables = 0
    total_cards = 0

    for src in user_sources:
        sid = src["id"]
        ds_schemas = resolve_source_schemas(src, schema_index)
        schema_ids = {sc["id"] for sc in ds_schemas}
        all_schema_ids |= schema_ids

        ds_cards = cards_by_source.get(sid, [])
        ds_vectors = vectors_by_source.get(sid, [])

        srow = SourceRow(
            datasource_id=sid,
            connect_name=src["connect_name"] or "",
            db_type=src["db_type"] or "",
            table_num=src["table_num"],
            schemas=len(ds_schemas),
            cards=len(ds_cards),
            vectors=len(ds_vectors),
        )
        total_tables += src["table_num"]
        total_cards += len(ds_cards)

        # 表数量失真
        if src["table_num"] != len(ds_schemas):
            srow.matched = False
            srow.notes.append(f"表数量{src['table_num']}≠表结构{len(ds_schemas)}")
            issues.add("TABLE_NUM", uid, row.username,
                       datasource_id=sid, connect_name=src["connect_name"],
                       table_num=src["table_num"], real_tables=len(ds_schemas),
                       reason=f"table_num={src['table_num']}，实际表结构 {len(ds_schemas)} 条")

        # 卡片数 vs 表数 vs 向量数
        if len(ds_cards) != len(ds_schemas):
            srow.matched = False
            srow.notes.append(f"卡片{len(ds_cards)}≠表结构{len(ds_schemas)}")
        if len(ds_vectors) != len(ds_cards):
            srow.matched = False
            srow.notes.append(f"向量{len(ds_vectors)}≠卡片{len(ds_cards)}")
        if src["table_num"] != len(ds_cards):
            srow.matched = False

        # 每张表恰好一张卡片
        card_docs = [c["doc_id"] for c in ds_cards if c["doc_id"]]
        dup_docs = {d for d in card_docs if card_docs.count(d) > 1}
        cards_with_doc = {c["doc_id"]: c for c in ds_cards if c["doc_id"]}

        for doc_id in sorted(dup_docs):
            same = [c for c in ds_cards if c["doc_id"] == doc_id]
            same.sort(key=lambda x: (x["created_at"] or "", x["id"]))
            keep = same[-1]
            for item in same[:-1]:
                srow.matched = False
                issues.add("DUP_CARD", uid, row.username,
                           card_id=item["id"], w_uuid=item["w_uuid"], doc_id=doc_id,
                           datasource_id=sid, table_name=item["table_name"],
                           keep_card_id=keep["id"],
                           reason=f"同一张表有 {len(same)} 张卡片，保留 card_id={keep['id']}")

        # 表无卡片
        for sc in ds_schemas:
            if sc["id"] not in cards_with_doc:
                srow.matched = False
                issues.add("TABLE_NO_CARD", uid, row.username,
                           schema_id=sc["id"], table_name=sc["table_name"],
                           datasource_id=sid, connect_name=src["connect_name"],
                           reason="该表结构没有对应的数据卡片")

        # 卡片缺向量 / 卡片无对应表 / 卡片归属错位
        for c in ds_cards:
            w = c["w_uuid"]
            if not w:
                srow.matched = False
                issues.add("CARD_NO_VECTOR", uid, row.username,
                           card_id=c["id"], w_uuid="", doc_id=c["doc_id"],
                           datasource_id=sid, table_name=c["table_name"],
                           reason="卡片没有 w_uuid，无法定位向量")
                continue

            in_space = w in vector_by_uuid
            if not in_space:
                srow.matched = False
                # 这个向量是不是跑到别的用户空间里了？
                other_uid = uuid_owner.get(w)
                elsewhere = None
                if other_uid and other_uid != uid:
                    elsewhere = username_by_uid.get(other_uid, other_uid[:8])
                issues.add("CARD_NO_VECTOR", uid, row.username,
                           card_id=c["id"], w_uuid=w, doc_id=c["doc_id"],
                           datasource_id=sid, table_name=c["table_name"],
                           elsewhere=elsewhere,
                           reason=("该向量不在本用户的向量空间中"
                                   + (f"，但出现在用户「{elsewhere}」的空间里（数据串档）"
                                      if elsewhere else "")))
                continue

            v = vector_by_uuid[w]
            # 卡片对应的表不在本数据源的表结构里
            if c["doc_id"] and c["doc_id"] not in schema_ids:
                owner = schema_owner.get(c["doc_id"])
                srow.matched = False
                if owner and owner["id"] != sid:
                    issues.add("CARD_WRONG_DS", uid, row.username,
                               card_id=c["id"], doc_id=c["doc_id"], table_name=c["table_name"],
                               datasource_id=sid, expected_datasource_id=owner["id"],
                               expected_connect_name=owner["connect_name"],
                               reason=f"这张表属于数据源「{owner['connect_name']}」，不应挂在当前数据源")
                else:
                    issues.add("CARD_NO_TABLE", uid, row.username,
                               card_id=c["id"], w_uuid=w, doc_id=c["doc_id"],
                               datasource_id=sid, table_name=c["table_name"],
                               reason="卡片对应的表在 user_datasource_schemas 中已不存在")

            # 向量属性与卡片不符
            new_ds = norm(c["datasource_id"])
            new_doc = norm(c["doc_id"])
            if (v.datasource_id and v.datasource_id != new_ds) or (v.doc_id and v.doc_id != new_doc):
                srow.matched = False
                issues.add("VEC_ATTR", uid, row.username,
                           collection=collection_name, uuid=v.uuid,
                           datasource_id=v.datasource_id, doc_id=v.doc_id,
                           new_datasource_id=new_ds, new_doc_id=new_doc,
                           reason="向量上的 datasource_id / doc_id 与卡片不一致")

        # 该数据源下的向量是否都能对上卡片
        ds_card_uuids = {c["w_uuid"] for c in ds_cards if c["w_uuid"]}
        for v in ds_vectors:
            if v.uuid not in ds_card_uuids:
                srow.matched = False

        row.sources.append(srow)

    # ---- 游离卡片（挂在不存在的/别人的数据源上）----
    for c in loose_cards:
        row.matched = False
        ds_id = norm(c["datasource_id"])
        if ds_id and ds_id not in snap.all_source_ids:
            reason = f"datasource_id={ds_id} 指向的数据源已不存在"
        elif ds_id:
            reason = f"datasource_id={ds_id} 属于其他用户"
        else:
            reason = "datasource_id 为空"
        issues.add("CARD_LOOSE_DS", uid, row.username,
                   card_id=c["id"], w_uuid=c["w_uuid"], doc_id=c["doc_id"],
                   datasource_id=ds_id, table_name=c["table_name"], reason=reason)

    # ---- 孤儿向量：本用户向量空间里匹配不到任何一张卡片 ----
    if vectors is not None:
        for v in vectors:
            if v.uuid not in card_uuids:
                row.matched = False
                row.orphan_vectors += 1
                issues.add("ORPHAN_VECTOR", uid, row.username,
                           collection=collection_name, uuid=v.uuid,
                           doc_id=v.doc_id, datasource_id=v.datasource_id,
                           reason="该向量在本用户空间内匹配不到任何数据卡片")

    # ---- 用户级合计 ----
    row.total_tables = total_tables
    row.total_schemas = len(all_schema_ids)
    row.total_cards = total_cards + len(loose_cards)
    row.total_vectors = len(vectors) if vectors is not None else 0
    row.loose_cards = len(loose_cards)

    if vectors is None:
        row.matched = False
        row.notes.append("向量空间缺失，未做向量校验")
    else:
        if row.total_tables != row.total_schemas:
            row.matched = False
            row.notes.append(f"表数量{row.total_tables}≠表结构{row.total_schemas}")
        if row.total_schemas != row.total_cards:
            row.matched = False
            row.notes.append(f"表结构{row.total_schemas}≠卡片{row.total_cards}")
        if row.total_cards != row.total_vectors:
            row.matched = False
            row.notes.append(f"卡片{row.total_cards}≠向量{row.total_vectors}")
    if row.loose_cards:
        row.notes.append(f"{row.loose_cards} 张游离卡片")

    return row


def build_schema_owner(snap: Snapshot, schema_index: Dict[Tuple, List[dict]]) -> Dict[str, dict]:
    """
    反向索引：一张表（user_datasource_schemas.id）属于哪个数据源。
    用同一套归属规则从数据源侧建索引，避免逐张表反查导致的 O(n³)。
    同一张表被多个数据源命中（共享物理连接）时，保留第一个。
    """
    owner: Dict[str, dict] = {}
    for src in snap.sources:
        for sc in resolve_source_schemas(src, schema_index):
            owner.setdefault(sc["id"], src)
    return owner


# ============================================================
# 报告输出
# ============================================================
def print_source_matrix(user_rows: List[UserRow]) -> None:
    header("一、逐用户 / 逐数据源明细"
           "（表数量 = 表结构 = 数据卡片 = 向量，四者必须完全相等）")
    print(pad("用户名", 18) + pad("数据源", 30) + pad("库类型", 12)
          + pad("表数量", 9, ">") + pad("表结构", 9, ">") + pad("卡片", 8, ">")
          + pad("向量", 8, ">") + "  结论")
    print(SUB)
    for u in user_rows:
        for s in u.sources:
            verdict = "匹配" if s.matched else "不匹配: " + "; ".join(s.notes)
            print(pad(cut(u.username, 17), 18)
                  + pad(cut(s.connect_name, 29), 30)
                  + pad(cut(s.db_type, 11), 12)
                  + pad(s.table_num, 9, ">")
                  + pad(s.schemas, 9, ">")
                  + pad(s.cards, 8, ">")
                  + pad(s.vectors, 8, ">")
                  + "  " + verdict)
    print(SUB)


def print_user_summary(user_rows: List[UserRow]) -> None:
    header("二、逐用户合计"
           "（所有数据源的表数量之和 = 表结构之和 = 卡片之和 = 该用户向量空间的记录总数）")
    print(pad("用户名", 18) + pad("数据源", 9, ">") + pad("表数量", 9, ">")
          + pad("表结构", 9, ">") + pad("卡片", 9, ">") + pad("向量", 9, ">")
          + pad("孤儿向量", 11, ">") + pad("游离卡片", 11, ">") + "  结论")
    print(SUB)
    for u in user_rows:
        verdict = "一致" if u.matched else "不一致: " + "; ".join(u.notes)
        print(pad(cut(u.username, 17), 18)
              + pad(len(u.sources), 9, ">")
              + pad(u.total_tables, 9, ">")
              + pad(u.total_schemas, 9, ">")
              + pad(u.total_cards, 9, ">")
              + pad(u.total_vectors, 9, ">")
              + pad(u.orphan_vectors, 11, ">")
              + pad(u.loose_cards, 11, ">")
              + "  " + verdict)
    print(SUB)
    print("合计："
          f"数据源 {sum(len(u.sources) for u in user_rows)}，"
          f"表数量 {sum(u.total_tables for u in user_rows)}，"
          f"表结构 {sum(u.total_schemas for u in user_rows)}，"
          f"卡片 {sum(u.total_cards for u in user_rows)}，"
          f"向量 {sum(u.total_vectors for u in user_rows)}，"
          f"孤儿向量 {sum(u.orphan_vectors for u in user_rows)}")


def print_issues(issues: Issues, sample: int) -> None:
    header("三、问题清单")
    if not issues.items:
        print("未发现任何问题。")
        return
    order = [c for c in ISSUE_DEFS if issues.count(c) > 0]
    for code in order:
        level, title, fixable, desc = ISSUE_DEFS[code]
        items = issues.of(code)
        mark = "[ERROR]" if level == "ERROR" else "[WARN ]"
        tag = "可自动修复" if fixable else "需人工处理"
        print(f"\n{mark} {code} {title}   x{len(items)}   ({tag})")
        print(f"       {desc}")
        for it in items[:sample]:
            detail = " | ".join(f"{k}={v}" for k, v in it.payload.items() if k != "reason")
            print(f"    - [{it.username}] {detail}")
        if len(items) > sample:
            print(f"    ... 其余 {len(items) - sample} 条省略（用 --sample 调整）")


def print_conclusion(user_rows: List[UserRow], issues: Issues) -> None:
    header("结论")
    bad_users = [u for u in user_rows if not u.matched]
    print(f"  审计用户数：{len(user_rows)}")
    print(f"  审计数据源数：{sum(len(u.sources) for u in user_rows)}")
    print(f"  完全一致的用户：{len(user_rows) - len(bad_users)}")
    print(f"  存在偏差的用户：{len(bad_users)}")
    if bad_users:
        print("  存在偏差的用户：")
        for u in bad_users:
            print(f"    - {u.username}（{u.user_id}）：{'; '.join(u.notes)}")
    errs = sum(issues.count(c) for c in ISSUE_DEFS if ISSUE_DEFS[c][0] == "ERROR")
    warns = sum(issues.count(c) for c in ISSUE_DEFS if ISSUE_DEFS[c][0] == "WARN")
    fixable = sum(issues.count(c) for c in ISSUE_DEFS if ISSUE_DEFS[c][2])
    print(f"  问题：ERROR {errs} 条 / WARN {warns} 条 / 其中可自动修复 {fixable} 条")


# ============================================================
# 修复
# ============================================================
class Fixer:
    """
    修复顺序（先向量、后卡片行），避免修完卡片行又留下孤儿向量：

      1. 修正 datasource_infos.table_num
      2. 修补向量属性（datasource_id / doc_id 从卡片回填）
      3. 删除要删的卡片行，以及它们独占的向量
      4. 删除孤儿向量（此时卡片行已经定稿，多余向量一眼可辨）

    删除卡片时会先算出「存活卡片仍在引用的 w_uuid」，
    被多张卡片共用的向量不会被误删。
    """

    def __init__(self, engine: Engine, snap: Snapshot, issues: Issues,
                 uuid_collection: Dict[str, str]):
        self.engine = engine
        self.snap = snap
        self.issues = issues
        # uuid -> 该向量实际所在的 collection（扫描阶段就已经知道了）
        self.uuid_collection = uuid_collection
        self.done: Dict[str, int] = defaultdict(int)
        self.failed: List[str] = []

    # ---------- 向量 ----------
    def _delete_vectors(self, items: List[dict]) -> int:
        by_class: Dict[str, Set[str]] = defaultdict(set)
        for it in items:
            cls = norm(it.get("collection"))
            uu = norm(it.get("uuid"))
            if cls and uu:
                by_class[cls].add(uu)
        if not by_class:
            return 0
        deleted = 0
        with weaviate_client() as client:
            for class_name, uuids in by_class.items():
                if not client.collections.exists(class_name):
                    self.failed.append(f"collection {class_name} 不存在，跳过 {len(uuids)} 条")
                    continue
                col = client.collections.get(class_name)
                for u in uuids:
                    try:
                        col.data.delete_by_id(u)
                        deleted += 1
                    except Exception as e:
                        self.failed.append(f"删除向量 {class_name}/{u} 失败: {e}")
        return deleted

    def _update_vectors(self, items: List[dict]) -> int:
        by_class: Dict[str, List[dict]] = defaultdict(list)
        for it in items:
            cls = norm(it.get("collection"))
            if cls and norm(it.get("uuid")):
                by_class[cls].append(it)
        if not by_class:
            return 0
        ok = 0
        with weaviate_client() as client:
            for class_name, rows in by_class.items():
                if not client.collections.exists(class_name):
                    self.failed.append(f"collection {class_name} 不存在，跳过 {len(rows)} 条属性修补")
                    continue
                col = client.collections.get(class_name)
                for row in rows:
                    props: Dict[str, Any] = {}
                    new_ds = norm(row.get("new_datasource_id"))
                    new_doc = norm(row.get("new_doc_id"))
                    if new_ds:
                        props["datasource_id"] = new_ds
                    if new_doc:
                        props["doc_id"] = new_doc
                    if not props:
                        continue
                    try:
                        col.data.update(row["uuid"], properties=props)
                        ok += 1
                    except Exception as e:
                        self.failed.append(f"修补向量 {class_name}/{row['uuid']} 失败: {e}")
        return ok

    # ---------- 数据库 ----------
    def _delete_cards(self, card_ids: Iterable[int]) -> int:
        ids = sorted({int(i) for i in card_ids if i is not None})
        if not ids:
            return 0
        deleted = 0
        with self.engine.begin() as c:
            for s in range(0, len(ids), 500):
                chunk = ids[s:s + 500]
                ph = ",".join(f":id{i}" for i in range(len(chunk)))
                params = {f"id{i}": v for i, v in enumerate(chunk)}
                deleted += c.execute(
                    text(f"DELETE FROM {DATACARDS_TABLE} WHERE id IN ({ph})"), params
                ).rowcount
        return deleted

    def _update_table_num(self, rows: List[dict]) -> int:
        if not rows:
            return 0
        ok = 0
        with self.engine.begin() as c:
            for r in rows:
                ok += c.execute(
                    text(f"UPDATE {SOURCES_TABLE} SET table_num = :n WHERE id = :did"),
                    {"n": int(r["real_tables"]), "did": r["datasource_id"]},
                ).rowcount
        return ok

    # ---------- 主流程 ----------
    def run(self, user_rows: List[UserRow]) -> Dict[str, int]:
        # 1) table_num
        self.done["修正 table_num"] = self._update_table_num(self.issues.of("TABLE_NUM"))

        # 2) 向量属性回填
        self.done["修补向量属性"] = self._update_vectors(self.issues.of("VEC_ATTR"))

        # 3) 要删的卡片：重复卡片 + 多余卡片（表已不存在）+ 游离卡片（数据源已不存在）
        drop_cards = (self.issues.of("DUP_CARD")
                      + self.issues.of("CARD_NO_TABLE")
                      + self.issues.of("CARD_LOOSE_DS"))
        drop_ids = {int(i.payload["card_id"]) for i in drop_cards
                    if i.payload.get("card_id") is not None}
        # 归属错位的卡片不删（需人工确认），从删除名单里剔除
        wrong_ds_cards = {int(i.payload["card_id"]) for i in self.issues.of("CARD_WRONG_DS")
                          if i.payload.get("card_id") is not None}
        drop_ids -= wrong_ds_cards

        if drop_ids:
            # 存活卡片仍在引用的 w_uuid —— 这些向量不能删
            surviving_uuids: Set[str] = set()
            for c in self.snap.cards:
                if int(c["id"]) in drop_ids:
                    continue
                w = norm(c["w_uuid"])
                if w:
                    surviving_uuids.add(w)

            drop_uuids: Set[str] = set()
            for c in self.snap.cards:
                if int(c["id"]) in drop_ids:
                    w = norm(c["w_uuid"])
                    if w and w not in surviving_uuids:
                        drop_uuids.add(w)

            # 这些向量在哪个 collection 里，扫描阶段就已经知道了
            vec_items = [{"collection": self.uuid_collection.get(u, ""), "uuid": u}
                         for u in sorted(drop_uuids)]
            self.done["删除卡片独占的向量"] = self._delete_vectors(vec_items)
            self.done["删除多余卡片"] = self._delete_cards(drop_ids)

        # 4) 孤儿向量（卡片行已定稿，此时剩下的就是真孤儿）
        orphan_items = [{"collection": i.payload.get("collection", ""),
                         "uuid": i.payload.get("uuid", "")}
                        for i in self.issues.of("ORPHAN_VECTOR")]
        self.done["删除孤儿向量"] = self._delete_vectors(orphan_items)

        return dict(self.done)


# ============================================================
# main
# ============================================================
def main() -> int:
    ap = argparse.ArgumentParser(
        description="OntiCards 用户级「数据源 · 表 · 数据卡片 · 向量」一致性校验与修复",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--fix", action="store_true",
                    help="修复模式：按可自动修复的清单执行写操作（默认是只检查模式）")
    ap.add_argument("--yes", action="store_true", help="配合 --fix 使用，跳过交互确认")
    ap.add_argument("--user", help="只校验指定 user_id")
    ap.add_argument("--json", dest="json_out", help="把完整结果导出为 JSON 文件")
    ap.add_argument("--sample", type=int, default=10, help="每类问题最多打印多少条明细，默认 10")
    args = ap.parse_args()

    print(BAR)
    print("OntiCards 数据源 / 表 / 数据卡片 / 向量 一致性校验"
          + ("（修复模式）" if args.fix else "（检查模式）"))
    print(BAR)
    print(f"数据库     : {DATABASE_URL.split('@')[-1]}")
    print(f"Weaviate   : {WEAVIATE_URL}:{WEAVIATE_GRPC_PORT}")
    print(f"范围       : {'用户 ' + args.user if args.user else 'users 表中的全部用户'}"
          f"（只扫描这些用户自己的向量空间）")

    engine = create_engine(DATABASE_URL, future=True)
    issues = Issues()

    print("\n[1/3] 加载数据库数据 ...")
    snap = load_snapshot(engine, args.user)
    scope_ids = {u["id"] for u in snap.users}
    schema_index = build_schema_index(snap.schemas)
    print(f"      用户 {len(snap.users)}  数据源 "
          f"{len([s for s in snap.sources if norm(s['user_id']) in scope_ids])}  "
          f"表结构 {len(snap.schemas)}  数据卡片 {len(snap.cards)}")

    print("[2/3] 扫描各用户的向量空间 ...")
    user_rows: List[UserRow] = []
    schema_owner = build_schema_owner(snap, schema_index)

    # ---- 第一遍：把各用户自己向量空间里的对象读出来 ----
    # 只扫 users.weaviate_class_name 指向的 collection，其他项目的 collection 一概不碰。
    vectors_by_user: Dict[str, Optional[List[Vector]]] = {}
    collection_by_user: Dict[str, str] = {}
    uuid_collection: Dict[str, str] = {}
    with weaviate_client() as client:
        existing = {class_key(c): c for c in client.collections.list_all()}
        for u in snap.users:
            cn = norm(u.get("class_name"))
            if not cn:
                vectors_by_user[u["id"]] = None
                continue
            real_name = existing.get(class_key(cn))
            if real_name is None:
                vectors_by_user[u["id"]] = None
                continue
            vecs = fetch_vectors(client, real_name)
            vectors_by_user[u["id"]] = vecs
            collection_by_user[u["id"]] = real_name
            for v in vecs:
                uuid_collection.setdefault(v.uuid, real_name)
            print(f"      {cut(u['username'] or u['id'][:8], 20):<22} "
                  f"{cut(real_name, 44):<45} 向量 {len(vecs)}")

    # uuid -> 归属用户，用于发现「卡片向量串到别人的空间里」
    uuid_owner: Dict[str, str] = {}
    for u in snap.users:
        for v in (vectors_by_user.get(u["id"]) or []):
            uuid_owner.setdefault(v.uuid, u["id"])

    # ---- 第二遍：逐用户校验 ----
    for u in snap.users:
        cn = norm(u.get("class_name"))
        uname = u["username"] or u["id"][:8]
        if not cn:
            issues.add("NO_COLLECTION", u["id"], uname,
                       class_name="", reason="用户没有登记向量空间")
            r = UserRow(user_id=u["id"], username=uname,
                        class_name="", collection_found=False)
            r.matched = False
            r.notes.append("未登记向量空间")
            user_rows.append(r)
            continue
        real_name = collection_by_user.get(u["id"])
        if real_name is None:
            issues.add("NO_COLLECTION", u["id"], uname,
                       class_name=cn,
                       reason="users.weaviate_class_name 有值，但 Weaviate 里没有这个 collection")
            r = audit_user(u, snap, schema_index, schema_owner, uuid_owner, cn, None, issues)
            r.collection_found = False
            user_rows.append(r)
            continue
        r = audit_user(u, snap, schema_index, schema_owner, uuid_owner,
                       real_name, vectors_by_user.get(u["id"]), issues)
        user_rows.append(r)

    print("[3/3] 输出结论 ...")
    print_source_matrix(user_rows)
    print_user_summary(user_rows)
    print_issues(issues, args.sample)
    print_conclusion(user_rows, issues)

    errs = sum(issues.count(c) for c in ISSUE_DEFS if ISSUE_DEFS[c][0] == "ERROR")

    if args.json_out:
        payload = {
            "mode": "fix" if args.fix else "check",
            "database_url": DATABASE_URL,
            "weaviate": WEAVIATE_URL,
            "scope": args.user or "all users in users table",
            "users": [u.as_dict() for u in user_rows],
            "issues": {
                c: {"level": ISSUE_DEFS[c][0], "title": ISSUE_DEFS[c][1],
                    "fixable": ISSUE_DEFS[c][2], "description": ISSUE_DEFS[c][3],
                    "count": issues.count(c),
                    "items": [{"user_id": i.user_id, "username": i.username, **i.payload}
                              for i in issues.of(c)]}
                for c in ISSUE_DEFS
            },
        }
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        print(f"\n  JSON 报告已导出：{args.json_out}")

    # ---------- 修复 ----------
    if not args.fix:
        fixable = [c for c in ISSUE_DEFS if ISSUE_DEFS[c][2] and issues.count(c) > 0]
        print("\n本次是检查模式，未做任何写操作。")
        if fixable:
            print("可自动修复的问题类型：" + ", ".join(fixable))
            print("执行修复：python consistency_audit.py --fix"
                  + (" --yes" if args.yes else ""))
        else:
            print("当前没有可自动修复的问题。")
        return 0 if errs == 0 else 1

    fixable = [c for c in ISSUE_DEFS if ISSUE_DEFS[c][2] and issues.count(c) > 0]
    if not fixable:
        print("\n【修复模式】没有需要自动修复的问题，未做任何写操作。")
        return 0 if errs == 0 else 1

    header("修复计划")
    for c in fixable:
        print(f"  {c} {ISSUE_DEFS[c][1]}   x{issues.count(c)}")
    print("\n注意：以下操作不可自动回滚")
    print("  · 删除数据卡片记录（datacards_datasource）")
    print("  · 删除该卡片独占的向量对象")
    print("  · 删除用户向量空间中的孤儿向量")
    print("  · 回填 datasource_infos.table_num")
    print("  · 回填向量对象上的 datasource_id / doc_id")

    if not args.yes:
        try:
            ans = input("\n确认执行以上修复？(输入 yes 继续): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            return 1
        if ans != "yes":
            print("已取消，未做任何修改。")
            return 1

    fixer = Fixer(engine, snap, issues, uuid_collection)
    done = fixer.run(user_rows)

    header("修复结果")
    for k, v in done.items():
        print(f"  {k}: {v}")
    if fixer.failed:
        print(f"\n  失败 {len(fixer.failed)} 项：")
        for m in fixer.failed[:20]:
            print(f"    - {m}")
        if len(fixer.failed) > 20:
            print(f"    ... 其余 {len(fixer.failed) - 20} 条省略")

    print("\n建议重新执行一次检查以确认结果：python consistency_audit.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
