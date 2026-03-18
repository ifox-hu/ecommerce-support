from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from math import prod
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import unquote, urlsplit

import pymysql
from sqlglot import exp, parse
from sqlglot.errors import OptimizeError, ParseError, TokenError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope

from app.services.analytics_catalog import SCHEMA


MAX_ROWS = 100
MAX_EXPLAIN_WORK = 10_000
MAX_QUERY_MS = 3_000
_SQLITE_FIXTURE = Path(__file__).resolve().parents[2] / "data" / "analytics_mysql.sql"
_SAFE_FUNCTIONS = frozenset({
    "ABS", "AND", "AVG", "CASE", "CAST", "CEIL", "COALESCE", "CONCAT", "COUNT", "CURRENT_DATE",
    "DATE", "DATE_ADD", "DATE_DIFF", "DATE_SUB", "DENSE_RANK", "EXISTS", "EXTRACT",
    "FLOOR", "GREATEST", "GROUP_CONCAT", "IF", "LEAST", "LEFT", "LENGTH", "LOWER",
    "MAX", "MIN", "NULLIF", "OR", "RANK", "REPLACE", "RIGHT", "ROUND", "ROW_NUMBER",
    "STRFTIME", "STR_TO_DATE", "STR_TO_TIME", "SUBSTRING", "SUM", "TIME_TO_STR",
    "TRIM", "TS_OR_DS_TO_DATE", "UPPER",
})
_UNSAFE_NODES = (exp.DDL, exp.DML, exp.Command, exp.Into, exp.Lock, exp.Hint, exp.Parameter, exp.Placeholder, exp.SessionParameter)
_MYSQL_REPAIR_ERRORS = frozenset({1052, 1054, 1055, 1060, 1064, 1066, 1111, 1140, 1241, 1242})


class QueryProblem(ValueError):
    def __init__(
        self, detail: str, *, phase: str = "validate_sql", repairable: bool = False,
        blocked: bool = False, evidence: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.phase = phase
        self.repairable = repairable
        self.blocked = blocked
        self.evidence = evidence


@dataclass(frozen=True)
class ApprovedQuery:
    sql: str
    table_aliases: dict[str, set[str]]

    @property
    def tables(self) -> set[str]:
        return {name for names in self.table_aliases.values() for name in names}


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    evidence: dict[str, Any]


def validate_sql(candidate: str, dialect: str) -> ApprovedQuery:
    if not candidate.strip():
        raise QueryProblem("没有生成可校验的 SQL。", repairable=True)
    if len(candidate) > 6_000 or "/*!" in candidate or "/*+" in candidate:
        raise QueryProblem("SQL 超出结构限制或包含数据库指令。", blocked=True)
    try:
        statements = parse(candidate, read=dialect)
    except ParseError as error:
        description = error.errors[0].get("description", "SQL 语法错误") if error.errors else "SQL 语法错误"
        raise QueryProblem(f"无法解析 SQL：{description}。", repairable=True) from error
    except TokenError as error:
        raise QueryProblem("无法解析 SQL 中的字符串、引号或词法记号。", repairable=True) from error
    if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise QueryProblem("只允许一条只读 SELECT 查询。", blocked=True)
    statement = statements[0]
    nodes = list(statement.walk())
    if len(nodes) > 450 or any(isinstance(node, _UNSAFE_NODES) for node in nodes):
        raise QueryProblem("SQL 包含非只读结构、会话变量或数据库指令。", blocked=True)
    for function in statement.find_all(exp.Func):
        name = function.name if isinstance(function, exp.Anonymous) else function.sql_name()
        if name.upper() not in _SAFE_FUNCTIONS:
            raise QueryProblem("SQL 调用了分析查询未授权的函数。", blocked=True)

    aliases = {}
    tables = list(statement.find_all(exp.Table))
    if len(tables) > 12:
        raise QueryProblem("查询引用的表过多，请缩小查询范围。", blocked=True)
    if any(table.catalog or table.db or not isinstance(table.this, exp.Identifier) for table in tables):
        raise QueryProblem("禁止访问带库名前缀或动态生成的数据表。", blocked=True)
    try:
        for scope in traverse_scope(statement):
            for alias, (_, source) in scope.selected_sources.items():
                if isinstance(source, exp.Table):
                    if source.name.casefold() not in SCHEMA:
                        raise QueryProblem("SQL 引用了未授权的分析表。", blocked=True)
                    aliases.setdefault(alias.casefold(), set()).add(source.name.casefold())
        if not aliases:
            raise QueryProblem("查询需要引用已授权的分析表。", blocked=True)
        qualify(statement.copy(), dialect=dialect, schema=SCHEMA, validate_qualify_columns=True)
    except OptimizeError as error:
        raise QueryProblem(f"表关联或字段校验失败：{str(error)[:180]}。", repairable=True) from error

    for offset in statement.find_all(exp.Offset):
        value = offset.expression
        if not isinstance(value, exp.Literal) or not value.is_int or not 0 <= int(value.this) <= 1_000:
            raise QueryProblem("偏移量须为 0 到 1000 的整数。", repairable=True)
    for limit in statement.find_all(exp.Limit):
        value = limit.expression
        if not isinstance(value, exp.Literal) or not value.is_int or int(value.this) < 1:
            raise QueryProblem("结果上限须为正整数。", repairable=True)
    limit = statement.args.get("limit")
    if limit is None or int(limit.expression.this) > MAX_ROWS:
        statement.limit(MAX_ROWS, copy=False)
    return ApprovedQuery(statement.sql(dialect=dialect, comments=False), aliases)


def init_snapshot(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=10)) as db, db:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("BEGIN IMMEDIATE")
        if db.execute("PRAGMA user_version").fetchone()[0]:
            return
        fixture = _SQLITE_FIXTURE.read_text(encoding="utf-8")
        fixture = fixture.split("-- BEGIN SHARED FIXTURE", 1)[1].split("-- END SHARED FIXTURE", 1)[0]
        for statement in fixture.split(";"):
            if statement.strip():
                db.execute(statement)
        db.execute("ANALYZE")
        db.execute("PRAGMA user_version = 1")


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return value


def _query_result(cursor: Any, evidence: dict[str, Any]) -> QueryResult:
    columns = [column[0] for column in cursor.description]
    rows = [[_json_value(value) for value in row] for row in cursor.fetchmany(MAX_ROWS)]
    return QueryResult(columns, rows, evidence)


def _cost_check(evidence: dict[str, Any]) -> None:
    if evidence["estimated_work"] > evidence["budget"]:
        raise QueryProblem("EXPLAIN 预计成本超过演示查询预算，请缩小日期范围或分组数量。", phase="explain", blocked=True, evidence=evidence)


def query_sqlite(path: Path, query: ApprovedQuery) -> QueryResult:
    evidence = None
    phase = "explain"
    try:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db, db:
            db.execute("PRAGMA query_only = ON")
            read_only = db.execute("PRAGMA query_only").fetchone()[0] == 1
            if not read_only:
                raise QueryProblem("无法建立 SQLite 只读查询连接。", phase=phase, blocked=True)
            deadline = monotonic() + MAX_QUERY_MS / 1_000
            db.set_progress_handler(lambda: int(monotonic() > deadline), 1_000)
            db.execute("BEGIN")
            plan = [item[3] for item in db.execute(f"EXPLAIN QUERY PLAN {query.sql}")]
            sizes = {name: int(stats.split()[0]) for name, stats in db.execute("SELECT tbl, stat FROM sqlite_stat1")}
            scans = [match[1].casefold() for step in plan if (match := re.match(r"SCAN (\S+)", step))]
            work = prod(
                max(sizes.get(table, 100) for table in query.table_aliases.get(alias, {alias}))
                for alias in scans
            )
            evidence = {
                "command": "EXPLAIN QUERY PLAN", "plan": plan, "estimated_work": work,
                "cost_signal": "SQLite 全扫描估计行数乘积", "budget": MAX_EXPLAIN_WORK,
                "permissions": {"read_only": True, "mode": "mode=ro, query_only=ON"},
            }
            _cost_check(evidence)
            phase = "execute"
            return _query_result(db.execute(query.sql), evidence)
    except sqlite3.Error as error:
        detail = str(error)
        repairable = detail.startswith(("no such column", "ambiguous column", "no such table", "near ", "misuse of aggregate"))
        raise QueryProblem(
            f"SQLite 查询失败：{detail[:180]}。" if repairable else "SQLite 分析查询不可用或超过执行时限。",
            phase=phase, repairable=repairable, evidence=evidence,
        ) from error


def _check_mysql_grants(cursor: Any, database: str) -> dict[str, Any]:
    cursor.execute("SHOW GRANTS FOR CURRENT_USER()")
    tables = set()
    for (grant,) in cursor.fetchall():
        if "WITH GRANT OPTION" in grant.upper():
            break
        if grant.startswith("GRANT USAGE ON *.* TO "):
            continue
        match = re.fullmatch(r"GRANT SELECT ON `([^`]+)`\.`([^`]+)` TO .+", grant)
        if not match or match[1] != database or match[2] not in SCHEMA:
            break
        tables.add(match[2])
    else:
        if tables:
            return {"read_only": True, "mode": "SHOW GRANTS + READ ONLY transaction", "tables": sorted(tables)}
    raise QueryProblem("MySQL 分析账户须仅拥有允许的数据表的 SELECT 权限。", phase="explain", blocked=True)


def _mysql_cost(plan: Any) -> float:
    if isinstance(plan, list):
        return max((_mysql_cost(item) for item in plan), default=0.0)
    if not isinstance(plan, dict):
        return 0.0
    signals = {"query_cost", "rows_examined_per_scan", "rows_produced_per_join"}
    return max(
        (float(value) if key in signals else _mysql_cost(value) for key, value in plan.items()),
        default=0.0,
    )


def query_mysql(url: str, query: ApprovedQuery) -> QueryResult:
    evidence = None
    phase = "explain"
    try:
        uri = urlsplit(url)
        database = unquote(uri.path.lstrip("/"))
        if uri.scheme not in {"mysql", "mysql+pymysql"} or not uri.hostname or not uri.username or not database:
            raise QueryProblem("MySQL 分析连接须配置数据库和只读账号。", phase=phase)
        with closing(pymysql.connect(
            host=uri.hostname, port=uri.port or 3306,
            user=unquote(uri.username), password=unquote(uri.password or ""),
            database=database, charset="utf8mb4", connect_timeout=4, read_timeout=5, write_timeout=5,
        )) as db, db.cursor() as cursor:
            permissions = _check_mysql_grants(cursor, database)
            if not query.tables <= set(permissions["tables"]):
                raise QueryProblem("MySQL 分析账户未获准查询全部所需数据表。", phase=phase, blocked=True)
            cursor.execute(f"SET SESSION MAX_EXECUTION_TIME = {MAX_QUERY_MS}")
            cursor.execute("START TRANSACTION READ ONLY")
            cursor.execute(f"EXPLAIN FORMAT=JSON {query.sql}")
            plan = json.loads(cursor.fetchone()[0])
            evidence = {
                "command": "EXPLAIN FORMAT=JSON", "plan": plan, "estimated_work": _mysql_cost(plan),
                "cost_signal": "MySQL 计划成本与扫描行数的最大值", "budget": MAX_EXPLAIN_WORK,
                "permissions": permissions,
            }
            _cost_check(evidence)
            phase = "execute"
            cursor.execute(query.sql)
            return _query_result(cursor, evidence)
    except pymysql.MySQLError as error:
        code = error.args[0] if error.args else None
        repairable = code in _MYSQL_REPAIR_ERRORS
        blocked = code in {1142, 1143, 1227}
        detail = str(error.args[1])[:180] if repairable and len(error.args) > 1 else "连接不可用、权限不足或超出执行时限"
        raise QueryProblem(f"MySQL 查询失败：{detail}。", phase=phase, repairable=repairable, blocked=blocked, evidence=evidence) from error
    except (RuntimeError, ValueError, TypeError) as error:
        if isinstance(error, QueryProblem):
            raise
        raise QueryProblem("无法验证 MySQL 连接或执行计划。", phase=phase) from error
