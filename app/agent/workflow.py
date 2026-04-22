import re
from contextvars import ContextVar
from functools import wraps
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.mcp.client import ToolUnavailable
from app.services.decisions import INTENTS, extract_order, forced_intent, professional_guidance
from app.services.retrieval import topics_for


fault_injection: ContextVar[str] = ContextVar("demo_fault", default="")
PROFESSIONAL_NOTICE = "目前的知识库没有检索到可核验的专业资料。\n\n"
POLICY_NOTICES = {
    "insufficient_evidence": "没有检索到足以确认这个问题的资料。\n\n",
    "evidence_service_unavailable": "知识库暂时无法查询，当前无法核实这个问题。\n\n",
    "conflicting_policy_versions": "检索到的政策版本有冲突，需要由客服核实。\n\n",
}


class RunInterrupted(RuntimeError):
    pass


class RetryableToolFailure(RuntimeError):
    def __init__(self, tool: str, message: str):
        super().__init__(message)
        self.tool = tool


class SupportState(TypedDict, total=False):
    user_id: str
    session_id: str
    run_id: str
    message: str
    memory: dict
    route: str
    intent: str
    reason: str
    probabilities: dict
    order_id: str | None
    order_data: dict
    sources: list[dict]
    retrieval: list[dict]
    tool: dict
    answer: str
    case_analysis: str
    ticket: dict
    degraded: bool
    facts: dict
    policy_query: str
    notice: str
    needs_professional_evidence: bool


class SupportWorkflow:
    def __init__(self, retrieval, decisions, tools, observability, checkpointer):
        self.retrieval = retrieval
        self.decisions = decisions
        self.tools = tools
        self.observability = observability
        graph = StateGraph(SupportState)
        for name in ("classify", "lookup", "policy", "answer_policy", "complex_case", "create_ticket", "clarify", "general"):
            graph.add_node(name, self._traced(name))
        graph.add_edge(START, "classify")
        graph.add_conditional_edges("classify", lambda state: {
            "policy": "policy", "order": "lookup", "logistics": "lookup", "aftersale": "lookup",
            "complex": "lookup" if state.get("order_id") else "complex_case",
            "handoff": "create_ticket", "clarify": "clarify",
            "general": "policy" if state.get("needs_professional_evidence") else "general",
        }[state["route"]], ["policy", "lookup", "complex_case", "create_ticket", "clarify", "general"])
        graph.add_conditional_edges("lookup", lambda state: "complex_case" if state["route"] == "complex" else END,
                                    ["complex_case", END])
        graph.add_conditional_edges("policy", lambda state: {
            "policy": "answer_policy", "handoff": "create_ticket", "general": "general",
        }[state["route"]], ["answer_policy", "create_ticket", "general"])
        graph.add_conditional_edges("answer_policy", lambda state: "create_ticket" if state["route"] == "handoff" else END,
                                    ["create_ticket", END])
        graph.add_edge("complex_case", "create_ticket")
        graph.add_edge("create_ticket", END)
        graph.add_edge("clarify", END)
        graph.add_edge("general", END)
        self.graph = graph.compile(checkpointer=checkpointer)

    def _traced(self, name):
        node = getattr(self, name)

        @wraps(node)
        async def run_node(state):
            with self.observability.span(f"graph.{name}", node=name, run_id=state["run_id"]):
                return await node(state)

        return run_node

    @staticmethod
    def _policy_followup(message: str, recent: list[dict]) -> str | None:
        if len(recent) < 2 or recent[-1]["role"] != "assistant" or recent[-2]["role"] != "user":
            return None
        if topics_for(message) or not re.fullmatch(
            r"(?:那|那么|所以)?[，,\s]*(?:周末|节假日|假期|当天|周六|周日)[^。！？?!]{0,12}[。！？?!\s]*", message.strip()
        ):
            return None
        if re.search(r"\[P-[A-Z0-9-]+\]", recent[-1]["content"]):
            return f"{recent[-2]['content']}\n追问：{message}"
        return None

    async def classify(self, state: SupportState) -> dict:
        message = state["message"]
        facts = state["memory"]["facts"]
        explicit_id = extract_order(message)
        pending = facts.get("pending")
        followup = any(term in message for term in ("上次", "刚才", "上一笔订单", "那单", "那笔", "那个订单", "这笔", "这个订单", "到哪"))
        order_id = explicit_id or (facts.get("order_id") if followup else None)
        route = forced_intent(message)
        policy_query = None
        classified = route
        probabilities = {}
        reason = "matched a deterministic rule"
        if fault_injection.get() == "model":
            self.observability.observe("model", "chat", "timeout")
            return {"route": "handoff", "reason": "model_unavailable", "order_id": order_id, "degraded": True}
        if route is None and explicit_id and pending and message.strip().upper() == explicit_id:
            route = pending
            reason = "continued a pending order lookup"
        if route is None and not explicit_id:
            policy_query = self._policy_followup(message, state["memory"]["recent"])
            if policy_query:
                route = "policy"
                reason = "continued the previous policy question"
        if route is None:
            try:
                probabilities = await self.decisions.classify(message, pending)
                ranked = sorted(((key, float(value)) for key, value in probabilities.items() if key in INTENTS),
                                key=lambda item: -item[1])
                if not ranked:
                    raise ValueError("No known intents returned")
                top, probability = ranked[0]
                classified = top
                runner_up = ranked[1][1] if len(ranked) > 1 else 0
                route = top if probability >= 0.58 and probability - runner_up >= 0.12 else "clarify"
                reason = "classifier threshold applied" if route == "clarify" else "classifier probability"
            except Exception:
                return {"route": "handoff", "reason": "intent_service_unavailable", "order_id": order_id, "degraded": True}
        intent = classified or route
        self.observability.observe("intent", intent, "ok")
        if route == "professional":
            return {"route": "general", "reason": reason, "probabilities": probabilities, "intent": intent,
                    "needs_professional_evidence": True}
        if route in {"order", "logistics", "aftersale"} and not order_id:
            return {"route": "clarify", "reason": "missing_order_id", "probabilities": probabilities,
                    "intent": intent, "facts": {"pending": route}}
        response = {"route": route, "reason": reason, "probabilities": probabilities, "intent": intent, "order_id": order_id}
        if policy_query:
            response["policy_query"] = policy_query
        return response

    async def general(self, state: SupportState) -> dict:
        notice = state.get("notice", "")
        if notice and (guidance := professional_guidance(state["message"])):
            return {"answer": notice + guidance}
        try:
            answer = await self.decisions.answer_general(
                state["message"], state["memory"]["recent"], run_id=state["run_id"], session_id=state["session_id"],
            )
        except Exception:
            return {"answer": notice + "通用问答服务暂时不可用，请稍后再试。",
                    "reason": "general_model_unavailable", "degraded": True}
        return {"answer": notice + ("通用参考\n" if notice else "") + answer}

    async def lookup(self, state: SupportState) -> dict:
        name = "lookup_order"
        route = state["route"]
        try:
            order = await self.tools.call(name, {"user_id": state["user_id"], "order_id": state["order_id"], "view": route},
                                          simulate_failure=fault_injection.get() == "mcp")
        except ToolUnavailable as exc:
            raise RetryableToolFailure(name, "订单工具暂时不可用。服务恢复后可重试。") from exc
        response = {"order_data": order, "tool": {"name": name, "outcome": "ok"}}
        if not order["found"]:
            return {**response, "answer": "未找到此订单，请核对订单号或使用下单账号重试。", "facts": {"pending": None}}
        facts = {"order_id": order["id"], **{key: order[key] for key in
                 ("status", "logistics_status", "refund_status", "amount_cents") if order.get(key)}}
        if route == "order":
            answer = f"订单 {order['id']}（{order['product']}）当前状态为{order['status']}，金额 ¥{order['amount_cents'] / 100:.2f}。"
        elif route == "logistics":
            answer = f"订单 {order['id']} 当前为{order['logistics_status']}，{order['logistics_detail']}。运单号 {order['tracking']}。"
        elif route == "aftersale" and order["refund_id"]:
            answer = f"订单 {order['id']} 的售后单 {order['refund_id']} 当前为{order['refund_status']}。"
        elif route == "aftersale":
            answer = f"订单 {order['id']} 目前没有退款或售后申请记录。"
        else:
            answer = ""
        return {**response, "answer": answer, "facts": facts}

    async def policy(self, state: SupportState) -> dict:
        reports, candidates = [], {}
        question = state.get("policy_query", state["message"])
        professional = state.get("needs_professional_evidence", False)
        queries = (question, question + (" 专业资料 适用条件" if professional else " 售后适用条款 退换货条件 例外"))
        try:
            if not professional and self.retrieval.is_ambiguous(question):
                return self._evidence_gap(professional, "conflicting_policy_versions", reports)
            for attempt, query in enumerate(queries, start=1):
                with self.observability.span("retrieval.attempt", attempt=attempt):
                    found, rankings = await self.retrieval.search(query)
                    candidates.update({item["id"]: item for item in (found[:2] if attempt == 1 else found)})
                    selected, sufficient, relevance = await self.decisions.evidence(question, list(candidates.values()))
                reports.append({"attempt": attempt, "rankings": rankings, "relevance": relevance, "sufficiency": sufficient})
                if sufficient >= 0.80 and selected:
                    return {"route": "policy", "sources": selected, "retrieval": reports}
                if attempt == 1:
                    self.observability.observe("retrieval", "supplement", "started")
        except Exception:
            return self._evidence_gap(professional, "evidence_service_unavailable", reports)
        return self._evidence_gap(professional, "insufficient_evidence", reports)

    @staticmethod
    def _evidence_gap(professional: bool, reason: str, reports: list[dict]) -> dict:
        notice = PROFESSIONAL_NOTICE if professional and reason == "insufficient_evidence" else POLICY_NOTICES[reason]
        return {"route": "general" if professional else "handoff", "reason": reason, "retrieval": reports,
                "notice": notice, "degraded": reason == "evidence_service_unavailable"}

    @staticmethod
    def _policy_excerpt(sources):
        return "\n".join(f"[{source['id']}] {source['text']}" for source in sources)

    async def answer_policy(self, state: SupportState) -> dict:
        sources = state["sources"]
        fallback = self._policy_excerpt(sources)
        if self.decisions.chat_model is None:
            return {"answer": fallback}
        try:
            answer = await self.decisions.completion(
                "chat", "你是售后政策助理。只根据提供的来源回答，保留适用条件，为每条事实使用 [来源ID]，不承诺实际操作。",
                {"question": state.get("policy_query", state["message"]), "sources": sources,
                 "previous_turns": state["memory"]["recent"]},
            )
            cited = set(re.findall(r"\[(P-[A-Z0-9-]+)\]", answer))
            allowed = {source["id"] for source in sources}
            if not cited or not cited.issubset(allowed):
                return {"answer": fallback}
            return {"answer": answer}
        except Exception:
            return {"route": "handoff", "reason": "answer_service_unavailable", "sources": [], "degraded": True}

    async def complex_case(self, state: SupportState) -> dict:
        order = state.get("order_data", {})
        detail = f"已核实订单 {order['id']}，物流状态为{order['logistics_status']}。" if order.get("found") else "订单信息待客服核实。"
        advice = detail + "请保留商品和外包装照片、此前联系记录，便于人工确认后续处理。"
        sources = []
        try:
            question = state["message"]
            candidates, _ = await self.retrieval.search(question)
            usable = [] if self.retrieval.is_ambiguous(question) else candidates
            relevant, sufficient, _ = await self.decisions.evidence(question, usable)
            if sufficient >= 0.80:
                sources = relevant
            if self.decisions.chat_model is not None:
                advice = await self.decisions.completion(
                    "case_analysis", "为售后客服综合核实的订单事实、客户陈述和条款。列出待核查材料。不给出退款或赔偿结论。",
                    {"customer_report": state["message"], "order": order, "clauses": sources,
                     "history": state["memory"]},
                )
        except Exception:
            return {"case_analysis": advice, "reason": "complex_case", "sources": [], "degraded": True}
        return {"case_analysis": advice, "reason": "complex_case", "sources": sources}

    async def create_ticket(self, state: SupportState) -> dict:
        name = "create_ticket"
        try:
            ticket = await self.tools.call(name, {"user_id": state["user_id"], "run_id": state["run_id"],
                                                 "reason": state["reason"], "order_id": state.get("order_id")},
                                           simulate_failure=fault_injection.get() == "mcp")
        except ToolUnavailable as exc:
            raise RetryableToolFailure(name, "暂时无法确认是否已提交工单。服务恢复后可重试，请保留本次会话的追踪号。") from exc
        if fault_injection.get() == "after_ticket":
            raise RunInterrupted("Ticket committed before LangGraph checkpoint")
        prefix = (state.get("case_analysis", "") if state["route"] == "complex"
                  else state.get("notice", POLICY_NOTICES.get(state["reason"], "")))
        return {"ticket": ticket, "answer": f"{prefix}已转人工，工单 {ticket['id']} 等待客服接单。",
                "tool": {"name": name, "outcome": "ok"}}

    async def clarify(self, state: SupportState) -> dict:
        answer = ("请提供订单号，例如 ORD-1001。" if state["reason"] == "missing_order_id"
                  else "你好，请直接说说发生了什么，或者输入你想问的问题。")
        return {"answer": answer}
