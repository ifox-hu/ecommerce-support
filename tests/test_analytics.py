import os
import tempfile
import unittest
from pathlib import Path

from app.services.analytics import AnalyticsService


FIXTURE_QUERIES = {
    "上周华东渠道各品类退款率是多少？": [["家居", 0.0], ["数码", 0.5], ["鞋服", 0.5]],
    "上周订单量是多少？": [[10]],
    "上周各渠道销售额": [["华东官方商城", 1430], ["华东微信小程序", 820], ["华南官方商城", 340]],
    "上周品类销售额排行": [["数码", 1500], ["鞋服", 660], ["家居", 430]],
}


class ScriptedModel:
    def __init__(self, *statements, terms=None):
        self.statements = iter(statements)
        self.terms = terms or []

    def with_structured_output(self, schema):
        model = self

        class Runnable:
            async def ainvoke(self, messages):
                if "terms" in schema.model_fields:
                    return schema(terms=model.terms)
                return schema(sql=next(model.statements))

        return Runnable()


class AnalyticsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.snapshot = Path(directory.name) / "analytics.sqlite"

    async def test_fixture_metrics_and_query_evidence(self):
        service = AnalyticsService(self.snapshot)
        for question, rows in FIXTURE_QUERIES.items():
            with self.subTest(question=question):
                result = await service.ask(question)
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["rows"], rows)
                self.assertTrue(result["metric"])
                self.assertIn("2026-11-16", result["time_range"])
                self.assertIn("2026-11-22", result["time_range"])
                self.assertIn("LIMIT 100", result["sql"])
                steps = {event["name"]: event for event in result["steps"]}
                self.assertTrue(steps["jieba_tfidf"]["data"]["terms"])
                self.assertTrue(all(steps["retrieve"]["data"].values()))
                self.assertEqual(steps["explain"]["status"], "ok")
                self.assertTrue(steps["explain"]["data"]["permissions"]["read_only"])
                self.assertGreater(len(steps["explain"]["data"]["plan"]), 0)

    async def test_missing_metric_requests_clarification_without_sql(self):
        result = await AnalyticsService(self.snapshot).ask("上周经营得怎么样？")
        self.assertEqual(result["status"], "clarify")
        self.assertEqual(result["sql"], "")
        self.assertEqual(result["rows"], [])

    async def test_structured_expansion_can_resolve_synonyms(self):
        model = ScriptedModel("SELECT COUNT(*) AS 订单量 FROM orders", terms=["订单量"])
        result = await AnalyticsService(self.snapshot, model=model).ask("全量单子的个数？")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["rows"], [[12]])
        expansion = next(step for step in result["steps"] if step["name"] == "llm_expand")
        self.assertEqual(expansion["data"]["terms"], ["订单量"])

    async def test_unknown_column_is_repaired(self):
        model = ScriptedModel(
            "SELECT COUNT(order_total) FROM orders",
            "SELECT COUNT(*) AS 订单量 FROM orders",
        )
        result = await AnalyticsService(self.snapshot, model=model).ask("全部订单量")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["repairs"], 1)
        self.assertEqual(result["rows"], [[12]])
        checks = [step["status"] for step in result["steps"] if step["name"] == "validate_sql"]
        self.assertEqual(checks, ["repair", "ok"])

    async def test_query_returns_at_most_one_hundred_rows(self):
        model = ScriptedModel("SELECT a.order_id FROM orders a CROSS JOIN orders b LIMIT 1000")
        result = await AnalyticsService(self.snapshot, model=model).ask("全部订单量")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["rows"]), 100)
        self.assertTrue(result["sql"].endswith("LIMIT 100"))

    async def test_two_failed_repairs_leave_no_query_result(self):
        model = ScriptedModel(*["SELECT COUNT(unknown_field) FROM orders"] * 3)
        result = await AnalyticsService(self.snapshot, model=model).ask("全部订单量")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["repairs"], 2)
        self.assertEqual(result["rows"], [])
        self.assertNotIn("execute", [step["name"] for step in result["steps"]])

    async def test_unterminated_sql_literal_is_repaired(self):
        model = ScriptedModel(
            "SELECT COUNT(*) FROM orders WHERE status = 'completed",
            "SELECT COUNT(*) AS 订单量 FROM orders",
        )
        result = await AnalyticsService(self.snapshot, model=model).ask("全部订单量")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["repairs"], 1)
        self.assertEqual(result["rows"], [[12]])

    async def test_table_evidence_preserves_aliases_reused_in_nested_scopes(self):
        model = ScriptedModel("SELECT COUNT(*) FROM orders item WHERE EXISTS (SELECT 1 FROM payments item)")
        result = await AnalyticsService(self.snapshot, model=model).ask("全部订单量")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["rows"], [[12]])
        validation = next(step for step in result["steps"] if step["name"] == "validate_sql")
        self.assertEqual(validation["data"]["tables"], ["orders", "payments"])

    async def test_disallowed_sql_never_reaches_explain(self):
        candidates = [
            "DELETE FROM orders",
            "SELECT * FROM sqlite_master",
            "SELECT COUNT(*) FROM orders; SELECT COUNT(*) FROM payments",
            "SELECT COUNT(*) FROM main.orders",
            "SELECT * FROM (SELECT * FROM customers) AS x",
            "SELECT COUNT(*) FROM orders FOR UPDATE",
            "SELECT unknown_function(order_id) FROM orders",
        ]
        for sql in candidates:
            with self.subTest(sql=sql):
                result = await AnalyticsService(self.snapshot, model=ScriptedModel(sql)).ask("全部订单量")
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["rows"], [])
                self.assertEqual(result["repairs"], 0)
                self.assertNotIn("explain", [step["name"] for step in result["steps"]])
        intact = await AnalyticsService(self.snapshot).ask("全部订单量")
        self.assertEqual(intact["rows"], [[12]])

    async def test_expensive_plan_is_blocked_before_execution(self):
        sql = "SELECT COUNT(*) FROM orders a CROSS JOIN orders b CROSS JOIN orders c CROSS JOIN orders d"
        result = await AnalyticsService(self.snapshot, model=ScriptedModel(sql)).ask("全部订单量")
        self.assertEqual(result["status"], "blocked")
        steps = {step["name"]: step for step in result["steps"]}
        self.assertEqual(steps["explain"]["status"], "blocked")
        self.assertGreater(steps["explain"]["data"]["estimated_work"], steps["explain"]["data"]["budget"])
        self.assertNotIn("execute", steps)

    @unittest.skipUnless(os.getenv("TEST_ANALYTICS_MYSQL_URL"), "需要已初始化的分析 MySQL 只读账户")
    async def test_mysql_fixture_readonly_grants_and_explain(self):
        from datetime import date

        service = AnalyticsService(self.snapshot, mysql_url=os.environ["TEST_ANALYTICS_MYSQL_URL"], as_of=date(2026, 11, 24))
        for question, rows in FIXTURE_QUERIES.items():
            with self.subTest(question=question):
                result = await service.ask(question)
                self.assertEqual(result["status"], "ok", result)
                self.assertEqual(result["rows"], rows)
                explain = next(step for step in result["steps"] if step["name"] == "explain")
                self.assertIn("query_block", explain["data"]["plan"])
                self.assertTrue(explain["data"]["permissions"]["read_only"])
                self.assertEqual(set(explain["data"]["permissions"]["tables"]), {"orders", "payments", "refunds", "channels", "products"})

        sql = "SELECT COUNT(*) FROM orders a CROSS JOIN orders b CROSS JOIN orders c CROSS JOIN orders d"
        expensive = AnalyticsService(self.snapshot, mysql_url=os.environ["TEST_ANALYTICS_MYSQL_URL"], model=ScriptedModel(sql))
        result = await expensive.ask("全部订单量")
        self.assertEqual(result["status"], "blocked", result)
        explain = next(step for step in result["steps"] if step["name"] == "explain")
        self.assertGreater(explain["data"]["estimated_work"], explain["data"]["budget"])
        self.assertNotIn("execute", [step["name"] for step in result["steps"]])

        model = ScriptedModel(
            "SELECT o.order_id, COUNT(*) AS 订单量 FROM orders o",
            "SELECT COUNT(*) AS 订单量 FROM orders",
        )
        repaired = AnalyticsService(self.snapshot, mysql_url=os.environ["TEST_ANALYTICS_MYSQL_URL"], model=model)
        result = await repaired.ask("全部订单量")
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["repairs"], 1)
        self.assertEqual(result["rows"], [[12]])
        self.assertEqual([step["status"] for step in result["steps"] if step["name"] == "explain"], ["repair", "ok"])


if __name__ == "__main__":
    unittest.main()
