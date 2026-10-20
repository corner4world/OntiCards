"""
@File: governance_rule_library.py
@Description: 治理规则库模型
@Author: 韩小豪 849631113@qq.com
@Create: 2026-06-01
"""

from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import func, text
from extensions.ext_database import db


class GovernanceRuleLibrary(db.Model):
    """治理规则库模型"""
    __tablename__ = 'governance_rule_libraries'

    __table_args__ = (

        db.Index('idx_gr_lib_datasource_created', 'datasource_id', 'created_by'),

        {'comment': '数据治理规则库表 - 存储用户创建的规则库，用于管理一组相关的治理规则'},

    )
    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text('uuid_generate_v4()'),
        comment='规则库唯一标识符'
    )
    name = db.Column(
        db.String(100),
        nullable=False,
        comment='规则库名称'
    )
    description = db.Column(
        db.Text,
        nullable=True,
        comment='规则库描述说明'
    )
    status = db.Column(
        db.String(20),
        default='active',
        comment='规则库状态: active=启用, inactive=停用'
    )
    created_by = db.Column(
        UUID(as_uuid=True),
        nullable=False,
        comment='创建该规则库的用户ID'
    )
    created_at = db.Column(
        db.TIMESTAMP(),
        server_default=func.current_timestamp(),
        nullable=False,
        comment='规则库创建时间'
    )
    updated_at = db.Column(
        db.TIMESTAMP(),
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
        nullable=False,
        comment='规则库最后更新时间'
    )

    # 关联数据源（必须关联，删除数据源时级联删除规则库）
    datasource_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey('datasource_infos.id', ondelete='CASCADE'),
        nullable=False,
        comment='规则库绑定的数据源ID，必须关联，删除数据源时规则库会被级联删除'
    )

    # 关联规则
    rules = db.relationship(
        'GovernanceRule',
        backref='library',
        lazy='dynamic',
        cascade='all, delete-orphan'
    )

    def _format_datetime(self, value):
        """安全格式化时间字段，支持 datetime 对象或字符串"""
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if hasattr(value, 'isoformat'):
            return value.isoformat()
        return str(value)

    def to_dict(self):
        return {
            'id': str(self.id),
            'name': self.name,
            'description': self.description,
            'status': self.status,
            'created_by': str(self.created_by) if self.created_by else None,
            'datasource_id': str(self.datasource_id) if self.datasource_id else None,
            'created_at': self._format_datetime(self.created_at),
            'updated_at': self._format_datetime(self.updated_at),
            'rule_count': self.rules.count() if self.rules else 0
        }
