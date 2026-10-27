import importlib.util
import sys
import types
from contextlib import nullcontext
from pathlib import Path

import pytest

API_ROOT = Path(__file__).resolve().parents[1]
GENERATOR_PATH = API_ROOT / "controllers" / "datacard" / "datacard_generator.py"


class FakeColumn:
    def __init__(self, name):
        self.name = name

    def __eq__(self, value):
        return ("eq", self.name, value)

    def in_(self, values):
        return ("in", self.name, tuple(values))


class FakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def with_entities(self, *entities):
        return self

    def filter(self, *criteria):
        return self

    def all(self):
        return self.rows


class FakeSession:
    def __init__(self, datasource):
        self.datasource = datasource
        self.datasource_filters = []

    def query(self, model):
        session = self

        class DatasourceQuery:
            def filter_by(self, **filters):
                session.datasource_filters.append(filters)
                return self

            def first(self):
                return session.datasource

        return DatasourceQuery()


def install_module(monkeypatch, name, **attributes):
    module = types.ModuleType(name)
    for attribute, value in attributes.items():
        setattr(module, attribute, value)
    monkeypatch.setitem(sys.modules, name, module)
    return module


@pytest.fixture
def generator_harness(monkeypatch):
    monkeypatch.syspath_prepend(str(API_ROOT))

    persisted_cards = []
    datasource = types.SimpleNamespace(id="datasource-from-db", connect_name="Orders DB")
    session = FakeSession(datasource)
    fake_db = types.SimpleNamespace(session=session)

    llm_utils = types.SimpleNamespace(
        llm_call=lambda prompt: '{"Abstract":"Orders", "KeyConcepts":{}, "Tags":[]}'
    )
    install_module(monkeypatch, "controllers.agents.qwen", llm_utils=llm_utils)
    install_module(
        monkeypatch,
        "controllers.datacard.data_card_db_api",
        add_data_card=lambda **card: persisted_cards.append(card),
    )
    install_module(
        monkeypatch,
        "controllers.weaviate_db_tool.weaviate_api",
        add_vector=lambda card, class_name=None: "vector-id",
    )
    install_module(monkeypatch, "extensions.ext_database", db=fake_db)

    class UserDatasourceSchema:
        query = FakeQuery([("orders", "schema-doc-id")])
        table_name = FakeColumn("table_name")
        id = FakeColumn("id")
        user_id = FakeColumn("user_id")
        connect_info_hash = FakeColumn("connect_info_hash")

    class DataCardDataSource:
        query = FakeQuery([])
        doc_id = FakeColumn("doc_id")

    install_module(
        monkeypatch,
        "models.user_datasource_schema",
        UserDatasourceSchema=UserDatasourceSchema,
    )
    install_module(
        monkeypatch,
        "models.datacards_datasource",
        DataCardDataSource=DataCardDataSource,
    )
    install_module(
        monkeypatch,
        "models.prompt_config",
        prompt_manager=types.SimpleNamespace(get_prompt=lambda name: "Generate {{table_name}}"),
    )
    install_module(monkeypatch, "models.datasource_infos", DatasourceInfo=type("DatasourceInfo", (), {}))
    install_module(
        monkeypatch,
        "core.connect_info_encryptor",
        get_connect_info_hash=lambda value: f"hash:{value}",
    )

    schema_aware_types = {"postgresql", "mssql", "oracle", "kingbase", "dm"}
    install_module(
        monkeypatch,
        "controllers.datasource.database_schema_extractor",
        request_status_manager=types.SimpleNamespace(is_cancelled=lambda request_id: False),
        _add_schema_filter=lambda query, db_type, schema_name: query,
        _has_schema_dim=lambda db_type: (db_type or "").lower() in schema_aware_types,
    )
    install_module(
        monkeypatch,
        "controllers.datacard.datacard_sampling",
        sample_and_analyze=lambda **kwargs: types.SimpleNamespace(to_dict=dict),
    )
    install_module(
        monkeypatch,
        "sqlalchemy",
        create_engine=lambda connect_info, pool_pre_ping: types.SimpleNamespace(
            connect=lambda: nullcontext(object()),
            dispose=lambda: None,
        ),
    )

    module_name = "test_target_datacard_generator"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, GENERATOR_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module,
        "current_app",
        types.SimpleNamespace(
            _get_current_object=lambda: types.SimpleNamespace(app_context=nullcontext)
        ),
    )

    return types.SimpleNamespace(
        module=module,
        session=session,
        persisted_cards=persisted_cards,
    )


@pytest.mark.parametrize(
    ("database_type", "schema_name", "connect_name", "datasource_id", "expected_filters"),
    [
        (
            "postgresql",
            "analytics",
            None,
            None,
            {
                "user_id": "user-1",
                "connect_info_hash": "hash:postgresql://orders",
                "schema_name": "analytics",
            },
        ),
        (
            "postgresql",
            "analytics",
            "Explicit name",
            None,
            {
                "user_id": "user-1",
                "connect_info_hash": "hash:postgresql://orders",
                "schema_name": "analytics",
            },
        ),
        (
            "mysql",
            "orders_database",
            None,
            "explicit-id",
            {
                "user_id": "user-1",
                "connect_info_hash": "hash:postgresql://orders",
            },
        ),
        (
            "mysql",
            "orders_database",
            None,
            None,
            {
                "user_id": "user-1",
                "connect_info_hash": "hash:postgresql://orders",
            },
        ),
    ],
)
def test_missing_datasource_identifiers_are_resolved_for_generated_cards(
    generator_harness,
    database_type,
    schema_name,
    connect_name,
    datasource_id,
    expected_filters,
):
    cards = generator_harness.module.generate_datacards_for_schema(
        schema={
            "database_type": database_type,
            "schema_name": schema_name,
            "tables": [{"table_name": "orders", "columns": []}],
        },
        user_id="user-1",
        connect_info="postgresql://orders",
        connect_name=connect_name,
        datasource_id=datasource_id,
    )

    expected_name = connect_name or "Orders DB"
    expected_id = datasource_id or "datasource-from-db"

    assert generator_harness.session.datasource_filters == [expected_filters]
    assert len(cards) == 1
    assert cards[0]["DocInfo"]["connect_name"] == expected_name
    assert cards[0]["DocInfo"]["datasource_id"] == expected_id
    assert len(generator_harness.persisted_cards) == 1
    assert generator_harness.persisted_cards[0]["connect_name"] == expected_name
    assert generator_harness.persisted_cards[0]["datasource_id"] == expected_id
