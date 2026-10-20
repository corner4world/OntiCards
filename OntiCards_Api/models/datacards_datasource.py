"""
 @File: datacards_datasource.py
 @Description: 多数据源对接-数据卡片对象
 @Author: 韩小豪 849631113@qq.com
 @Create: 2025-09-25 14:43
"""
from sqlalchemy import Column, Integer, String, Text, func, Index
from sqlalchemy.dialects.postgresql import UUID

from extensions.ext_database import db
from models.utils import format_datetime

class DataCardDataSource(db.Model):
    __tablename__ = 'datacards_datasource'

    id = Column(Integer, primary_key=True, autoincrement=True, comment='主键ID')

    # 业务字段
    doc_id = Column(String(255), nullable=False, index=True, comment="文档ID")
    w_uuid = Column(String(255), nullable=False, comment="Weaviate UUID")
    card_data = Column(Text, nullable=False, comment="卡片数据(JSON字符串)")

    # 新增字段：用于快速查询和区分不同用户的数据（允许为NULL以保持向后兼容）
    user_id = Column(UUID(as_uuid=True), nullable=True, comment="用户ID（冗余字段，用于快速查询）")
    datasource_id = Column(UUID(as_uuid=True), nullable=True, comment="数据源ID（冗余字段，用于快速查询）")
    table_name = Column(String(255), nullable=True, comment="表名（冗余字段，用于快速查询）")
    connect_name = Column(String(255), nullable=True, comment="连接名称（冗余字段，便于显示）")

    # 审计字段
    created_at = Column(
        db.TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="创建时间"
    )
    updated_at = Column(
        db.TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment='修改时间（触发器或应用更新）'
    )

    # 添加联合索引以提升查询性能
    __table_args__ = (
        Index('idx_datasource_table', 'datasource_id', 'table_name'),
        Index('idx_user_datasource', 'user_id', 'datasource_id'),

        db.Index('idx_datacards_datasource_datasource_id', 'datasource_id'),
        db.Index('idx_datacards_datasource_table_name', 'table_name'),
        db.Index('idx_datacards_datasource_user_id', 'user_id'),
        {'comment': '数据卡片-数据源关联表 - 存储数据卡片与数据源、物理表的绑定关系及卡片内容，并冗余关键字段便于快速查询'},
    )

    def to_dict(self):
        return {
            "id": self.id,
            "doc_id": self.doc_id,
            "w_uuid": self.w_uuid,
            "card_data": self.card_data,  # 这里是 JSON 字符串
            "user_id": str(self.user_id) if self.user_id else None,
            "datasource_id": str(self.datasource_id) if self.datasource_id else None,
            "table_name": self.table_name,
            "connect_name": self.connect_name,
            "created_at": format_datetime(self.created_at),
            "updated_at": format_datetime(self.updated_at),
        }
