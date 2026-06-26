from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from datetime import date
from operator import add
from pathlib import Path
from time import monotonic
from typing import Annotated, Any, Literal

from jieba.analyse import extract_tags
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from app.core.observability import Observability
from .catalog import CATALOG, METRICS, SYNONYM_GROUPS, Document, Period, date_period, demo_sql
from .database import ApprovedQuery, MAX_ROWS, QueryProblem, init_snapshot, query_mysql, query_sqlite, validate_sql


MAX_REPAIRS = 2
MAX_QUESTION_LENGTH = 1500
DEMO_AS_OF = date(2026, 11, 24)


class AnalyticsExpansion(BaseModel):
    terms: list[str] = Field(max_length=16, description="与原始问题相关的同义词、指标或维度术语。")


class AnalyticsSQL(BaseModel):
    sql: str = Field(default="", description="单条 SELECT SQL，无 Markdown。")
    clarification: str = Field(default="", description="口径确有歧义时，给出一句需要用户回答的问题。")


class AnalyticsState(TypedDict, total=False):
    question: str
    keywords: list[tuple[str, float]]
    expansion: list[str]
    matches: dict[str, list[tuple[Document, float]]]
    context: dict[str, list[str]]
    metric_id: str
    period: Period
    candidate: str
    approved: ApprovedQuery | None
    repairs: int
    feedback: str
    status: str
    answer: str
    columns: list[str]
    rows: list[list[Any]]
    steps: Annotated[list[dict[str, Any]], add]


def _step(name: str, status: str, detail: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"name": name, "status": status, "detail": detail, "data": data or {}}


def _local_expansion(question: str) -> list[str]:
    return list(dict.fromkeys(
        synonym for group in SYNONYM_GROUPS if any(keyword in question for keyword in group) for synonym in group
    ))[:16]


def _keyword_match(term: str, keywords: tuple[str, ...]) -> bool:
    return any(term in word or word in term for word in keywords)


def _resolve_metric(question: str, expansion: list[str]) -> str:
    implicit_sales_aliases = {"分类排行", "品类排行", "分类排名", "品类排名", "品类榜"}
    explicit = [
        key for key, metric in METRICS.items()
        if any(alias in question and alias not in implicit_sales_aliases for alias in metric.aliases)
    ]
    if explicit:
        return explicit[0] if len(explicit) == 1 else ""
    if any(alias in question for alias in implicit_sales_aliases):
        return "sales"
    matching = [key for key, metric in METRICS.items() if set(metric.aliases).intersection(expansion)]
    return matching[0] if len(matching) == 1 else ""


class AnalyticsService:
    def __init__(
        self, demo_db: Path, mysql_url: str = "", model: Any = None, *,
        as_of: date | None = None, observability: Observability | None = None,
    ) -> None:
        self.demo_db = Path(demo_db)
        self.mysql_url = mysql_url
        self.model = model
        self.as_of = as_of
        self.observability = observability
        self.dialect = "mysql" if mysql_url else "sqlite"
        if not mysql_url:
            init_snapshot(self.demo_db)
        self.graph = self._compile_graph()

    def _compile_graph(self):
        builder = StateGraph(AnalyticsState)
        nodes = {
            "jieba_tfidf": self._extract,
            "llm_expand": self._expand,
            "retrieve": self._retrieve,
            "rank": self._rank,
            "generate_sql": self._generate,
            "validate_sql": self._validate,
            "explain_execute": self._explain_execute,
            "explain_result": self._result,
        }
        for name, node in nodes.items():
            builder.add_node(name, self._observed(name, node))
        builder.add_edge(START, "jieba_tfidf")
        builder.add_edge("jieba_tfidf", "llm_expand")
        builder.add_edge("llm_expand", "retrieve")
        builder.add_edge("retrieve", "rank")
        builder.add_conditional_edges("rank", self._after_rank)
        builder.add_conditional_edges("generate_sql", self._after_generate)
        builder.add_conditional_edges("validate_sql", self._after_validate)
        builder.add_conditional_edges("explain_execute", self._after_execute)
        builder.add_edge("explain_result", END)
        return builder.compile()

    def _observed(self, name: str, function: Callable[[AnalyticsState], Awaitable[AnalyticsState]]):
        async def run(state: AnalyticsState) -> AnalyticsState:
            observer = self.observability
            if observer is None:
                return await function(state)
            started = monotonic()
            with observer.span(f"analytics.{name}", node=name):
                update = await function(state)
                steps = update.get("steps", [])
                outcome = steps[-1]["status"] if steps else "ok"
                observer.observe("analytics_node", name, outcome, monotonic() - started)
                return update

        return run

    async def ask(self, question: str) -> dict[str, Any]:
        question = question.strip()
        if not question or len(question) > MAX_QUESTION_LENGTH:
            return {
                "status": "clarify", "answer": f"请输入 1 至 {MAX_QUESTION_LENGTH} 个字符的经营指标问题。",
                "sql": "", "columns": [], "rows": [], "steps": [], "metric": "", "metric_id": "",
                "time_range": "", "dialect": self.dialect, "repairs": 0,
            }
        observer = self.observability
        started = monotonic()
        with observer.span("analytics.ask", mode=self.dialect) if observer else nullcontext():
            state = await self.graph.ainvoke({"question": question, "repairs": 0, "steps": []}, {"recursion_limit": 24})
        if observer:
            observer.observe("analytics", "ask", state["status"], monotonic() - started)
        metric_id = state.get("metric_id", "")
        period = state.get("period")
        approved = state.get("approved")
        return {
            "status": state["status"], "answer": state["answer"], "sql": approved.sql if approved else "",
            "columns": state.get("columns", []), "rows": state.get("rows", []), "steps": state["steps"],
            "metric": METRICS[metric_id].definition if metric_id else "", "metric_id": metric_id,
            "time_range": period.label if period else "", "dialect": self.dialect, "repairs": state["repairs"],
        }

    async def _extract(self, state: AnalyticsState) -> AnalyticsState:
        keywords = await asyncio.to_thread(extract_tags, state["question"], topK=10, withWeight=True)
        return {
            "keywords": keywords,
            "steps": [_step("jieba_tfidf", "ok", "已按 TF-IDF 提取关键词。", {"terms": keywords})],
        }

    async def _structured(self, schema: type[BaseModel], instructions: str, payload: dict[str, Any]):
        observer = self.observability
        messages = [("system", instructions), ("human", json.dumps(payload, ensure_ascii=False))]
        runnable = self.model.with_structured_output(schema)
        if observer is None:
            return await asyncio.wait_for(runnable.ainvoke(messages), timeout=15)
        with observer.langchain_span() as config:
            call = runnable.ainvoke(messages, config=config) if config else runnable.ainvoke(messages)
            return await asyncio.wait_for(call, timeout=15)

    async def _expand(self, state: AnalyticsState) -> AnalyticsState:
        question = state["question"]
        terms = _local_expansion(question.casefold())
        status = "demo"
        detail = "演示词典已扩展同义词和领域术语。"
        if self.model is not None:
            try:
                response = await self._structured(
                    AnalyticsExpansion,
                    "你是电商经营分析的检索词扩展器。保留提问的原意，扩展同义词和领域术语。"
                    "仅在提问明确表达了某个指标时提供它的指标词。可用指标为订单量、销售额和退款率。",
                    {"question": question, "keywords": [term for term, _ in state["keywords"]]},
                )
                terms = list(dict.fromkeys(term.strip().casefold() for term in response.terms if 1 < len(term.strip()) <= 32))
                status, detail = "ok", "模型已返回结构化同义词和领域术语。"
            except Exception:
                status, detail = "degraded", "模型扩词暂不可用，已使用演示词典扩展。"
        return {"expansion": terms, "steps": [_step("llm_expand", status, detail, {"terms": terms})]}

    async def _retrieve(self, state: AnalyticsState) -> AnalyticsState:
        question = state["question"].casefold()
        matches = {}
        for kind, documents in CATALOG.items():
            matches[kind] = []
            for document in documents:
                direct = max((len(word) for word in document.terms if word in question), default=0)
                tfidf_score = sum(weight for term, weight in state["keywords"] if _keyword_match(term.casefold(), document.terms))
                expansion_score = sum(1 for term in state["expansion"] if _keyword_match(term, document.terms))
                score = direct * 3 + tfidf_score + expansion_score
                if score:
                    matches[kind].append((document, score))
        evidence = {kind: [document.key for document, _ in results] for kind, results in matches.items()}
        return {"matches": matches, "steps": [_step("retrieve", "ok", "已分别召回指标词典、表字段元数据、业务口径和示例 SQL。", evidence)]}

    async def _rank(self, state: AnalyticsState) -> AnalyticsState:
        question = state["question"].casefold()
        metric_id = _resolve_metric(question, state["expansion"])
        if not metric_id:
            return {
                "status": "clarify", "answer": "请明确一个指标，例如订单量、销售额或退款率，必要时可加上下单日期、区域或品类。",
                "steps": [_step("rank", "clarify", "没有确定唯一的指标口径。")],
            }

        today = self.as_of or DEMO_AS_OF
        try:
            period = date_period(question, today)
        except (ValueError, OverflowError):
            return {
                "metric_id": metric_id, "status": "clarify", "answer": "请补充有效的下单日期或连续的日期区间，使用 YYYY-MM-DD 格式。",
                "steps": [_step("rank", "clarify", "无法确定下单日期范围。")],
            }

        metric = METRICS[metric_id]
        sources = {document.key: score for document, score in state["matches"]["表字段元数据"]}
        ranked_tables = sorted(CATALOG["表字段元数据"], key=lambda doc: (doc.key not in metric.tables, -sources.get(doc.key, 0)))
        context = {
            "指标词典": [metric.name],
            "表字段元数据": [document.body for document in ranked_tables if document.key in metric.tables or document.key in sources],
            "业务口径": [metric.definition],
            "示例 SQL": [metric.example],
        }
        return {
            "metric_id": metric_id, "period": period, "context": context,
            "steps": [_step("rank", "ok", f"已筛选{metric.name}口径、日期范围和可用字段。", {
                "metric": metric.name, "time_range": period.label, "selected": context,
            })],
        }

    async def _generate(self, state: AnalyticsState) -> AnalyticsState:
        repairs = state["repairs"] + int(bool(state.get("feedback")))
        candidate = ""
        if self.model is None:
            candidate = demo_sql(state["metric_id"], state["question"].casefold(), state["period"])
        else:
            try:
                response = await self._structured(
                    AnalyticsSQL,
                    "你负责生成电商分析 SQL。按指定方言输出单条只读 SELECT，只用提供的表、字段和已选指标。"
                    "使用指定统计日期范围。结束日期是排除边界。保留用户指定的区域、渠道、品类及分组，不编造字段或过滤条件。"
                    f"最多返回 {MAX_ROWS} 行。校验报错时参考上一版 SQL 和具体反馈进行修正。"
                    "示例 SQL 只说明结构，日期、分组和过滤条件以此次问题为准。口径确有歧义时填写 clarification。",
                    {
                        "question": state["question"], "dialect": self.dialect, "context": state["context"],
                        "start": state["period"].start.isoformat() if state["period"].start else None,
                        "stop_exclusive": state["period"].stop.isoformat() if state["period"].stop else None,
                        "previous_sql": state.get("candidate") if repairs else None, "feedback": state.get("feedback"),
                    },
                )
                if response.clarification and not response.sql.strip():
                    return {
                        "approved": None, "status": "clarify", "answer": response.clarification[:200], "repairs": repairs,
                        "steps": [_step("generate_sql", "clarify", "模型请求补充业务条件。")],
                    }
                candidate = response.sql
            except Exception:
                return {
                    "status": "error", "approved": None, "repairs": repairs, "answer": "暂时无法生成经营查询，请稍后重试。",
                    "steps": [_step("generate_sql", "error", "SQL 生成模型暂不可用或未返回结构化结果。")],
                }
        detail = f"已根据校验反馈进行第 {repairs} 次 SQL 修正。" if repairs else "已按选定指标口径和查询条件生成 SQL。"
        return {
            "candidate": candidate, "approved": None, "feedback": "", "repairs": repairs,
            "steps": [_step("generate_sql", "ok", detail, {"repair": repairs, "source": "llm" if self.model else "demo"})],
        }

    @staticmethod
    def _failure(state: AnalyticsState, problem: QueryProblem) -> AnalyticsState:
        if problem.repairable and state["repairs"] < MAX_REPAIRS:
            return {"feedback": str(problem), "steps": [_step(problem.phase, "repair", str(problem), problem.evidence)]}
        status = "blocked" if problem.blocked else "error"
        detail = f"已达到 {MAX_REPAIRS} 次修正上限。{problem}" if problem.repairable else str(problem)
        return {"status": status, "answer": detail, "feedback": "", "steps": [_step(problem.phase, status, detail, problem.evidence)]}

    async def _validate(self, state: AnalyticsState) -> AnalyticsState:
        try:
            approved = validate_sql(state["candidate"], self.dialect)
        except QueryProblem as problem:
            return self._failure(state, problem)
        return {
            "approved": approved, "steps": [_step("validate_sql", "ok", "单条只读语句、表字段白名单和结果上限已通过校验。", {
                "tables": sorted(approved.tables), "max_rows": MAX_ROWS,
            })],
        }

    async def _explain_execute(self, state: AnalyticsState) -> AnalyticsState:
        approved = state["approved"]
        assert approved is not None
        observer = self.observability
        span = observer.span(f"analytics.{self.dialect}", operation="explain_execute") if observer else nullcontext()
        try:
            with span:
                if self.mysql_url:
                    result = await asyncio.to_thread(query_mysql, self.mysql_url, approved)
                else:
                    result = await asyncio.to_thread(query_sqlite, self.demo_db, approved)
        except QueryProblem as problem:
            failure = self._failure(state, problem)
            if problem.phase == "execute" and problem.evidence:
                failure["steps"] = [_step("explain", "ok", "查询计划与只读权限检查已通过。", problem.evidence), *failure["steps"]]
            return failure
        return {
            "columns": result.columns, "rows": result.rows,
            "steps": [
                _step("explain", "ok", "查询计划与只读权限检查已通过。", result.evidence),
                _step("execute", "ok", f"已通过只读连接执行 SQL，返回 {len(result.rows)} 行。"),
            ],
        }

    async def _result(self, state: AnalyticsState) -> AnalyticsState:
        metric = METRICS[state["metric_id"]]
        rows = state["rows"]
        answer = f"{state['period'].label}，{metric.name}查询返回 {len(rows)} 行。"
        if len(rows) == 1 and len(rows[0]) == 1:
            value = rows[0][0]
            if value is None:
                answer = f"{state['period'].label}，当前筛选范围无可计算{metric.name}的数据。"
            elif isinstance(value, (int, float)) and state["metric_id"] == "refund_rate":
                answer = f"{state['period'].label}，退款率为 {value:.2%}。"
            elif isinstance(value, (int, float)):
                unit = " 元" if state["metric_id"] == "sales" else " 笔"
                answer = f"{state['period'].label}，{metric.name}为 {value:g}{unit}。"
        if not rows:
            answer = f"{state['period'].label}，当前筛选范围无符合条件的记录。"
        return {
            "status": "ok", "answer": f"{answer} {metric.definition}",
            "steps": [_step("explain_result", "ok", "已按实际查询结果附上指标口径与日期范围。")],
        }

    @staticmethod
    def _after_rank(state: AnalyticsState) -> Literal["generate_sql", "__end__"]:
        return END if state.get("status") else "generate_sql"

    @staticmethod
    def _after_generate(state: AnalyticsState) -> Literal["validate_sql", "__end__"]:
        return END if state.get("status") else "validate_sql"

    @staticmethod
    def _after_validate(state: AnalyticsState) -> Literal["generate_sql", "explain_execute", "__end__"]:
        if state.get("status"):
            return END
        return "generate_sql" if state.get("feedback") else "explain_execute"

    @staticmethod
    def _after_execute(state: AnalyticsState) -> Literal["generate_sql", "explain_result", "__end__"]:
        if state.get("status"):
            return END
        return "generate_sql" if state.get("feedback") else "explain_result"
