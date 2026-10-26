"""Focused endpoint regressions for the API-key aggregate query controller.

The production module imports database drivers and Flask extensions that are not
available in the lightweight test environment.  These tests execute the real
``post`` and ``_do_post_core`` method bodies while replacing their external
collaborators with deterministic fakes.
"""

import ast
import contextlib
import io
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

CONTROLLER_PATH = (
    Path(__file__).resolve().parents[1]
    / "controllers"
    / "query"
    / "query_by_datacards_agg_plugin.py"
)


class _Query:
    def __init__(self, value):
        self.value = value

    def filter_by(self, **_filters):
        return self

    def first(self):
        return self.value


def _load_resource_class(namespace):
    """Compile the controller's real resource methods without module imports."""
    tree = ast.parse(CONTROLLER_PATH.read_text(encoding="utf-8"))
    resource = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "QueryByDataCardsAggPlugin"
    )
    resource.bases = []
    resource.keywords = []
    resource.body = [
        node
        for node in resource.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"post", "_do_post_core"}
    ]
    isolated_tree = ast.fix_missing_locations(
        ast.Module(body=[resource], type_ignores=[])
    )
    exec(  # noqa: S102 - execute only repository code in a controlled namespace
        compile(isolated_tree, CONTROLLER_PATH, "exec"), namespace
    )
    return namespace["QueryByDataCardsAggPlugin"]


class QueryByDataCardsAggPluginTest(unittest.TestCase):
    def _run_successful_request(self, *, use_trino):
        original_question = "How many orders were placed?"
        rewritten_question = "Count all rows in the orders table"
        connect_name = "trino-orders" if use_trino else "orders-db"
        log_calls = []
        executed_questions = []

        schema_row = SimpleNamespace(
            table_name="orders",
            database_name="analytics",
        )
        datasource = SimpleNamespace(
            id="datasource-1",
            connect_name=connect_name,
            database_name="analytics",
            schema_name="public",
        )
        user = SimpleNamespace(weaviate_class_name="UserCards")
        table = {
            "connect_name": connect_name,
            "connect_info": "postgresql://example",
            "db_type": "postgresql",
            "table_name": "orders",
        }
        cluster_result = {
            "rows": [{"count": 3}],
            "warnings": [],
            "target_sql": "SELECT COUNT(*) FROM orders",
            "datasource_ids": [datasource.id],
            "datasource_names": [datasource.database_name],
            "table_names": [schema_row.table_name],
            "cluster_tables": [table],
            "_llm_usage": {},
            "_sql_execution_ms": 1,
        }

        def execute_cluster(**kwargs):
            executed_questions.append(kwargs["user_question"])
            return cluster_result

        def execute_trino(**kwargs):
            executed_questions.append(kwargs["user_question"])
            return {
                "data": [{"count": 3}],
                "target_sql": "SELECT COUNT(*) FROM orders",
            }

        def format_response(payload, status, message):
            return {"payload": payload, "status": status, "message": message}

        namespace = {
            "time_module": time,
            "request": SimpleNamespace(
                get_json=lambda: {
                    "query": original_question,
                    "enable_term_rewrite": True,
                    "enable_rerank": False,
                }
            ),
            "_require_api_key_user_id": lambda: ("user-1", None, "key-1"),
            "_infer_strategy": lambda _question: "OR",
            "process_question": lambda _question: (
                rewritten_question,
                [{"term": "orders"}],
                True,
            ),
            "User": SimpleNamespace(query=_Query(user)),
            "get_data_card_json": lambda *_args, **_kwargs: {
                "doc_ids": ["doc-1"],
                "data_card_results": [
                    {"DocInfo": {"connect_name": connect_name}}
                ],
                "usage": {},
            },
            "UserDatasourceSchema": SimpleNamespace(query=_Query(schema_row)),
            "DatasourceInfo": SimpleNamespace(query=_Query(datasource)),
            "map_connect_name_to_connect_info": (
                lambda *_args, **_kwargs: "postgresql://example"
            ),
            "card_to_table_obj": lambda *_args, **_kwargs: table,
            "infer_entity_key_from_cards": lambda _tables: "id",
            "fetch_relationship_cards": lambda **_kwargs: {
                "cards": {},
                "join_suggestions": [],
            },
            "_exec_trino_unified": execute_trino,
            "build_clusters": lambda _tables: {
                ("postgresql", "postgresql://example"): [table]
            },
            "_exec_cluster": execute_cluster,
            "_filter_clusters_by_question": lambda results, _question: (
                results,
                [],
            ),
            "merge_relationship_data": lambda *_data: {
                "cards": {},
                "join_suggestions": [],
            },
            "_should_use_llm_fusion": lambda _results: False,
            "_make_json_serializable": lambda value: value,
            "_log_query_plugin": lambda **kwargs: log_calls.append(kwargs),
            "format_response": format_response,
            "db": SimpleNamespace(
                session=SimpleNamespace(rollback=lambda: None)
            ),
        }
        resource_class = _load_resource_class(namespace)

        with contextlib.redirect_stdout(io.StringIO()):
            response = resource_class().post()

        return response, log_calls, executed_questions, original_question, rewritten_question

    def test_single_cluster_success_returns_result_and_logs_original_question(self):
        response, logs, executed, original, rewritten = self._run_successful_request(
            use_trino=False
        )

        self.assertEqual(200, response["status"])
        self.assertEqual([{"count": 3}], response["payload"]["final_rows"])
        self.assertEqual([rewritten], executed)
        self.assertEqual(original, logs[-1]["question"])
        self.assertEqual(rewritten, logs[-1]["processed_question"])
        self.assertTrue(logs[-1]["success"])

    def test_trino_success_returns_result_and_logs_original_question(self):
        response, logs, executed, original, rewritten = self._run_successful_request(
            use_trino=True
        )

        self.assertEqual(200, response["status"])
        self.assertEqual([{"count": 3}], response["payload"]["final_rows"])
        self.assertEqual([rewritten], executed)
        self.assertEqual(original, logs[-1]["question"])
        self.assertEqual(rewritten, logs[-1]["processed_question"])
        self.assertTrue(logs[-1]["success"])


if __name__ == "__main__":
    unittest.main()
