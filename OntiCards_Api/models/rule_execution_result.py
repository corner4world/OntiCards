"""
@File: rule_execution_result.py
@Description: 规则执行结果模型
@Author: 韩小豪 849631113@qq.com
@Create: 2026-06-01
"""

from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy import func, text
from extensions.ext_database import db
from models.utils import format_datetime


class RuleExecutionResult(db.Model):
    """规则执行结果模型"""
    __tablename__ = 'rule_execution_results'

    __table_args__ = (

        db.Index('idx_rule_execution_results_report_id', 'report_id'),

        db.Index('idx_rule_execution_results_rule_id', 'rule_id'),

        db.Index('idx_rule_execution_results_library_id', 'library_id'),

        db.Index('idx_rule_execution_results_status', 'status'),

        db.Index('idx_rule_execution_results_rule_mode', 'rule_mode'),

        db.Index('idx_rule_execution_results_source', 'report_id', 'execution_source'),

        {'comment': '规则执行结果表 - 存储每条规则在盘点时的执行结果，包括通过/失败统计和失败样本'},

    )
    # 状态常量
    STATUS_PASSED = 'passed'
    STATUS_FAILED = 'failed'
    STATUS_ERROR = 'error'

    STATUSES = [STATUS_PASSED, STATUS_FAILED, STATUS_ERROR]

    # 执行模式常量（与 SQL 注释中的三个值保持一致）
    MODE_SCOPED_SINGLE = 'scoped_single'         # 有 target_table + target_column（单列/单条件）
    MODE_SCOPED_MULTI_COND = 'scoped_multi_cond' # 有 target_table + conditions_config（多条件）
    MODE_UNSCOPED = 'unscoped'                  # 无 target_table（全局规则）

    # 执行来源常量
    SOURCE_RULE_LIBRARY = 'rule_library'  # 规则库质检
    SOURCE_BASIC_AUDIT = 'basic_audit'    # 基础空值检测

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text('uuid_generate_v4()'),
        comment='执行结果唯一标识符'
    )
    report_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey('governance_reports.id', ondelete='CASCADE'),
        nullable=True,
        comment='所属报告ID，删除报告时会级联删除该结果'
    )
    library_id = db.Column(
        UUID(as_uuid=True),
        nullable=True,
        comment='规则库ID，用于追溯执行结果来自哪个规则库'
    )
    rule_id = db.Column(
        UUID(as_uuid=True),
        nullable=True,
        comment='执行的规则ID'
    )
    rule_name = db.Column(
        db.String(255),
        nullable=True,
        comment='执行的规则名称'
    )
    rule_type = db.Column(
        db.String(50),
        nullable=True,
        comment='执行的规则类型: null_check=空值检测, unique=唯一性检测, format=格式检测, threshold=阈值检测, enum=枚举检测, custom_sql=自定义SQL等'
    )
    severity = db.Column(
        db.String(20),
        nullable=True,
        comment='规则严重级别: critical=严重需立即处理, warning=警告建议处理, info=参考信息'
    )
    table_name = db.Column(
        db.String(255),
        nullable=True,
        comment='规则应用的目标表名'
    )
    column_name = db.Column(
        db.String(255),
        nullable=True,
        comment='规则应用的目标列名（单列/单条件场景时填充，多条件场景为空）'
    )
    # 执行模式：scoped_single / scoped_multi_cond / unscoped
    rule_mode = db.Column(
        db.String(30),
        nullable=True,
        comment='执行模式: scoped_single=单列规则(有target_table+target_column), scoped_multi_cond=多条件规则(有target_table+conditions_config), unscoped=全局规则(无target_table)'
    )
    total_count = db.Column(
        db.BigInteger,
        nullable=True,
        comment='检测的总记录数'
    )
    passed_count = db.Column(
        db.BigInteger,
        nullable=True,
        comment='通过/符合规则的记录数'
    )
    failed_count = db.Column(
        db.BigInteger,
        nullable=True,
        comment='失败/不符合规则的记录数'
    )
    failed_rate = db.Column(
        db.Numeric(5, 2),
        nullable=True,
        comment='失败率，范围0-100，表示不符合规则的记录占比'
    )
    failed_samples = db.Column(JSONB,
        nullable=True,
        comment='失败样本数据(JSON数组)，包含具体不符合规则的记录示例'
    )
    # 本次执行的检测 SQL 文本
    executed_sql_text = db.Column(
        db.Text,
        nullable=True,
        comment='本次执行的检测SQL文本，便于审计和排查问题'
    )
    # SQL 执行耗时（毫秒）
    execution_time_ms = db.Column(
        db.Integer,
        nullable=True,
        comment='SQL执行耗时（毫秒），用于性能分析和慢查询定位'
    )
    # SQL 原始执行结果：以键值对形式存储查询返回的完整列名和值
    raw_result = db.Column(
        JSONB,
        nullable=True,
        comment='SQL查询原始返回结果(JSON)，以键值对形式存储查询返回的完整列名和值，便于审计和排查'
    )
    status = db.Column(
        db.String(20),
        nullable=True,
        comment='执行状态: passed=通过(失败率<5%), failed=失败, error=执行出错'
    )
    # 规则执行出错时的错误信息
    error_message = db.Column(
        db.Text,
        nullable=True,
        comment='规则执行出错时的错误信息'
    )
    # 执行结果来源: rule_library=规则库质检, basic_audit=基础空值检测
    execution_source = db.Column(
        db.String(30),
        nullable=True,
        comment='执行结果来源: rule_library=规则库质检, basic_audit=基础空值检测'
    )
    created_at = db.Column(
        db.TIMESTAMP(),
        server_default=func.current_timestamp(),
        nullable=False,
        comment='执行结果记录创建时间'
    )

    def to_dict(self):
        return {
            'id': str(self.id),
            'report_id': str(self.report_id) if self.report_id else None,
            'library_id': str(self.library_id) if self.library_id else None,
            'rule_id': str(self.rule_id) if self.rule_id else None,
            'rule_name': self.rule_name,
            'rule_type': self.rule_type,
            'severity': self.severity,
            'table_name': self.table_name,
            'column_name': self.column_name,
            'rule_mode': self.rule_mode,
            'total_count': self.total_count,
            'passed_count': self.passed_count,
            'failed_count': self.failed_count,
            'failed_rate': float(self.failed_rate) if self.failed_rate else None,
            'failed_samples': self.failed_samples,
            'executed_sql_text': self.executed_sql_text,
            'execution_time_ms': self.execution_time_ms,
            'raw_result': self.raw_result,
            'status': self.status,
            'error_message': self.error_message,
            'execution_source': self.execution_source,
            'created_at': format_datetime(self.created_at)
        }
