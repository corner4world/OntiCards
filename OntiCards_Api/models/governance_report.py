"""
@File: governance_report.py
@Description: 治理报告模型
@Author: 韩小豪 849631113@qq.com
@Create: 2026-06-01
"""

from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy import func, text
from extensions.ext_database import db


class GovernanceReport(db.Model):
    """治理报告模型"""
    __tablename__ = 'governance_reports'

    __table_args__ = (

        db.Index('idx_governance_reports_user_id', 'user_id'),

        db.Index('idx_governance_reports_datasource_id', 'datasource_id'),

        {'comment': '数据治理报告表 - 存储治理盘点生成的报告，包含质量评分、评级、执行明细及导出文件信息'},

    )
    # 评级常量
    GRADE_EXCELLENT = '优秀'
    GRADE_GOOD = '良好'
    GRADE_AVERAGE = '一般'
    GRADE_POOR = '较差'
    GRADE_BAD = '差'

    GRADES = [GRADE_EXCELLENT, GRADE_GOOD, GRADE_AVERAGE, GRADE_POOR, GRADE_BAD]

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text('uuid_generate_v4()'),
        comment='报告唯一标识符'
    )
    user_id = db.Column(
        UUID(as_uuid=True),
        nullable=False,
        comment='生成该报告的用户ID'
    )
    datasource_id = db.Column(
        UUID(as_uuid=True),
        nullable=True,
        comment='报告对应的数据源ID'
    )
    report_name = db.Column(
        db.String(255),
        nullable=True,
        comment='报告显示名称'
    )
    execution_time = db.Column(
        db.TIMESTAMP(),
        server_default=func.current_timestamp(),
        nullable=False,
        comment='报告执行/生成时间'
    )
    scope_tables = db.Column(
        db.ARRAY(db.Text),
        nullable=True,
        comment='本次盘点涉及的表名数组'
    )
    rules_applied = db.Column(
        db.Integer,
        nullable=True,
        comment='本次应用/执行的规则总数'
    )
    include_quality = db.Column(
        db.Boolean,
        default=True,
        comment='报告是否包含数据质量检测结果'
    )
    include_basic_audit = db.Column(
        db.Boolean,
        default=False,
        comment='报告是否包含基础空值检测结果'
    )
    include_relationship = db.Column(
        db.Boolean,
        default=False,
        comment='报告是否包含表关系发现结果'
    )
    quality_score = db.Column(
        db.Numeric(5, 2),
        nullable=True,
        comment='数据质量综合评分，范围0-100'
    )
    grade = db.Column(
        db.String(20),
        nullable=True,
        comment='数据质量评级: 优秀(95+), 良好(85-94), 一般(70-84), 较差(60-69), 差(<60)'
    )
    # 基础空值检测完整结果（以表为单位，每表包含所有列的检测结果）
    basic_audit_result = db.Column(
        JSONB,
        nullable=True,
        comment='基础空值检测完整结果(JSON): 以表为单位，每表包含该表所有列的null/empty统计'
    )
    # 基础空值检测执行明细（从 rule_execution_results 表查询 execution_source='basic_audit' 的记录）
    basic_audit_detail = db.Column(
        JSONB,
        nullable=True,
        comment='基础空值检测执行明细(JSON): 从rule_execution_results表查询execution_source=basic_audit的记录'
    )
    # 关系盘点完整结果（直接复用全域盘点返回值）
    full_relation_discovery = db.Column(
        JSONB,
        nullable=True,
        comment='关系盘点完整结果(JSON): 直接复用全域盘点discover_all_relationships返回值，包含relationships、cards、statistics等'
    )
    # 基于规则库的质检完整结果（仅保留有 rule_id 的规则执行结果）
    quality_audit_result = db.Column(
        JSONB,
        nullable=True,
        comment='基于规则库质检完整结果(JSON): 仅包含有rule_id的规则执行结果，用于追溯'
    )
    summary = db.Column(
        JSONB,
        nullable=True,
        comment='报告汇总数据(JSON): 包含规则执行统计、分类统计等'
    )
    details = db.Column(
        JSONB,
        nullable=True,
        comment='报告详细结果(JSON): 包含执行明细、问题列表、建议等'
    )
    # 导出文件路径
    exported_file_path = db.Column(
        db.String(512),
        nullable=True,
        comment='导出的报告文件完整路径'
    )
    exported_file_type = db.Column(
        db.String(20),
        nullable=True,
        comment='导出文件类型: pdf=PDF格式, excel=Excel格式, docx=Word格式'
    )
    exported_file_name = db.Column(
        db.String(255),
        nullable=True,
        comment='导出文件的用户友好显示名称，如"数据治理报告_20260629.docx"'
    )
    # 文件元信息
    file_size = db.Column(
        db.BigInteger,
        nullable=True,
        comment='导出文件大小，单位字节'
    )
    file_created_at = db.Column(
        db.TIMESTAMP(),
        nullable=True,
        comment='导出文件生成时间'
    )
    file_status = db.Column(
        db.String(20),
        default='pending',
        nullable=True,
        comment='文件生成状态: pending=待生成, generating=生成中, completed=已完成, failed=失败'
    )
    file_error_msg = db.Column(
        db.Text,
        nullable=True,
        comment='文件生成失败时的错误信息'
    )
    created_at = db.Column(
        db.TIMESTAMP(),
        server_default=func.current_timestamp(),
        nullable=False,
        comment='报告记录创建时间'
    )

    # 完整执行接口返回值（三大模块完整数据，作为报告生成的唯一真实数据源）
    execution_response = db.Column(
        JSONB,
        nullable=True,
        comment='执行接口完整返回值(JSON): 包含 basic_audit / quality_audit / relation_discovery / summary，作为报告生成的唯一真实数据源'
    )

    # 关联执行结果
    execution_results = db.relationship(
        'RuleExecutionResult',
        backref='report',
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

    def to_summary_dict(self, datasource_dict=None):
        """轻量级摘要，用于报告列表页。datasource_dict 为 {datasource_id: DatasourceInfo} 的映射"""
        ds = datasource_dict.get(self.datasource_id) if datasource_dict and self.datasource_id else None
        return {
            'id': str(self.id),
            'datasource_id': str(self.datasource_id) if self.datasource_id else None,
            'datasource_name': ds.connect_name if ds else None,
            'database_name': ds.database_name if ds else None,
            'schema_name': ds.schema_name if ds else None,
            'report_name': self.report_name,
            'execution_time': self._format_datetime(self.execution_time),
            'rules_applied': self.rules_applied,
            'quality_score': float(self.quality_score) if self.quality_score else None,
            'grade': self.grade,
            'include_quality': self.include_quality,
            'include_basic_audit': self.include_basic_audit,
            'include_relationship': self.include_relationship,
            'file_status': self.file_status,
            'has_export': bool(self.exported_file_path) and self.file_status == 'completed',
            'exported_file_name': self.exported_file_name,
            'exported_file_type': self.exported_file_type,
            'created_at': self._format_datetime(self.created_at),
        }

    def to_dict(self, include_details=False):
        result = {
            'id': str(self.id),
            'user_id': str(self.user_id) if self.user_id else None,
            'datasource_id': str(self.datasource_id) if self.datasource_id else None,
            'report_name': self.report_name,
            'execution_time': self._format_datetime(self.execution_time),
            'scope_tables': self.scope_tables,
            'rules_applied': self.rules_applied,
            'include_quality': self.include_quality,
            'include_basic_audit': self.include_basic_audit,
            'include_relationship': self.include_relationship,
            'quality_score': float(self.quality_score) if self.quality_score else None,
            'grade': self.grade,
            'basic_audit_result': self.basic_audit_result,
            'basic_audit_detail': self.basic_audit_detail,
            'full_relation_discovery': self.full_relation_discovery,
            'quality_audit_result': self.quality_audit_result,
            'summary': self.summary,
            'created_at': self._format_datetime(self.created_at),
            # 导出文件信息
            'exported_file_path': self.exported_file_path,
            'exported_file_type': self.exported_file_type,
            'exported_file_name': self.exported_file_name,
            'file_size': self.file_size,
            'file_created_at': self._format_datetime(self.file_created_at),
            'file_status': self.file_status,
            'file_error_msg': self.file_error_msg,
            'has_export': bool(self.exported_file_path) and self.file_status == 'completed'
        }
        if include_details:
            result['details'] = self.details
            # 返回该报告的所有历史导出文件列表
            from models.governance_report_file import GovernanceReportFile
            history_files = GovernanceReportFile.query.filter_by(report_id=self.id).order_by(
                GovernanceReportFile.created_at.desc()
            ).all()
            result['history_files'] = [f.to_dict() for f in history_files]
        return result
