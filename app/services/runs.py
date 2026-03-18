import asyncio
import hashlib
import json
import logging
import re
import sqlite3
from contextlib import closing
from time import perf_counter

from app.agent.workflow import RetryableToolFailure, fault_injection
from app.services.business import utc_timestamp
from app.services.memory import safe_key


logger = logging.getLogger(__name__)


class RequestConflict(ValueError):
    pass


def redact_feedback(text: str) -> str:
    redacted = re.sub(r"(?i)(?<![A-Z0-9])(?:ORD|REF)-\d+(?![A-Z0-9])", "[业务编号]", text)
    redacted = re.sub(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", "[邮箱]", redacted)
    return re.sub(r"(?<!\d)(?:\+?86[ -]?)?1[3-9]\d{9}(?!\d)", "[手机号]", redacted)


class RunRepository:
    def __init__(self, path):
        self.path = path

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return closing(conn)

    def initialize(self):
        with self.connect() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, user_id TEXT, session_id TEXT, message TEXT, result TEXT
                );
                CREATE TABLE IF NOT EXISTS feedback (
                    id TEXT PRIMARY KEY, run_id TEXT UNIQUE, rating INTEGER, comment TEXT,
                    created_at TEXT, status TEXT, note TEXT DEFAULT ''
                );
                """)

    def reserve(self, user, session, request_id, message):
        run_id = safe_key(user, session + ":" + request_id)[:32]
        with self.connect() as conn, conn:
            conn.execute("INSERT OR IGNORE INTO runs VALUES (?,?,?,?,NULL)", (run_id, user, session, message))
            row = conn.execute("SELECT * FROM runs WHERE id=? AND user_id=?", (run_id, user)).fetchone()
            if row is None or row["message"] != message or row["session_id"] != session:
                raise RequestConflict("同一请求编号已对应另一条消息。")
            return dict(row)

    def get(self, run_id, user=None):
        with self.connect() as conn:
            if user is None:
                row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            else:
                row = conn.execute("SELECT * FROM runs WHERE id=? AND user_id=?", (run_id, user)).fetchone()
            return dict(row) if row is not None else None

    def save(self, run_id, result):
        with self.connect() as conn, conn:
            conn.execute("UPDATE runs SET result=? WHERE id=?", (json.dumps(result, ensure_ascii=False), run_id))

    def feedback(self, user, payload):
        run = self.get(payload.run_id, user)
        if run is None or run["result"] is None:
            return None
        identifier = "F-" + hashlib.sha256(payload.run_id.encode()).hexdigest()[:16].upper()
        with self.connect() as conn, conn:
            conn.execute("""INSERT INTO feedback (id,run_id,rating,comment,created_at,status) VALUES (?,?,?,?,?,'open')
                ON CONFLICT(run_id) DO UPDATE SET rating=excluded.rating,comment=excluded.comment,status='open',note=''""",
                         (identifier, payload.run_id, payload.rating, redact_feedback(payload.comment), utc_timestamp()))
            row = conn.execute("SELECT * FROM feedback WHERE id=?", (identifier,)).fetchone()
            return dict(row)

    def review(self, identifier, payload):
        with self.connect() as conn, conn:
            conn.execute("UPDATE feedback SET status=?,note=? WHERE id=?", (payload.status, redact_feedback(payload.note), identifier))
            row = conn.execute("SELECT * FROM feedback WHERE id=?", (identifier,)).fetchone()
            return dict(row) if row is not None else None

    def list_feedback(self):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM feedback ORDER BY created_at DESC LIMIT 100")]


class RunService:
    def __init__(self, repository, workflow, memory, observability):
        self.repository = repository
        self.workflow = workflow
        self.memory = memory
        self.observability = observability
        self.locks = [asyncio.Lock() for _ in range(64)]
        self.background: set[asyncio.Task] = set()

    async def close(self):
        for task in tuple(self.background):
            task.cancel()
        await asyncio.gather(*self.background, return_exceptions=True)

    def _schedule_memory(self, user, session, message):
        async def update():
            try:
                await self.memory.process(user, session, message)
            except Exception as exc:
                self.observability.observe("memory", "background", "error")
                logger.warning("session enrichment error_type=%s", type(exc).__name__)

        task = asyncio.create_task(update())
        self.background.add(task)
        task.add_done_callback(self.background.discard)

    async def stream(self, run, fault="", resume=False):
        run_id, user, session = run["id"], run["user_id"], run["session_id"]
        lock = self.locks[int(safe_key(user, session)[:8], 16) % len(self.locks)]
        async with lock:
            run = await asyncio.to_thread(self.repository.get, run_id, user)
            yield "started", {"run_id": run_id, "session_id": session}
            if run["result"] is not None:
                yield "done", json.loads(run["result"])
                return

            start = perf_counter()
            outcome = "error"
            fault_token = fault_injection.set(fault)
            try:
                with self.observability.span("api.chat", run_id=run_id, session_id=safe_key(user, session), restored=resume):
                    config = {"configurable": {"thread_id": run_id}}
                    graph = self.workflow.graph
                    snapshot = await graph.aget_state(config)
                    current_id = run_id + ":user"
                    await self.memory.repository.append(user, session, {"id": current_id, "role": "user", "content": run["message"]})
                    initial = None
                    if not snapshot.values.get("run_id"):
                        initial = {"run_id": run_id, "user_id": user, "session_id": session, "message": run["message"],
                                   "memory": await self.memory.build(user, session, current_id)}
                    active_route = snapshot.values.get("route")
                    intent = snapshot.values.get("intent")
                    probabilities = snapshot.values.get("probabilities", {})
                    streamed_notice = snapshot.values.get("notice", "") if snapshot.next else ""
                    if streamed_notice:
                        yield "content", {"text": streamed_notice}
                    if initial or snapshot.next:
                        try:
                            async for group in graph.astream(initial, config, stream_mode="updates", durability="sync"):
                                for delta in group.values():
                                    if not isinstance(delta, dict):
                                        continue
                                    if "route" in delta and delta["route"] != active_route:
                                        active_route = delta["route"]
                                        intent = delta.get("intent", intent)
                                        probabilities = delta.get("probabilities", probabilities)
                                        yield "route", {"route": active_route, "reason": delta.get("reason", ""),
                                                        "intent": intent, "probabilities": probabilities, "run_id": run_id}
                                    if "tool" in delta:
                                        yield "tool", {**delta["tool"], "run_id": run_id}
                                    if delta.get("notice") and delta["notice"] != streamed_notice:
                                        streamed_notice = delta["notice"]
                                        yield "content", {"text": streamed_notice}
                        except RetryableToolFailure as exc:
                            pending = (await graph.aget_state(config)).values
                            active_route = pending.get("route", active_route or "handoff")
                            outcome = "degraded"
                            self.observability.observe("route", active_route, outcome)
                            self.observability.observe("degraded", active_route, "fallback")
                            yield "tool", {"name": exc.tool, "outcome": "error", "run_id": run_id}
                            yield "done", {"run_id": run_id, "status": outcome, "route": active_route,
                                           "reason": "mcp_unavailable", "answer": pending.get("notice", "") + str(exc),
                                           "sources": [], "ticket": None, "degraded": True, "retryable": True}
                            return
                    state = (await graph.aget_state(config)).values
                    result = {"run_id": run_id, "status": "degraded" if state.get("degraded") else "ok",
                              "route": state["route"], "reason": state.get("reason", ""), "intent": state.get("intent"),
                              "answer": state["answer"],
                              "sources": state.get("sources", []), "ticket": state.get("ticket"),
                              "degraded": bool(state.get("degraded")), "retrieval": state.get("retrieval", [])}
                    if "case_analysis" in state:
                        verified_order = state.get("order_data", {})
                        result["case_summary"] = {"analysis": state["case_analysis"],
                                                  "verified_order": verified_order if verified_order.get("found") else None}
                    await self.memory.repository.append(user, session, {"id": run_id + ":assistant", "role": "assistant",
                                                        "content": result["answer"], "sources": result["sources"],
                                                        "ticket": result["ticket"], "facts": state.get("facts", {})})
                    await asyncio.to_thread(self.repository.save, run_id, result)
                    self._schedule_memory(user, session, run["message"])
                    outcome = result["status"]
                    self.observability.observe("route", result["route"], outcome)
                    if result["degraded"]:
                        self.observability.observe("degraded", result["route"], "fallback")
                    if resume:
                        self.observability.observe("recovery", "chat", outcome)

                if result["sources"] or result["retrieval"]:
                    yield "sources", {"items": result["sources"], "diagnostics": result["retrieval"]}
                if result["ticket"]:
                    yield "ticket", result["ticket"]
                remaining = result["answer"].removeprefix(streamed_notice)
                for offset in range(0, len(remaining), 36):
                    yield "content", {"text": remaining[offset:offset + 36]}
                yield "done", result
            finally:
                fault_injection.reset(fault_token)
                self.observability.observe("agent", "chat", outcome, perf_counter() - start)
