"""
@File: governance_report_file.py
@Description: 报告文件关联模型 - 追踪同一报告的所有导出文件
@Author: 韩小豪 849631113@qq.com
@Create: 2026-07-20
"""

from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import func, text
from extensions.ext_database import db
from models.utils import format_datetime


class GovernanceReportFile(db.Model):
    """报告文件关联模型 - 解决一份报告多次导出产生多个文件的问题

    问题背景：
    - 一份治理报告可以导出多种格式（md、docx、pdf、xlsx）
    - 用户可能多次导出同一份报告，每次产生新文件
    - governance_reports.exported_file_path 只记录最新一次的文件
    - 删除报告时需要精准删除该报告产生的所有文件

    解决方案：
    - governance_report_files 记录每次导出的文件信息
    - 删除报告时，通过 report_id 查询所有关联文件并删除
    - 通过 CASCADE 确保数据库记录自动清理
    """
    __tablename__ = 'governance_report_files'

    __table_args__ = (

        db.Index('idx_report_files_report_id', 'report_id'),

        db.Index('idx_report_files_user_id', 'user_id'),

        db.Index('idx_report_files_file_type', 'file_type'),

        {'comment': '报告文件关联表 - 追踪同一报告的所有导出文件，解决一份报告多次导出产生多个文件的问题'},

    )
    # 文件类型常量
    FILE_TYPE_MD = 'md'
    FILE_TYPE_DOCX = 'docx'
    FILE_TYPE_PDF = 'pdf'
    FILE_TYPE_XLSX = 'xlsx'

    FILE_TYPES = [FILE_TYPE_MD, FILE_TYPE_DOCX, FILE_TYPE_PDF, FILE_TYPE_XLSX]

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text('uuid_generate_v4()'),
        comment='文件记录唯一标识符'
    )
    report_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey('governance_reports.id', ondelete='CASCADE'),
        nullable=False,
        comment='所属报告ID，删除报告时通过CASCADE级联删除文件记录'
    )
    user_id = db.Column(
        UUID(as_uuid=True),
        nullable=False,
        comment='所属用户ID，冗余存储便于文件清理和权限校验'
    )
    report_name = db.Column(
        db.String(255),
        nullable=True,
        comment='报告名称（冗余存储，便于文件管理和展示）'
    )
    file_path = db.Column(
        db.String(512),
        nullable=False,
        comment='文件完整路径（统一使用正斜杠分隔符）'
    )
    file_name = db.Column(
        db.String(255),
        nullable=False,
        comment='文件名（不含路径），用于显示'
    )
    file_type = db.Column(
        db.String(20),
        nullable=False,
        comment='文件类型: md=Markdown, docx=Word, pdf=PDF, xlsx=Excel'
    )
    file_size = db.Column(
        db.BigInteger,
        nullable=True,
        comment='文件大小，单位字节'
    )
    created_at = db.Column(
        db.TIMESTAMP(),
        server_default=func.current_timestamp(),
        nullable=False,
        comment='文件记录创建时间'
    )

    def to_dict(self):
        """转换为字典"""
        return {
            'id': str(self.id),
            'report_id': str(self.report_id),
            'user_id': str(self.user_id),
            'report_name': self.report_name,
            'file_path': self.file_path,
            'file_name': self.file_name,
            'file_type': self.file_type,
            'file_size': self.file_size,
            'created_at': format_datetime(self.created_at),
        }
