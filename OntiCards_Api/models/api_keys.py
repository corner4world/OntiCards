"""
 @File: api_keys.py
 @Description: api_key 实体类
 @Author: 韩小豪 849631113@qq.com
 @Create: 2025-12-29 13:55
"""

import uuid
from datetime import datetime

from sqlalchemy.dialects.postgresql import UUID

from extensions.ext_database import db


class ApiKey(db.Model):
    __tablename__ = "api_keys"

    __table_args__ = (
        db.Index('idx_api_keys_status_expires', 'status', 'expires_at'),
        db.Index('uq_api_keys_plain', 'api_key', unique=True),
        db.Index('idx_api_keys_user_id', 'user_id'),
        {'comment': 'API 密钥表 - 存储第三方平台（如 Coze / HiAgent）调用聚合检索接口所用的密钥，含状态、有效期与最近使用时间'},
    )
    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        comment='主键 ID'
    )

    user_id = db.Column(
        UUID(as_uuid=True),
        nullable=False,
        comment='所属用户 ID'
    )

    name = db.Column(
        db.String(64),
        nullable=False,
        comment='Key 名称/备注'
    )

    api_key = db.Column(
        db.String(128),
        nullable=False,
        comment='API Key 明文字符串（注意避免在日志中输出）'
    )

    status = db.Column(
        db.String(16),
        nullable=False,
        default="active",
        comment='状态：active=可用，disabled=已禁用'
    )

    expires_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
        comment='过期时间'
    )

    last_used_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
        comment='最近一次成功调用接口的时间'
    )

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        comment='创建时间'
    )

    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        comment='更新时间'
    )

    def __repr__(self):
        return f"<ApiKey id={self.id} name={self.name} user_id={self.user_id}>"
