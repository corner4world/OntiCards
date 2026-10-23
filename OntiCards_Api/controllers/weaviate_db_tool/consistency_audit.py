"""
consistency_audit.py

OntiCards 数据一致性全量校验 / 修复脚本

核心改进（相对旧脚本）
--------------------
1. 数据源→表结构的归属不再用 `connect_info_hash` 猜测，而是以
   `datacards_datasource.datasource_id + doc_id` 为唯一权威链路。
   旧脚本把「同一物理连接被登记成多个数据源」时的全部 schema 都算给每个数据源，
   从而虚增出成百上千条"脏数据"（实测 396 条全部是误报，schemas 与 datacards
   实际是严格 1:1 的 765 : 765）。
   现在把这种情况降级为 INFO 级的"共享连接"提示，不再计入脏数据。

2. Weaviate 侧改为「按 collection 全量扫描」，不再依赖 datasource_infos 里的
   datasource_id 能否命中。旧脚本只遍历还活着的数据源，导致数据源被删除后残留的
   孤儿向量永远看不见（用户 51a3d359 卡片 17 条 / 向量 169 条，差额 152 条全是这类）。

3. 修复动作按"先删向量、再删 DB 行"的顺序执行，避免产生新的孤儿向量
   （旧 clean_dirty_data.py 把 WEAVIATE_CLASS_NAME 写死成某一个用户，
   清 DB 时全用户一起清、清向量时只清那一个，必然留下孤儿向量）。

用法
----
    python consistency_audit.py                     # 只检查（默认模式，不做任何写操作）
    python consistency_audit.py --fix               # 检查 + 修复（执行前需确认）
    python consistency_audit.py --fix --yes          # 检查 + 修复（跳过确认）
    python consistency_audit.py --user <uuid>        # 只审计某个用户
    python consistency_audit.py --only DB1,WV1      # 只处理指定问题类型
    python consistency_audit.py --skip-weaviate     # 只校验数据库，不连向量库
    python consistency_audit.py --json report.json  # 导出 JSON 报告
    python consistency_audit.py --sample 20         # 明细最多打印 20 条/类型
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from weaviate.client import WeaviateClient
from weaviate.connect import ConnectionParams


# ============================================================
# 配置区
# ============================================================
DATABASE_URL = os.getenv(
    "AUDIT_DATABASE_URL",
    "postgresql+psycopg2://db_username:db_password@db_ip:db_port/db_name",
)
WEAVIATE_URL = os.getenv("WEAVIATE_URL", "http://weaviate_ip:weaviate_port")
WEAVIATE_GRPC_PORT = int(os.getenv("GRPC_PORT") or 50051)

SCHEMAS_TABLE = "user_datasource_schemas"
DATACARDS_TABLE = "datacards_datasource"
SOURCES_TABLE = "datasource_infos"
FIELD_INDEX_SUFFIX = "__field_index"
# ============================================================


# ============================================================
# 问题定义
# ============================================================
# code -> (级别, 标题, 是否可自动修复, 说明)
#   level: ERROR 需要处理 / WARN 建议处理 / INFO 仅提示
ISSUE_DEFS: Dict[str, Tuple[str, str, bool, str]] = {
    # ---------------- 精简模式（--simple）----------------
    "S1": ("ERROR", "向量多于卡片：数据源下有多余向量", True,
           "该数据源在向量空间中的向量条数 > 卡片条数，多出的向量没有对应卡片记录"),
    "S2": ("ERROR", "卡片多于向量：数据源下有卡片缺向量", False,
           "该数据源的卡片数 > 向量数，说明有卡片没被向量化，需重新生成向量"),
    "S3": ("ERROR", "卡片多于表：同一张表有重复卡片", True,
           "同一 doc_id 对应多条卡片记录，通常是重复生成卡片导致的"),
    "S4": ("ERROR", "表结构与卡片不对应：schema 集合与卡片 doc_id 不一致", False,
           "该数据源按 (user_id, connect_info_hash, schema_name) 关联到的 schema "
           "与卡片 doc_id 不是同一集合，说明表结构或卡片的归属有问题"),
    # ---------------- 数据库层 ----------------
    "DB1": ("ERROR", "孤儿卡片：数据源已删除", True,
            "datacards_datasource.datasource_id 为空或指向不存在的 datasource_infos"),
    "DB2": ("ERROR", "孤儿卡片：表结构已删除", True,
            "datacards_datasource.doc_id 在 user_datasource_schemas 中找不到"),
    "DB3": ("ERROR", "重复卡片：同一张表有多条卡片", True,
            "同一个 doc_id 对应多条 datacards_datasource 记录"),
    "DB4": ("ERROR", "重复向量：同一 w_uuid 被多条卡片引用", True,
            "多张卡片共用一个向量对象，删掉其中一张会导致另一张也失效"),
    "DB5": ("ERROR", "卡片归属错误：user_id 与数据源不一致", True,
            "datacards_datasource.user_id 与其数据源的 user_id 不一致"),
    "DB6": ("WARN", "数据源 table_num 与实际表数不符", True,
            "datasource_infos.table_num 是冗余计数，与实际卡片数不一致"),
    "DB7": ("WARN", "无卡片表：表结构没有对应卡片", False,
            "需要重新生成卡片（含 LLM 调用），不可自动修复"),
    "DB8": ("WARN", "孤儿数据源：user_id 在 users 中不存在", False,
            "用户被删除但数据源残留，需业务确认"),
    "DB9": ("WARN", "卡片缺少 w_uuid", False,
            "无法定位向量，需重新向量化"),
    "DB10": ("WARN", "同一张表被多个数据源引用", False,
            "通常是同一物理连接被登记成多个数据源导致"),
    "DB11": ("WARN", "数据源无任何卡片", False,
            "表结构已采集但从未生成卡片"),
    # ---------------- 向量库层 ----------------
    "WV1": ("ERROR", "孤儿向量：向量库有、数据库无对应卡片", True,
            "按 collection 全量扫描得出，数据源被删除后残留的向量即属此类"),
    "WV2": ("ERROR", "向量 datasource_id 缺失或失效", True,
            "向量上的 datasource_id 为空，或指向已删除的数据源"),
    "WV3": ("WARN", "向量 doc_id 与卡片不一致", True,
            "同一 w_uuid 上记录的 doc_id 与数据库卡片对不上"),
    "WV4": ("ERROR", "卡片向量缺失：数据库有卡片、向量库没有", False,
            "需重新向量化，不可自动修复"),
    "WV5": ("ERROR", "卡片向量存在于别的用户的 collection", False,
            "跨用户数据串档，需人工确认"),
    "WV6": ("WARN", "非卡片 collection：向量库中存在但无用户登记", False,
            "Weaviate 中的 collection 未出现在 users.weaviate_class_name。"
            "本脚本只审计 datacard_datasource__* 的卡片 collection，其余仅列出不处理"),
    "WV7": ("WARN", "用户 collection 缺失", False,
            "users.weaviate_class_name 有值但 Weaviate 中没有该 collection"),
    "INFO_SHARED_CONN": ("INFO", "共享连接：多个数据源指向同一物理库", False,
            "非脏数据，仅提示。旧脚本把它误判为脏数据，是 396 条误报的来源"),
}


@dataclass
class Issue:
    code: str
    key: str                       # 归属维度（一般是 user_id），用于分组展示
    payload: Dict[str, Any]
    fixable: bool = True


@dataclass
class AuditResult:
    issues: Dict[str, List[Issue]] = field(default_factory=lambda: defaultdict(list))

    def add(self, code: str, key: str, payload: Dict[str, Any]) -> None:
        self.issues[code].append(Issue(code=code, key=key, payload=payload))

    def count(self, code: str) -> int:
        return len(self.issues.get(code, []))


# ============================================================
# 基础工具
# ============================================================
def normalize_class_name(name: Optional[str]) -> str:
    if not name:
        return ""
    return str(name).strip().replace("-", "_")


# Weaviate 会把 collection 名的首字母强制转成大写，DB 里存的却是小写，
# 因此比对 collection 名时必须大小写不敏感。
def class_key(name: Optional[str]) -> str:
    return normalize_class_name(name).lower()


# 卡片 collection 的命名约定：datacard_datasource__<user_id>
CARD_CLASS_PREFIX = "datacard_datasource__"


def is_card_collection(name: str) -> bool:
    return class_key(name).startswith(CARD_CLASS_PREFIX)


def group_key(user_id: Optional[str], connect_info_hash: Optional[str],
              schema_name: Optional[str]) -> Tuple[str, str, str]:
    """(user_id, connect_info_hash, schema_name) 归一化分组键，空值统一成 ''"""
    return (
        str(user_id) if user_id else "",
        str(connect_info_hash) if connect_info_hash else "",
        str(schema_name) if schema_name else "",
    )


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


# ============================================================
# 数据库数据加载
# ============================================================
@dataclass
class Snapshot:
    users: List[dict] = field(default_factory=list)                # 参与审计的用户（受 --user 过滤）
    all_users: List[dict] = field(default_factory=list)            # 全量用户（用于归属判定）
    sources: List[dict] = field(default_factory=list)              # datasource_infos（受 --user 过滤）
    schemas: List[dict] = field(default_factory=list)              # user_datasource_schemas（受 --user 过滤）
    datacards: List[dict] = field(default_factory=list)            # 卡片（受 --user 过滤）
    all_datacards: List[dict] = field(default_factory=list)        # 全量卡片（向量判定用）
    scope_user: Optional[str] = None


def load_snapshot(engine: Engine, only_user: Optional[str]) -> Snapshot:
    snap = Snapshot()
    snap.scope_user = only_user
    user_filter = "WHERE id::text = :uid" if only_user else ""
    src_filter = "WHERE user_id::text = :uid" if only_user else ""
    sch_filter = "WHERE user_id::text = :uid" if only_user else ""

    params = {"uid": only_user} if only_user else {}

    with engine.begin() as c:
        snap.all_users = [
            {"id": str(r[0]), "username": r[1], "class_name": normalize_class_name(r[2])}
            for r in c.execute(
                text("SELECT id::text, username, weaviate_class_name FROM users")
            ).fetchall()
        ]
        snap.users = [u for u in snap.all_users
                      if (not only_user) or u["id"] == only_user]

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
                text(f"""
                    SELECT id::text, user_id::text, connect_name, connect_info_hash,
                           schema_name, table_num, db_type, database_name
                    FROM {SOURCES_TABLE} {src_filter}
                    ORDER BY created_at
                """),
                params,
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
                text(f"""
                    SELECT id::text, user_id::text, connect_info_hash, schema_name,
                           table_name, connect_name, database_name, db_type
                    FROM {SCHEMAS_TABLE} {sch_filter}
                    ORDER BY created_at
                """),
                params,
            ).fetchall()
        ]

        # 卡片始终全量加载：向量侧的孤儿判定依赖它（跨用户的 WV5 也需要）
        # 卡片：全量加载。向量侧的"是否有卡片"判定必须用全量数据，
        # 否则 --user 过滤会把其他用户的向量全部误判成孤儿向量。
        snap.all_datacards = [
            {
                "id": int(r[0]),
                "doc_id": str(r[1]) if r[1] else None,
                "w_uuid": str(r[2]) if r[2] else None,
                "user_id": str(r[3]) if r[3] else None,
                "datasource_id": str(r[4]) if r[4] else None,
                "table_name": r[5],
                "connect_name": r[6],
                "created_at": str(r[7]) if r[7] else None,
            }
            for r in c.execute(
                text(f"""
                    SELECT id, doc_id, w_uuid, user_id::text, datasource_id::text,
                           table_name, connect_name, created_at
                    FROM {DATACARDS_TABLE}
                    ORDER BY id
                """)
            ).fetchall()
        ]

    if only_user:
        snap.datacards = [d for d in snap.all_datacards if d["user_id"] == only_user]
    else:
        snap.datacards = list(snap.all_datacards)

    return snap


# ============================================================
# 数据库层校验
# ============================================================
def audit_db(snap: Snapshot, result: AuditResult, simple: bool = False) -> None:
    user_ids: Set[str] = {u["id"] for u in snap.all_users}
    source_by_id: Dict[str, dict] = {s["id"]: s for s in snap.sources}
    schema_by_id: Dict[str, dict] = {s["id"]: s for s in snap.schemas}

    if simple:
        # 精简模式数据库层只查两项，都直接决定「表 / 卡片 / 向量」能否对应：
        #   S3 同一张表重复卡片 -> 卡片数 > 表数
        #   S4 数据源关联到的 schema 集合与卡片 doc_id 集合不一致 -> 表归属错位
        # 业务侧不允许同一用户对同一物理库重复建数据源（除非 schema 不同），
        # 因此 (user_id, connect_info_hash, schema_name) 三元组是唯一且权威的关联键。
        by_doc: Dict[str, List[dict]] = defaultdict(list)
        for dc in snap.datacards:
            if dc["doc_id"]:
                by_doc[dc["doc_id"]].append(dc)
        for doc_id, items in by_doc.items():
            if len(items) < 2:
                continue
            items_sorted = sorted(items, key=lambda x: (x["created_at"] or "", x["id"]))
            keep = items_sorted[-1]
            for item in items_sorted[:-1]:
                result.add("S3", item["user_id"] or "-", {
                    "card_id": item["id"], "w_uuid": item["w_uuid"],
                    "doc_id": doc_id, "datasource_id": item["datasource_id"],
                    "table_name": item["table_name"], "connect_name": item["connect_name"],
                    "kept_card_id": keep["id"],
                    "reason": f"同一张表有 {len(items)} 张卡片，保留 card_id={keep['id']}",
                })

        # S4 schema 表结构 <-> 卡片 doc_id 双向核对
        schemas_by_triple: Dict[Tuple[str, str, str], Set[str]] = defaultdict(set)
        for sc in snap.schemas:
            schemas_by_triple[group_key(
                sc["user_id"], sc["connect_info_hash"], sc["schema_name"])].add(sc["id"])
        card_docs_by_source: Dict[str, Set[str]] = defaultdict(set)
        for dc in snap.datacards:
            if dc["datasource_id"] and dc["doc_id"]:
                card_docs_by_source[dc["datasource_id"]].add(dc["doc_id"])

        table_name_by_id = {s["id"]: s["table_name"] for s in snap.schemas}
        for src in snap.sources:
            key = group_key(src["user_id"], src["connect_info_hash"], src["schema_name"])
            sch_ids = schemas_by_triple.get(key, set())
            doc_ids = card_docs_by_source.get(src["id"], set())
            if sch_ids == doc_ids:
                continue
            only_sch = sch_ids - doc_ids
            only_doc = doc_ids - sch_ids
            result.add("S4", src["user_id"] or "-", {
                "datasource_id": src["id"], "connect_name": src["connect_name"],
                "schema_count": len(sch_ids), "card_doc_count": len(doc_ids),
                "schema_only": [table_name_by_id.get(x, x) for x in only_sch],
                "card_only": [x for x in only_doc],
                "reason": "该数据源按三元组关联到的 schema 与卡片 doc_id 不是同一集合"
                          + (f"；仅 schema 有：{sorted(table_name_by_id.get(x, x) for x in only_sch)}" if only_sch else "")
                          + (f"；仅卡片有：{len(only_doc)} 个 doc_id 无对应表结构" if only_doc else ""),
            })
        return

    # ---- DB1 孤儿卡片：数据源已删除 ----
    for dc in snap.datacards:
        ds_id = dc["datasource_id"]
        if not ds_id or ds_id not in source_by_id:
            result.add("DB1", dc["user_id"] or "-", {
                "card_id": dc["id"], "w_uuid": dc["w_uuid"], "doc_id": dc["doc_id"],
                "datasource_id": ds_id, "table_name": dc["table_name"],
                "connect_name": dc["connect_name"],
                "reason": "datasource_id 为空" if not ds_id else "datasource_id 不存在",
            })

    # ---- DB2 孤儿卡片：表结构已删除 ----
    for dc in snap.datacards:
        if not dc["doc_id"] or dc["doc_id"] not in schema_by_id:
            if any(i.payload["card_id"] == dc["id"] for i in result.issues.get("DB1", [])):
                continue  # 已被 DB1 覆盖，避免同一条卡片重复上报
            result.add("DB2", dc["user_id"] or "-", {
                "card_id": dc["id"], "w_uuid": dc["w_uuid"], "doc_id": dc["doc_id"],
                "datasource_id": dc["datasource_id"], "table_name": dc["table_name"],
                "connect_name": dc["connect_name"],
                "reason": "doc_id 为空" if not dc["doc_id"] else "doc_id 在 schemas 中不存在",
            })

    # ---- DB3 重复卡片：同 doc_id 多条 ----
    by_doc: Dict[str, List[dict]] = defaultdict(list)
    for dc in snap.datacards:
        if dc["doc_id"]:
            by_doc[dc["doc_id"]].append(dc)
    dup_card_ids: Set[int] = set()
    for doc_id, items in by_doc.items():
        if len(items) < 2:
            continue
        # 保留最新创建的一条，其余判为冗余
        items_sorted = sorted(items, key=lambda x: (x["created_at"] or "", x["id"]))
        keep = items_sorted[-1]
        for item in items_sorted[:-1]:
            dup_card_ids.add(item["id"])
            result.add("DB3", item["user_id"] or "-", {
                "card_id": item["id"], "w_uuid": item["w_uuid"], "doc_id": doc_id,
                "datasource_id": item["datasource_id"], "table_name": item["table_name"],
                "kept_card_id": keep["id"],
                "reason": f"doc_id 重复 {len(items)} 条，保留 card_id={keep['id']}",
            })

    # ---- DB4 重复向量引用：同 w_uuid 多条 ----
    by_uuid: Dict[str, List[dict]] = defaultdict(list)
    for dc in snap.datacards:
        if dc["w_uuid"]:
            by_uuid[dc["w_uuid"]].append(dc)
    dup_uuid_card_ids: Set[int] = set()
    for w_uuid, items in by_uuid.items():
        if len(items) < 2:
            continue
        keep = sorted(items, key=lambda x: (x["created_at"] or "", x["id"]))[-1]
        for item in items:
            if item["id"] == keep["id"] or item["id"] in dup_card_ids:
                continue
            dup_uuid_card_ids.add(item["id"])
            result.add("DB4", item["user_id"] or "-", {
                "card_id": item["id"], "w_uuid": w_uuid, "doc_id": item["doc_id"],
                "datasource_id": item["datasource_id"], "table_name": item["table_name"],
                "kept_card_id": keep["id"],
                "reason": f"w_uuid 被 {len(items)} 张卡片引用，保留 card_id={keep['id']}",
            })

    # ---- DB5 卡片 user_id 与数据源不一致 ----
    for dc in snap.datacards:
        src = source_by_id.get(dc["datasource_id"] or "")
        if not src:
            continue
        if dc["user_id"] and src["user_id"] and dc["user_id"] != src["user_id"]:
            result.add("DB5", src["user_id"], {
                "card_id": dc["id"], "w_uuid": dc["w_uuid"], "doc_id": dc["doc_id"],
                "datasource_id": dc["datasource_id"], "table_name": dc["table_name"],
                "old_user_id": dc["user_id"], "new_user_id": src["user_id"],
                "reason": f"卡片 user_id={dc['user_id']}，数据源 user_id={src['user_id']}",
            })
        elif not dc["user_id"] and src["user_id"]:
            result.add("DB5", src["user_id"], {
                "card_id": dc["id"], "w_uuid": dc["w_uuid"], "doc_id": dc["doc_id"],
                "datasource_id": dc["datasource_id"], "table_name": dc["table_name"],
                "old_user_id": None, "new_user_id": src["user_id"],
                "reason": "卡片 user_id 为空，可回填为数据源所属用户",
            })

    # ---- DB6 table_num 失真 ----
    actual_tables: Dict[str, Set[str]] = defaultdict(set)
    cards_per_source: Dict[str, int] = defaultdict(int)
    for dc in snap.datacards:
        if dc["datasource_id"]:
            cards_per_source[dc["datasource_id"]] += 1
            if dc["doc_id"]:
                actual_tables[dc["datasource_id"]].add(dc["doc_id"])
    for src in snap.sources:
        real = len(actual_tables.get(src["id"], set()))
        if real != src["table_num"]:
            result.add("DB6", src["user_id"] or "-", {
                "datasource_id": src["id"], "connect_name": src["connect_name"],
                "table_num": src["table_num"], "real_tables": real,
                "cards": cards_per_source.get(src["id"], 0),
                "reason": f"table_num={src['table_num']}，实际有卡片的表={real}",
            })

    # ---- DB7 无卡片表 ----
    card_doc_ids = {dc["doc_id"] for dc in snap.datacards if dc["doc_id"]}
    for sch in snap.schemas:
        if sch["id"] not in card_doc_ids:
            result.add("DB7", sch["user_id"] or "-", {
                "doc_id": sch["id"], "table_name": sch["table_name"],
                "connect_name": sch["connect_name"], "database_name": sch["database_name"],
                "reason": "该表结构没有对应数据卡片",
            })

    # ---- DB8 孤儿数据源 ----
    for src in snap.sources:
        if src["user_id"] and src["user_id"] not in user_ids:
            result.add("DB8", src["user_id"] or "-", {
                "datasource_id": src["id"], "connect_name": src["connect_name"],
                "table_num": src["table_num"],
                "reason": f"数据源所属用户 {src['user_id']} 在 users 表中不存在",
            })

    # ---- DB9 卡片缺 w_uuid ----
    for dc in snap.datacards:
        if not dc["w_uuid"]:
            result.add("DB9", dc["user_id"] or "-", {
                "card_id": dc["id"], "doc_id": dc["doc_id"],
                "datasource_id": dc["datasource_id"], "table_name": dc["table_name"],
                "reason": "w_uuid 为空，无法定位向量",
            })

    # ---- DB10 同一张表被多个数据源引用 ----
    doc_to_sources: Dict[str, Set[str]] = defaultdict(set)
    for dc in snap.datacards:
        if dc["doc_id"] and dc["datasource_id"]:
            doc_to_sources[dc["doc_id"]].add(dc["datasource_id"])
    for doc_id, ds_ids in doc_to_sources.items():
        if len(ds_ids) > 1:
            sch = schema_by_id.get(doc_id, {})
            names = [source_by_id[i]["connect_name"] for i in ds_ids if i in source_by_id]
            result.add("DB10", sch.get("user_id") or "-", {
                "doc_id": doc_id, "table_name": sch.get("table_name"),
                "datasource_ids": sorted(ds_ids), "connect_names": names,
                "reason": f"同一张表被 {len(ds_ids)} 个数据源引用",
            })

    # ---- DB11 数据源无任何卡片 ----
    for src in snap.sources:
        if cards_per_source.get(src["id"], 0) == 0:
            result.add("DB11", src["user_id"] or "-", {
                "datasource_id": src["id"], "connect_name": src["connect_name"],
                "table_num": src["table_num"],
                "reason": "该数据源下没有任何数据卡片",
            })

    # ---- INFO 共享连接（误报来源，仅提示） ----
    group_members: Dict[Tuple[str, str, str], List[dict]] = defaultdict(list)
    for src in snap.sources:
        group_members[group_key(src["user_id"], src["connect_info_hash"], src["schema_name"])].append(src)
    for key, srcs in group_members.items():
        if len(srcs) < 2:
            continue
        uid, h, sn = key
        owned = sum(len(actual_tables.get(s["id"], set())) for s in srcs)
        result.add("INFO_SHARED_CONN", uid or "-", {
            "connect_info_hash": h or None,
            "schema_name": sn or None,
            "source_count": len(srcs),
            "connect_names": [s["connect_name"] for s in srcs],
            "card_tables_owned": owned,
            "reason": f"{len(srcs)} 个数据源指向同一物理连接，"
                      f"合计持有 {owned} 张卡片（旧脚本会把这部分重复算成脏数据）",
        })


# ============================================================
# 向量库层校验（按 collection 全量扫描，不依赖 datasource_infos）
# ============================================================
def audit_weaviate(snap: Snapshot, result: AuditResult, uuid_owner: Dict[str, str],
                   ds_vectors: Dict[Tuple[str, str], int],
                   simple: bool = False) -> Dict[str, dict]:
    """返回 {class_key: {...}}，并把问题写进 result。

    ds_vectors 会被填充为 {(用户ID, 数据源ID): 向量条数}。
    向量只在「该数据源所属用户的 collection」内计数并带上 owner，
    因此不同用户之间的向量不会被混算。

    simple=True 时进入精简模式：
      - 只扫描 users 表登记过的用户的 collection
      - 只记录 S1/S2/S3 三类与「表/卡片/向量是否对应」直接相关的问题
      - 跳过 WV1~WV7、共享连接提示等所有其他检查
    """
    stats: Dict[str, dict] = {}
    # 大小写不敏感的归属映射：DB 存小写，Weaviate 实际是大写
    class_to_user: Dict[str, str] = {}
    db_class_name: Dict[str, str] = {}   # class_key -> DB 中登记的原名
    for u in snap.all_users:
        if u["class_name"]:
            k = class_key(u["class_name"])
            class_to_user.setdefault(k, u["id"])
            db_class_name.setdefault(k, u["class_name"])

    card_by_uuid: Dict[str, dict] = {}
    for dc in snap.all_datacards:
        if dc["w_uuid"]:
            card_by_uuid.setdefault(dc["w_uuid"], dc)

    valid_source_ids: Set[str] = {s["id"] for s in snap.sources}
    # 数据源 -> 其所属用户，用于把向量归到正确的 (用户, 数据源) 上
    source_owner: Dict[str, str] = {s["id"]: s["user_id"] for s in snap.sources}

    with weaviate_client() as client:
        all_collections = sorted(client.collections.list_all())
        # collection 实际存在集合（大小写不敏感）
        existing_keys: Set[str] = {class_key(c) for c in all_collections}

        if not simple:
            for k, owner in class_to_user.items():
                if snap.scope_user and owner != snap.scope_user:
                    continue
                if k in existing_keys:
                    continue
                result.add("WV7", owner, {
                    "class_name": db_class_name.get(k, k),
                    "reason": "users.weaviate_class_name 有值，但 Weaviate 中没有该 collection",
                })

        for class_name in all_collections:
            k = class_key(class_name)
            if class_name.endswith(FIELD_INDEX_SUFFIX):
                continue  # 字段索引 collection 属于盘点功能，不在本脚本范围
            if not is_card_collection(class_name):
                # 精简模式：非卡片 collection 一概不看
                if not simple and not snap.scope_user:
                    result.add("WV6", "-", {
                        "class_name": class_name,
                        "reason": "非 datacard_datasource__* 命名的 collection，"
                                  "不参与卡片一致性比对（可能是历史遗留或其他业务用途）",
                    })
                continue
            owner = class_to_user.get(k)
            if owner is None:
                if simple or snap.scope_user:
                    continue  # 未登记到 users 表，不在审计范围内
                result.add("WV6", "-", {
                    "class_name": class_name,
                    "reason": "卡片 collection 在 Weaviate 中存在，但没有任何用户的 "
                              "weaviate_class_name 指向它",
                })
                owner = "-"
            elif snap.scope_user and owner != snap.scope_user:
                continue  # 不在本次审计范围内

            # 精简模式下，该 collection 只认它自己登记的用户
            # 但向量上的 datasource_id 可能属于别的用户（串档），需分别归位
            col = client.collections.get(class_name)
            total = 0
            orphan_total = 0
            for obj in iter_objects(col):
                total += 1
                obj_uuid = str(getattr(obj, "uuid", "") or "")
                props = obj.properties or {}
                doc_id = str(props.get("doc_id") or "")
                ds_id = str(props.get("datasource_id") or "")
                if not obj_uuid:
                    continue

                # 逐数据源统计向量数。
                # key 用「数据源真正的所属用户」而非当前 collection 的用户，
                # 串档的向量因此会被算到它应属的用户名下，而不是被忽略。
                # 精简模式只统计仍存在于 datasource_infos 的数据源：
                # 已删除数据源的残留不属于「某个数据源下的向量」，不参与匹配判断。
                if ds_id and (not simple or ds_id in valid_source_ids):
                    ds_owner = source_owner.get(ds_id) or owner
                    dkey = (ds_owner or "-", ds_id)
                    ds_vectors[dkey] = ds_vectors.get(dkey, 0) + 1

                if simple:
                    # 精简模式只关心「多出来的向量」和「重复卡片」。
                    # 已删除数据源的残留（ds_id 不在 datasource_infos）不属于
                    # 任何现存数据源，跳过——否则会凭空多报一批与矩阵无关的问题。
                    card = card_by_uuid.get(obj_uuid)
                    if card is None and ds_id in valid_source_ids:
                        orphan_total += 1
                        result.add("S1", source_owner.get(ds_id) or owner, {
                            "class_name": class_name, "uuid": obj_uuid,
                            "doc_id": doc_id, "datasource_id": ds_id,
                            "reason": "该数据源下的向量多于卡片，多出的向量无对应卡片记录",
                        })
                    continue

                # 记录 uuid 归属，供修复阶段定位要删的向量在哪个 collection
                uuid_owner.setdefault(obj_uuid, class_name)

                card = card_by_uuid.get(obj_uuid)
                if card is None:
                    orphan_total += 1
                    result.add("WV1", owner, {
                        "class_name": class_name, "uuid": obj_uuid,
                        "doc_id": doc_id, "datasource_id": ds_id,
                        "reason": "向量库中存在，但 datacards_datasource 无对应记录"
                                  "（典型成因：数据源被删除时只清了 DB 没清向量）",
                    })
                    continue

                # 向量 datasource_id 缺失或指向已删除的数据源
                if not ds_id:
                    result.add("WV2", owner, {
                        "class_name": class_name, "uuid": obj_uuid,
                        "card_id": card["id"], "datasource_id": ds_id,
                        "new_datasource_id": card["datasource_id"] or "",
                        "reason": "向量 datasource_id 为空，可从卡片回填",
                    })
                elif ds_id not in valid_source_ids:
                    result.add("WV2", owner, {
                        "class_name": class_name, "uuid": obj_uuid,
                        "card_id": card["id"], "datasource_id": ds_id,
                        "new_datasource_id": card["datasource_id"] or "",
                        "reason": "向量 datasource_id 指向的数据源已不存在，"
                                  "将从卡片回填（卡片也无 datasource_id 时将删除该向量）",
                    })

                # 向量 doc_id 与卡片不一致
                if card["doc_id"] and doc_id and doc_id != card["doc_id"]:
                    result.add("WV3", owner, {
                        "class_name": class_name, "uuid": obj_uuid,
                        "card_id": card["id"], "doc_id": doc_id,
                        "new_doc_id": card["doc_id"],
                        "reason": f"向量 doc_id={doc_id} 与卡片 doc_id={card['doc_id']} 不一致",
                    })

            stats[class_key(class_name)] = {"total": total, "orphan": orphan_total,
                                            "owner": owner, "class_name": class_name}

    return stats


def cross_check_vectors(snap: Snapshot, stats: Dict[str, dict],
                        uuid_owner: Dict[str, str],
                        result: AuditResult) -> None:
    """
    卡片侧反查向量：
      WV4 卡片 w_uuid 不在本用户 collection 中
      WV5 卡片 w_uuid 却出现在别的用户 collection 中（数据串档）
    依据 audit_weaviate 扫描时记录的 uuid -> collection 归属。
    """
    class_to_user: Dict[str, str] = {}
    for u in snap.all_users:
        if u["class_name"]:
            class_to_user.setdefault(class_key(u["class_name"]), u["id"])

    for dc in snap.datacards:
        uid = dc["user_id"] or "-"
        if not dc["w_uuid"]:
            continue  # 已由 DB9 覆盖
        holder = uuid_owner.get(dc["w_uuid"])
        if holder is None:
            result.add("WV4", uid, {
                "card_id": dc["id"], "w_uuid": dc["w_uuid"], "doc_id": dc["doc_id"],
                "table_name": dc["table_name"], "connect_name": dc["connect_name"],
                "class_name": _user_class(snap, uid),
                "reason": "数据库有卡片，但所有 collection 里都找不到这个向量",
            })
            continue
        holder_key = class_key(holder)
        holder_owner = class_to_user.get(holder_key)
        if holder_owner is not None and holder_owner != uid:
            result.add("WV5", uid, {
                "card_id": dc["id"], "w_uuid": dc["w_uuid"], "doc_id": dc["doc_id"],
                "table_name": dc["table_name"],
                "owner_user_id": uid, "found_in_class": holder,
                "found_user_id": holder_owner,
                "reason": f"卡片属于用户 {uid}，向量却存放在 {holder}"
                          f"（用户 {holder_owner}）的 collection 中",
            })
            continue
        # 向量在别的 collection：同用户存在多个 collection，或用户 collection 缺失
        own_class = _user_class(snap, uid)
        if own_class and holder_key != class_key(own_class):
            result.add("WV4", uid, {
                "card_id": dc["id"], "w_uuid": dc["w_uuid"], "doc_id": dc["doc_id"],
                "table_name": dc["table_name"], "connect_name": dc["connect_name"],
                "class_name": holder, "expected_class": own_class,
                "reason": f"向量实际位于 {holder}，而非本用户的 {own_class}",
            })


def _user_class(snap: Snapshot, user_id: str) -> Optional[str]:
    for u in snap.all_users:
        if u["id"] == user_id:
            return u["class_name"] or None
    return None


# ============================================================
# 修复
# ============================================================
class Fixer:
    def __init__(self, engine: Engine, snap: Snapshot, result: AuditResult,
                 uuid_owner: Dict[str, str], selected: Set[str],
                 include_unowned: bool = False):
        self.engine = engine
        self.snap = snap
        self.result = result
        self.uuid_owner = uuid_owner
        self.selected = selected
        self.source_by_id = {s["id"]: s for s in snap.sources}
        self.done: Dict[str, int] = defaultdict(int)
        self.failed: List[str] = []
        self.skipped: List[str] = []
        # 无人认领的 collection（Weaviate 里有，但 users.weaviate_class_name 没指向）
        # 默认不删：这类数据可能属于已注销但仍有价值的环境，避免误删
        owned_classes: Set[str] = set()
        for u in snap.all_users:
            if u["class_name"]:
                owned_classes.add(class_key(u["class_name"]))
        self.owned_classes = owned_classes
        self.include_unowned = include_unowned

    # ---------- 向量操作 ----------
    def _partition_owned(self, items: List[Issue]) -> Tuple[List[dict], List[Issue]]:
        """把「位于有主 collection」和「位于无人认领 collection」的问题分开"""
        safe: List[dict] = []
        skipped: List[Issue] = []
        for it in items:
            cls = it.payload.get("class_name")
            if cls and class_key(cls) not in self.owned_classes and not self.include_unowned:
                skipped.append(it)
            else:
                safe.append(it.payload)
        return safe, skipped

    def _delete_vectors(self, items: List[dict], owner_hint: Optional[str] = None) -> int:
        if not items:
            return 0
        by_class: Dict[str, Set[str]] = defaultdict(set)
        for it in items:
            obj_uuid = it.get("uuid") or it.get("w_uuid")
            if not obj_uuid:
                continue
            cls = it.get("class_name") or self.uuid_owner.get(obj_uuid) or owner_hint
            if cls:
                by_class[cls].add(obj_uuid)
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

    def _update_vector_props(self, items: List[dict], build) -> int:
        if not items:
            return 0
        by_class: Dict[str, List[dict]] = defaultdict(list)
        for it in items:
            cls = it.get("class_name")
            if cls and it.get("uuid"):
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
                    props = build(row)
                    if props is None:
                        continue
                    try:
                        col.data.update(row["uuid"], properties=props)
                        ok += 1
                    except Exception as e:
                        self.failed.append(f"修补向量属性 {class_name}/{row['uuid']} 失败: {e}")
        return ok

    # ---------- DB 操作 ----------
    def _delete_cards(self, card_ids: Iterable[int]) -> int:
        ids = sorted({int(i) for i in card_ids if i is not None})
        if not ids:
            return 0
        deleted = 0
        with self.engine.begin() as c:
            for chunk_start in range(0, len(ids), 500):
                chunk = ids[chunk_start:chunk_start + 500]
                placeholders = ",".join(f":id{i}" for i in range(len(chunk)))
                params = {f"id{i}": v for i, v in enumerate(chunk)}
                deleted += c.execute(
                    text(f"DELETE FROM {DATACARDS_TABLE} WHERE id IN ({placeholders})"), params
                ).rowcount
        return deleted

    # ---------- 主流程 ----------
    def run(self) -> Dict[str, int]:
        sel = self.selected

        # --- 精简模式：只处理 S1（多余向量）与 S3（重复卡片） ---
        if "S1" in sel:
            safe, skipped = self._partition_owned(self.result.issues.get("S1", []))
            for it in skipped:
                self.skipped.append(
                    f"{it.payload.get('class_name')}/{it.payload.get('uuid')}："
                    f"位于未登记到 users 表的 collection，默认不删除"
                )
            self.done["S1 删除多余向量"] = self._delete_vectors(safe)
        if "S3" in sel:
            rows = self.result.issues.get("S3", [])
            by_id: Dict[int, dict] = {}
            for iss in rows:
                cid = iss.payload.get("card_id")
                if cid is not None:
                    by_id.setdefault(int(cid), iss.payload)
            if by_id:
                self.done["S3 删除多余向量"] = self._delete_vectors(
                    [{"w_uuid": p.get("w_uuid")} for p in by_id.values()]
                )
                self.done["S3 删除重复卡片"] = self._delete_cards(by_id.keys())

        # --- 先删向量，再删 DB 行：避免产生新的孤儿向量 ---
        wv1 = self.result.issues.get("WV1", [])
        if "WV1" in sel:
            safe, skipped = self._partition_owned(wv1)
            for it in skipped:
                self.skipped.append(
                    f"{it.payload.get('class_name')}/{it.payload.get('uuid')}："
                    f"位于无人认领的 collection，默认不删除（如需清理加 --include-unowned）"
                )
            n = self._delete_vectors(safe)
            self.done["WV1 删除向量"] = n

        card_issues = (self.result.issues.get("DB1", [])
                       + self.result.issues.get("DB2", [])
                       + self.result.issues.get("DB3", [])
                       + self.result.issues.get("DB4", []))
        if card_issues:
            # 同一条卡片可能被多个类型命中，按 card_id 去重后再删
            card_by_id: Dict[int, dict] = {}
            for iss in card_issues:
                cid = iss.payload.get("card_id")
                if cid is not None:
                    card_by_id.setdefault(int(cid), iss.payload)
            if set(["DB1", "DB2", "DB3", "DB4"]) & sel:
                payloads = list(card_by_id.values())
                n = self._delete_vectors(
                    [{"w_uuid": p.get("w_uuid")} for p in payloads],
                    owner_hint=None,
                )
                self.done["DB1~DB4 删除向量"] = n
                d = self._delete_cards(card_by_id.keys())
                self.done["DB1~DB4 删除卡片"] = d

        # --- DB5 回填 user_id ---
        if "DB5" in sel:
            rows = self.result.issues.get("DB5", [])
            by_class: Dict[str, List[dict]] = defaultdict(list)
            for iss in rows:
                by_class[iss.payload["new_user_id"]].append(iss.payload)
            ok = 0
            with self.engine.begin() as c:
                for new_uid, group in by_class.items():
                    ids = [int(p["card_id"]) for p in group]
                    for s in range(0, len(ids), 500):
                        chunk = ids[s:s + 500]
                        ph = ",".join(f":id{i}" for i in range(len(chunk)))
                        params = {f"id{i}": v for i, v in enumerate(chunk)}
                        params["uid"] = new_uid
                        ok += c.execute(
                            text(f"UPDATE {DATACARDS_TABLE} SET user_id = :uid "
                                 f"WHERE id IN ({ph})"), params
                        ).rowcount
            self.done["DB5 回填 user_id"] = ok

        # --- DB6 修正 table_num ---
        if "DB6" in sel:
            rows = self.result.issues.get("DB6", [])
            ok = 0
            with self.engine.begin() as c:
                for p in rows:
                    ok += c.execute(
                        text(f"UPDATE {SOURCES_TABLE} SET table_num = :n WHERE id = :did"),
                        {"n": int(p["real_tables"]), "did": p["datasource_id"]},
                    ).rowcount
            self.done["DB6 修正 table_num"] = ok

        # --- WV2 修补/删除向量 datasource_id ---
        if "WV2" in sel:
            rows = self.result.issues.get("WV2", [])
            patchable = [p for p in rows if p.get("new_datasource_id")]
            deletable = [p for p in rows if not p.get("new_datasource_id")]
            ok = self._update_vector_props(
                patchable,
                lambda r: {"datasource_id": r["new_datasource_id"]},
            )
            self.done["WV2 修补 datasource_id"] = ok
            n = self._delete_vectors(deletable)
            self.done["WV2 无从回填→删除向量"] = n

        # --- WV3 修补向量 doc_id ---
        if "WV3" in sel:
            rows = self.result.issues.get("WV3", [])
            ok = self._update_vector_props(
                rows, lambda r: {"doc_id": r["new_doc_id"]}
            )
            self.done["WV3 修补 doc_id"] = ok

        return dict(self.done)


# ============================================================
# 报告输出
# ============================================================
BAR = "=" * 88


def print_header(title: str) -> None:
    print()
    print(BAR)
    print(title)
    print(BAR)


def print_issues(result: AuditResult, sample: int, show_info: bool) -> None:
    order = [c for c in ISSUE_DEFS if result.count(c) > 0]
    for code in order:
        level, title, fixable, desc = ISSUE_DEFS[code]
        if level == "INFO" and not show_info:
            continue
        items = result.issues[code]
        mark = {"ERROR": "[ERROR]", "WARN": "[WARN ]", "INFO": "[INFO ]"}[level]
        fix_tag = "可自动修复" if fixable else "需人工处理"
        print(f"\n{mark} {code} {title}  x{len(items)}   ({fix_tag})")
        print(f"       {desc}")
        for it in items[:sample]:
            detail = " | ".join(
                f"{k}={v}" for k, v in it.payload.items() if k != "reason"
            )
            print(f"    - [{it.key}] {detail}")
        if len(items) > sample:
            print(f"    ... 其余 {len(items) - sample} 条省略（用 --sample 调整）")


def build_user_summary(snap: Snapshot, stats: Dict[str, dict]) -> List[dict]:
    """按用户汇总，口径与 /datasource_tool 接口一致，便于对照"""
    rows = []
    for u in snap.users:
        uid = u["id"]
        srcs = [s for s in snap.sources if s["user_id"] == uid]
        cards = [d for d in snap.datacards if d["user_id"] == uid]
        # Weaviate 实际 collection 名可能大小写不同，用 class_key 对照
        info = stats.get(class_key(u["class_name"])) if u["class_name"] else None
        rows.append({
            "user_id": uid,
            "username": u["username"],
            "class_name": u["class_name"],
            "sources": len(srcs),
            "table_num_sum": sum(s["table_num"] for s in srcs),
            "cards": len(cards),
            "vectors": info["total"] if info else 0,
            "orphan_vectors": info["orphan"] if info else 0,
            "diff": (info["total"] - len(cards)) if info else 0,
        })
    rows.sort(key=lambda r: -r["vectors"])
    return rows


def print_user_summary(rows: List[dict]) -> None:
    if not rows:
        return
    print_header("按用户汇总（口径对齐 /datasource_tool：数据表 / 数据卡片 / 向量索引）")
    print(f"{'用户名':<24}{'数据源':>6}{'数据表':>8}{'数据卡片':>10}{'向量索引':>10}{'差值':>8}")
    print("-" * 88)
    for r in rows:
        flag = "  <-- 不一致" if r["diff"] != 0 else ""
        print(f"{str(r['username'])[:22]:<24}{r['sources']:>6}{r['table_num_sum']:>8}"
              f"{r['cards']:>10}{r['vectors']:>10}{r['diff']:>8}{flag}")
    print("-" * 88)
    print(f"{'合计':<24}{sum(r['sources'] for r in rows):>6}"
          f"{sum(r['table_num_sum'] for r in rows):>8}"
          f"{sum(r['cards'] for r in rows):>10}"
          f"{sum(r['vectors'] for r in rows):>10}"
          f"{sum(r['diff'] for r in rows):>8}")


def _report_simple_mismatch(result: AuditResult, rows: List[dict],
                           skip_weaviate: bool) -> None:
    """
    根据矩阵结果补齐 S2（卡片多于向量）。
    S1（向量多于卡片）已在扫描时逐条记录，S3（卡片多于表）已在 audit_db 记录。
    """
    if skip_weaviate:
        return
    for r in rows:
        if r["cards"] > r["vectors"]:
            for _ in range(r["cards"] - r["vectors"]):
                result.add("S2", r["user_id"], {
                    "datasource_id": r["datasource_id"],
                    "connect_name": r["connect_name"],
                    "cards": r["cards"], "vectors": r["vectors"],
                    "missing": r["cards"] - r["vectors"],
                    "reason": f"该数据源有 {r['cards']} 张卡片但只有 {r['vectors']} 个向量，"
                              f"缺 {r['cards'] - r['vectors']} 个，需重新向量化",
                })


def build_simple_matrix(snap: Snapshot, stats: Dict[str, dict],
                       ds_vectors: Dict[Tuple[str, str], int]) -> List[dict]:
    """
    精简模式的对齐矩阵：以 users 表中每个用户的每个数据源为一行。

      表结构  该数据源按 (user_id, connect_info_hash, schema_name) 三元组关联到的
             user_datasource_schemas 条数——schema 侧的独立口径
      表      该数据源卡片覆盖的去重 doc_id 数——卡片侧的独立口径
      卡片    datacards_datasource 中该数据源的卡片条数
      向量    该数据源在「其所属用户的向量空间」中实际占用的向量条数

    「表结构」与「表」是同一批表的两个独立读数，正常情况下必然相等；
    不相等说明表结构登记或卡片归属出了问题，由 S4 单独报出。

    完全匹配 = 表结构 == 表 == 卡片 == 向量。
    不涉及无主 collection、不涉及已删除数据源的残留、不涉及跨用户串档。
    """
    username_by_id = {u["id"]: u["username"] for u in snap.all_users}

    cards_by_source: Dict[str, List[dict]] = defaultdict(list)
    for dc in snap.datacards:
        if dc["datasource_id"]:
            cards_by_source[dc["datasource_id"]].append(dc)

    # schema 侧按三元组归到数据源
    schemas_by_triple: Dict[Tuple[str, str, str], Set[str]] = defaultdict(set)
    for sc in snap.schemas:
        schemas_by_triple[group_key(
            sc["user_id"], sc["connect_info_hash"], sc["schema_name"])].add(sc["id"])

    rows: List[dict] = []
    for src in snap.sources:
        uid = src["user_id"] or "-"
        cards = cards_by_source.get(src["id"], [])
        tables = {c["doc_id"] for c in cards if c["doc_id"]}
        key = group_key(src["user_id"], src["connect_info_hash"], src["schema_name"])
        n_schema = len(schemas_by_triple.get(key, set()))
        vec = ds_vectors.get((uid, src["id"]), 0)
        rows.append({
            "user_id": uid,
            "username": username_by_id.get(uid, "-"),
            "datasource_id": src["id"],
            "connect_name": src["connect_name"],
            "db_type": src["db_type"],
            "schemas": n_schema,
            "tables": len(tables),
            "cards": len(cards),
            "vectors": vec,
            "table_num": src["table_num"],
            "matched": n_schema == len(tables) == len(cards) == vec,
        })

    rows.sort(key=lambda r: (r["matched"], r["username"] or "", r["connect_name"] or ""))
    return rows


def print_simple_matrix(rows: List[dict], only_bad: bool = False) -> None:
    bad = [r for r in rows if not r["matched"]]
    shown = bad if only_bad else rows
    print()
    print("=" * 104)
    print("按用户 / 数据源校验：表结构 · 表 · 数据卡片 · 向量")
    print("  表结构 = 该数据源三元组关联到的 user_datasource_schemas 条数")
    print("  表     = 该数据源卡片覆盖的去重 doc_id 数")
    print("  四者完全相等才算对应")
    print("=" * 104)
    print(f"数据源总数 {len(rows)}    完全匹配 {len(rows) - len(bad)}    不匹配 {len(bad)}")
    print()
    if not shown:
        print("所有用户的每个数据源，表结构 / 表 / 卡片 / 向量完全对应。")
        return
    print(f"{'用户名':<16}{'数据源':<26}{'库类型':<10}{'表结构':>7}{'表':>5}{'卡片':>6}{'向量':>6}  结论")
    print("-" * 104)
    for r in shown:
        notes = []
        if r["schemas"] != r["tables"]:
            notes.append(f"表结构{r['schemas']}≠表{r['tables']}")
        if r["tables"] != r["cards"]:
            notes.append(f"表{r['tables']}≠卡片{r['cards']}")
        if r["cards"] != r["vectors"]:
            notes.append(f"卡片{r['cards']}≠向量{r['vectors']}")
        verdict = "匹配" if not notes else "不匹配: " + "; ".join(notes)
        print(f"{str(r['username'])[:14]:<16}{str(r['connect_name'])[:24]:<26}"
              f"{str(r['db_type'])[:8]:<10}{r['schemas']:>7}{r['tables']:>5}{r['cards']:>6}"
              f"{r['vectors']:>6}  {verdict}")
    print("-" * 104)


def build_source_matrix(snap: Snapshot, stats: Dict[str, dict],
                        ds_vectors: Dict[Tuple[str, str], int]) -> List[dict]:
    """
    逐数据源对齐矩阵：以 (用户, 数据源) 为一行，比对四个口径
      表结构(claimed) = 该数据源卡片覆盖的去重 doc_id 数
      数据卡片        = 该数据源的卡片条数
      向量            = 该数据源在 Weaviate 中实际占用的向量条数
      table_num       = datasource_infos 里存的冗余计数
    aligned = 表结构 == 卡片 == 向量
    """
    username_by_id = {u["id"]: u["username"] for u in snap.all_users}

    cards_by_source: Dict[str, List[dict]] = defaultdict(list)
    for dc in snap.datacards:
        if dc["datasource_id"]:
            cards_by_source[dc["datasource_id"]].append(dc)

    rows: List[dict] = []
    for src in snap.sources:
        cards = cards_by_source.get(src["id"], [])
        tables = {c["doc_id"] for c in cards if c["doc_id"]}
        vec = ds_vectors.get((src["user_id"] or "-", src["id"]), 0)
        aligned = len(tables) == len(cards) == vec
        num_ok = src["table_num"] == len(tables)
        rows.append({
            "user_id": src["user_id"],
            "username": username_by_id.get(src["user_id"] or "", "-"),
            "datasource_id": src["id"],
            "connect_name": src["connect_name"],
            "db_type": src["db_type"],
            "table_num": src["table_num"],
            "tables": len(tables),
            "cards": len(cards),
            "vectors": vec,
            "aligned": aligned,
            "num_ok": num_ok,
        })

    rows.sort(key=lambda r: (not r["aligned"], not r["num_ok"],
                             r["username"] or "", r["connect_name"] or ""))
    return rows


def print_source_matrix(rows: List[dict], only_bad: bool = False) -> None:
    if not rows:
        return
    bad = [r for r in rows if not (r["aligned"] and r["num_ok"])]
    shown = bad if only_bad else rows
    print_header(f"逐数据源对齐矩阵（表结构 / 数据卡片 / 向量）"
                 f"—— 共 {len(rows)} 个数据源，{len(bad)} 个存在偏差")
    if only_bad:
        print(f"（--only-bad-sources：只列出有偏差的数据源，共 {len(bad)} 条）\n")
    if not shown:
        print("所有数据源三层完全对齐，无需处理。\n")
        return
    print(f"{'用户名':<18}{'数据源':<28}{'表结构':>7}{'卡片':>7}{'向量':>7}"
          f"{'table_num':>11}  状态")
    print("-" * 96)
    for r in shown:
        notes = []
        if not r["aligned"]:
            parts = []
            if r["tables"] != r["cards"]:
                parts.append(f"表{r['tables']}≠卡{r['cards']}")
            if r["cards"] != r["vectors"]:
                parts.append(f"卡{r['cards']}≠向量{r['vectors']}")
            if r["tables"] != r["vectors"]:
                parts.append(f"表{r['tables']}≠向量{r['vectors']}")
            notes.append("; ".join(parts))
        if not r["num_ok"]:
            notes.append(f"table_num={r['table_num']} 应为 {r['tables']}")
        status = "OK" if not notes else " | ".join(notes)
        print(f"{str(r['username'])[:16]:<18}{str(r['connect_name'])[:26]:<28}"
              f"{r['tables']:>7}{r['cards']:>7}{r['vectors']:>7}"
              f"{r['table_num']:>11}  {status}")
    print("-" * 96)
    print(f"合计：数据源 {len(rows)}，完全对齐 {len(rows) - len(bad)}，有偏差 {len(bad)}")


def print_deleted_source_residue(rows: List[dict], snap: Snapshot,
                                 ds_vectors: Dict[str, int],
                                 result: AuditResult) -> None:
    """
    单独说明「数据源已被整体删除」的向量残留。
    这类向量在 datasource_infos 里已经没有对应数据源，因此不会出现在
    逐数据源矩阵的任何一行里，只能在这里按 datasource_id 维度提示。
    归属用户从向量所在的 collection 反查（卡片已被清理，无法从卡片侧反查）。
    """
    known_all = {s["id"] for s in snap.sources}
    dangling = {
        ds: n for (_uid, ds), n in ds_vectors.items() if ds not in known_all
    }
    if not dangling:
        return

    class_to_user: Dict[str, str] = {}
    for u in snap.all_users:
        if u["class_name"]:
            class_to_user.setdefault(class_key(u["class_name"]), u["id"])
    username_by_id = {u["id"]: u["username"] for u in snap.all_users}

    # 用 WV1 明细反查每个 datasource_id 落在哪些 collection
    ds_to_classes: Dict[str, Set[str]] = defaultdict(set)
    for iss in result.issues.get("WV1", []):
        p = iss.payload
        if p.get("datasource_id"):
            ds_to_classes[p["datasource_id"]].add(p.get("class_name") or "-")

    print_header("数据源已整体删除后的向量残留"
                 "（这些数据源已不在 datasource_infos 中，故不计入上面的矩阵）")
    print(f"{'向量条数':>8}  {'数据源ID':<38}{'归属用户':<20}")
    print("-" * 96)
    for ds, n in sorted(dangling.items(), key=lambda kv: -kv[1]):
        owners = set()
        for cn in ds_to_classes.get(ds, set()):
            uid = class_to_user.get(class_key(cn))
            if uid:
                owners.add(str(username_by_id.get(uid, uid[:8])))
        owner = ", ".join(sorted(owners)) if owners else "未知（collection 无主或已跳过扫描）"
        print(f"{n:>8}  {ds:<38}{owner[:18]:<20}")
    print("-" * 96)
    print(f"合计：{len(dangling)} 个已删除数据源残留 {sum(dangling.values())} 条向量")
    print("清理方式：--fix（会在 WV1 中一并处理；无主 collection 需加 --include-unowned）")


# ============================================================
# main
# ============================================================
def main() -> int:
    ap = argparse.ArgumentParser(
        description="OntiCards 数据源 / 数据卡片 / 向量库 一致性全量校验与修复",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--fix", action="store_true", help="执行修复（默认只检查，不做任何写操作）")
    ap.add_argument("--yes", action="store_true", help="配合 --fix 使用，跳过交互确认")
    ap.add_argument("--user", help="只审计指定 user_id")
    ap.add_argument("--only", help="只处理指定问题类型，逗号分隔，如 DB1,DB3,WV1")
    ap.add_argument("--skip-weaviate", action="store_true", help="跳过向量库校验（只查数据库）")
    ap.add_argument("--include-unowned", action="store_true",
                    help="--fix 时连同「无人认领 collection」里的孤儿向量一起删除"
                         "（默认不动这些 collection，避免误删他人环境数据）")
    ap.add_argument("--json", dest="json_out", help="把完整结果导出为 JSON 文件")
    ap.add_argument("--sample", type=int, default=10, help="每类问题最多打印多少条明细，默认 10")
    ap.add_argument("--show-info", action="store_true", help="同时打印 INFO 级提示（如共享连接）")
    ap.add_argument("--only-bad-sources", action="store_true",
                    help="逐数据源矩阵只列出有偏差的数据源（其余视为对齐）")
    ap.add_argument("--simple", action="store_true",
                    help="(默认) 只校验 users 表中每个用户的每个数据源，"
                         "判断「表结构 / 表 / 数据卡片 / 向量」是否对应")
    ap.add_argument("--full", action="store_true",
                    help="全量巡检：额外统计无主 collection、已删除数据源的向量残留、"
                         "用户汇总等所有检查项（输出较长）")
    args = ap.parse_args()

    # 默认精简模式：只校验 users 表登记过的用户的每个数据源，
    # 判断「表结构 / 表 / 数据卡片 / 向量」是否对应。其余一律不输出。
    # --full 才切换到全量巡检（含无主 collection、已删除数据源残留等）。
    args = ap.parse_args()
    args.simple = not args.full

    simple_codes = {"S1", "S2", "S3", "S4"}
    if args.simple:
        default_codes = simple_codes
    else:
        default_codes = set(ISSUE_DEFS.keys())
    selected = (
        {c.strip().upper() for c in args.only.split(",") if c.strip()}
        if args.only else default_codes
    )
    unknown = selected - set(ISSUE_DEFS.keys())
    if unknown:
        print(f"[错误] 未知的 --only 取值: {', '.join(sorted(unknown))}")
        print(f"       可用取值: {', '.join(ISSUE_DEFS.keys())}")
        return 2

    print(BAR)
    title = "OntiCards 数据源 / 数据卡片 / 向量 一致性校验"
    if args.simple:
        title += "（精简模式）"
    print(title + ("（修复模式）" if args.fix else "（只检查模式）"))
    print(BAR)
    print(f"数据库     : {DATABASE_URL.split('@')[-1]}")
    print(f"Weaviate   : {WEAVIATE_URL}:{WEAVIATE_GRPC_PORT}"
          f"{'（本次跳过）' if args.skip_weaviate else ''}")
    print(f"范围       : {'用户 ' + args.user if args.user else '全部用户'}")
    print(f"问题过滤   : {', '.join(sorted(selected)) if args.only else '全部'}")

    engine = create_engine(DATABASE_URL, future=True)
    result = AuditResult()
    uuid_owner: Dict[str, str] = {}

    print("\n[1/3] 加载数据库快照 ...")
    snap = load_snapshot(engine, args.user)
    print(f"      users={len(snap.users)}  数据源={len(snap.sources)}  "
          f"表结构={len(snap.schemas)}  数据卡片={len(snap.datacards)}")

    print("[2/3] 比对表 / 数据卡片 / 向量 ...")
    audit_db(snap, result, simple=args.simple)

    stats: Dict[str, dict] = {}
    ds_vectors: Dict[Tuple[str, str], int] = {}
    if args.skip_weaviate:
        print("      跳过向量库（--skip-weaviate）：向量列恒为 0，不做结论")
    else:
        stats = audit_weaviate(snap, result, uuid_owner, ds_vectors, simple=args.simple)
        if not args.simple:
            cross_check_vectors(snap, stats, uuid_owner, result)

    # 精简模式：只按 (用户, 数据源) 给出三者是否对应
    if args.simple:
        matrix_rows = build_simple_matrix(snap, stats, ds_vectors)
        _report_simple_mismatch(result, matrix_rows, args.skip_weaviate)

    print("[3/3] 输出结论 ...")
    if args.simple:
        print_simple_matrix(matrix_rows, only_bad=args.only_bad_sources)
    else:
        user_rows = build_user_summary(snap, stats)
        print_user_summary(user_rows)
        matrix_rows = build_source_matrix(snap, stats, ds_vectors)
        print_source_matrix(matrix_rows, only_bad=args.only_bad_sources)
        if not args.skip_weaviate:
            print_deleted_source_residue(matrix_rows, snap, ds_vectors, result)
    print_issues(result, args.sample, args.show_info or args.simple)

    # ---- 汇总统计 ----
    if args.simple:
        matched = sum(1 for r in matrix_rows if r["matched"])
        print_header("结论")
        print(f"  users 表中的用户：{len(snap.users)}")
        print(f"  数据源总数：{len(matrix_rows)}")
        print(f"  表结构 / 表 / 卡片 / 向量 四者完全对应：{matched}")
        print(f"  不对应：{len(matrix_rows) - matched}")
        if args.skip_weaviate:
            print("  注意：本次 --skip-weaviate，向量列按 0 计算，结论不成立")
    else:
        print_header("校验结果总结")
        errs = sum(result.count(c) for c in ISSUE_DEFS
                   if ISSUE_DEFS[c][0] == "ERROR" and c in selected)
        warns = sum(result.count(c) for c in ISSUE_DEFS
                    if ISSUE_DEFS[c][0] == "WARN" and c in selected)
        fixable = sum(result.count(c) for c in ISSUE_DEFS
                      if ISSUE_DEFS[c][2] and c in selected)
        print(f"  选中范围内：ERROR {errs} 条 / WARN {warns} 条 / 其中可自动修复 {fixable} 条")

    errs = sum(result.count(c) for c in ISSUE_DEFS
               if ISSUE_DEFS[c][0] == "ERROR" and c in selected)
    warns = sum(result.count(c) for c in ISSUE_DEFS
                if ISSUE_DEFS[c][0] == "WARN" and c in selected)
    fixable = sum(result.count(c) for c in ISSUE_DEFS
                  if ISSUE_DEFS[c][2] and c in selected)

    if args.json_out:
        payload = {
            "mode": "fix" if args.fix else "check",
            "simple": args.simple,
            "database_url": DATABASE_URL,
            "weaviate": WEAVIATE_URL,
            "summary": {
                "users": len(snap.users),
                "sources": len(snap.sources),
                "schemas": len(snap.schemas),
                "datacards": len(snap.datacards),
                "vectors": sum(v["total"] for v in stats.values()),
                "matched_sources": sum(1 for r in matrix_rows if r.get("matched", True)),
                "errors": errs,
                "warnings": warns,
            },
            "source_matrix": matrix_rows,
            "issues": {
                c: {"level": ISSUE_DEFS[c][0], "title": ISSUE_DEFS[c][1],
                    "fixable": ISSUE_DEFS[c][2], "description": ISSUE_DEFS[c][3],
                    "count": result.count(c), "items": [i.payload for i in result.issues.get(c, [])]}
                for c in selected
            },
        }
        if not args.simple:
            payload["collection_stats"] = stats
            payload["user_summary"] = build_user_summary(snap, stats)
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        print(f"\n  JSON 报告已导出：{args.json_out}")

    # ---- 修复 ----
    if not args.fix:
        if args.simple:
            print("\n本次只做检查，未做任何写操作。")
            if any(result.count(c) > 0 for c in simple_codes):
                print("需要清理时执行：python consistency_audit.py --fix --yes")
            else:
                print("无需修复。")
            return 0 if errs == 0 else 1
        fixable_list = [c for c in ISSUE_DEFS if ISSUE_DEFS[c][2] and result.count(c) > 0]
        print("\n当前为【只检查模式】，未做任何写操作。")
        if fixable_list:
            print("可修复的问题类型：" + ", ".join(fixable_list))
            print("执行修复：python consistency_audit.py --fix"
                  + (" --only " + ",".join(fixable_list) if args.only else ""))
        else:
            print("当前没有任何可自动修复的问题。")
        return 0 if errs == 0 else 1

    fix_codes = {c for c in ISSUE_DEFS if ISSUE_DEFS[c][2]} & selected
    pending = {c: result.count(c) for c in fix_codes if result.count(c) > 0}
    if not pending:
        print("\n【修复模式】没有需要修复的问题，未做任何写操作。")
        return 0 if errs == 0 else 1

    print_header("修复计划")
    for c, n in sorted(pending.items()):
        print(f"  {c} {ISSUE_DEFS[c][1]}  x{n}")

    if not args.yes:
        try:
            ans = input("\n确认执行以上修复？(输入 yes 继续): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            return 1
        if ans != "yes":
            print("已取消，未做任何修改。")
            return 1

    fixer = Fixer(engine, snap, result, uuid_owner, selected,
                   include_unowned=args.include_unowned)
    done = fixer.run()

    print_header("修复结果")
    for k, v in done.items():
        print(f"  {k}: {v}")
    if fixer.failed:
        print(f"\n  失败 {len(fixer.failed)} 项：")
        for m in fixer.failed[:20]:
            print(f"    - {m}")
        if len(fixer.failed) > 20:
            print(f"    ... 其余 {len(fixer.failed) - 20} 条省略")
    if fixer.skipped:
        print(f"\n  已跳过 {len(fixer.skipped)} 条（位于无人认领的 collection，默认不删）：")
        for m in fixer.skipped[:5]:
            print(f"    - {m}")
        if len(fixer.skipped) > 5:
            print(f"    ... 其余 {len(fixer.skipped) - 5} 条省略")
        print(f"    如确认要清理：加 --include-unowned 重新执行")

    print("\n建议重新执行一次校验以确认结果：python consistency_audit.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())