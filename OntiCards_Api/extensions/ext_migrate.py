import flask_migrate

from core.metadata_scope import include_object


def init(app, db):
    # include_object：让 autogenerate 只比较 init.sql 里声明过的表。
    # 详见 core/metadata_scope.py（不改 migrations/env.py，靠 configure_args 透传生效）
    flask_migrate.Migrate(app, db, include_object=include_object)
