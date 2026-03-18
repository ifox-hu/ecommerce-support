import asyncio
import json
import logging
import secrets
from contextlib import aclosing
from dataclasses import dataclass
from time import perf_counter
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse

from app.models.platform import AnalyticsRequest, ChatRequest, FeedbackRequest, FeedbackReview
from app.services.runs import RequestConflict


logger = logging.getLogger(__name__)
router = APIRouter()


@dataclass(frozen=True)
class Principal:
    user: str
    role: str


DEMO_IDENTITIES = {"demo-alice": "customer:alice", "demo-bob": "customer:bob",
                   "demo-support": "support:support", "demo-ops": "ops:ops"}


def principal(request: Request, authorization: str = Header(default="")) -> Principal:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token:
        settings = request.app.state.settings
        identities = settings.identity_tokens if settings.mode == "full" else DEMO_IDENTITIES
        for configured_token, identity in identities.items():
            if secrets.compare_digest(configured_token.encode(), token.encode()):
                role, _, user = identity.partition(":")
                if role in {"customer", "support", "ops"} and user:
                    return Principal(user, role)
    raise HTTPException(401, "演示身份无效。", headers={"WWW-Authenticate": "Bearer"})


def allowed(actor, *roles):
    if actor.role not in roles:
        raise HTTPException(403, "当前身份不具备访问权限。")


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


@router.get("/api/health")
def health(request: Request):
    settings = request.app.state.settings
    return {"status": "ok", "mode": settings.mode, "mcp": request.app.state.tools.session is not None,
            "retrieval_variant": settings.retrieval_variant, "memory_variant": settings.memory_variant,
            "knowledge_version": request.app.state.knowledge_version,
            "model_version": settings.chat_model if settings.mode == "full" else "deterministic-demo-v1"}


@router.get("/metrics", include_in_schema=False)
def metrics(request: Request):
    return Response(request.app.state.observability.metrics(), media_type="text/plain; version=0.0.4")


@router.post("/api/chat/stream")
async def chat(payload: ChatRequest, request: Request, actor: Principal = Depends(principal),
               x_demo_fault: str = Header(default="")):
    allowed(actor, "customer")
    services = request.app.state
    try:
        run = await asyncio.to_thread(services.runs.repository.reserve, actor.user, payload.session_id,
                                      payload.request_id or uuid4().hex, payload.message)
    except RequestConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    fault = ""
    if services.settings.mode == "demo" and services.settings.enable_faults:
        fault = x_demo_fault if x_demo_fault in {"after_ticket", "model", "mcp"} else ""

    async def events():
        try:
            async with aclosing(services.runs.stream(run, fault)) as stream:
                async for event, data in stream:
                    yield sse(event, data)
        except Exception as exc:
            logger.warning("chat_interrupted run_id=%s error_type=%s", run["id"], type(exc).__name__)
            yield sse("error", {"run_id": run["id"], "message": "本次处理已中断。可使用追踪号继续。"})

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/api/runs/{run_id}/resume")
async def resume(run_id: str, request: Request, actor: Principal = Depends(principal)):
    allowed(actor, "customer")
    service = request.app.state.runs
    run = await asyncio.to_thread(service.repository.get, run_id, actor.user)
    if run is None:
        raise HTTPException(404, "找不到这个执行记录。")
    try:
        async with aclosing(service.stream(run, resume=True)) as stream:
            async for event, result in stream:
                if event == "done":
                    return result
    except Exception as exc:
        logger.warning("resume_interrupted run_id=%s error_type=%s", run_id, type(exc).__name__)
        raise HTTPException(503, {"run_id": run_id, "message": "恢复未完成，请稍后重试。"}) from exc
    raise HTTPException(503, "恢复未完成，请稍后重试。")


@router.get("/api/sessions")
async def sessions(request: Request, actor: Principal = Depends(principal)):
    allowed(actor, "customer")
    return {"items": await request.app.state.runs.memory.repository.sessions(actor.user)}


@router.get("/api/sessions/{session_id}/messages")
async def messages(session_id: str, request: Request, offset: int = Query(0, ge=0),
                   limit: int = Query(50, ge=1, le=100), actor: Principal = Depends(principal)):
    allowed(actor, "customer")
    items, total = await request.app.state.runs.memory.repository.history(actor.user, session_id, offset, limit)
    return {"items": [{key: value for key, value in item.items() if key != "facts"} for item in items],
            "next_offset": offset + len(items) if offset + len(items) < total else None}


@router.get("/api/tickets")
async def tickets(request: Request, actor: Principal = Depends(principal)):
    allowed(actor, "customer", "support")
    services = request.app.state

    def collect():
        items = services.business.tickets(actor.user if actor.role == "customer" else None)
        for item in items:
            run_id = item["run_id"]
            if actor.role == "support":
                run = services.runs.repository.get(run_id)
                if run:
                    item["customer_message"] = run["message"]
                    if run["result"]:
                        result = json.loads(run["result"])
                        item["sources"] = result["sources"]
                        item["case_summary"] = result.get("case_summary")
        return items

    return {"items": await asyncio.to_thread(collect)}


@router.post("/api/tickets/{ticket_id}/claim")
async def claim(ticket_id: str, request: Request, actor: Principal = Depends(principal)):
    allowed(actor, "support")
    item = await asyncio.to_thread(request.app.state.business.claim, ticket_id, actor.user)
    if item is None:
        raise HTTPException(404, "找不到这个工单。")
    if item["owner"] != actor.user:
        raise HTTPException(409, "工单已被其他客服接单。")
    return item


@router.post("/api/analytics")
async def analytics(payload: AnalyticsRequest, request: Request, actor: Principal = Depends(principal)):
    allowed(actor, "ops")
    services = request.app.state
    start = perf_counter()
    with services.observability.span("api.analytics", operation="analytics"):
        result = await services.analytics.ask(payload.question)
    services.observability.observe("agent", "analytics", result["status"], perf_counter() - start)
    return result


@router.post("/api/feedback")
async def submit_feedback(payload: FeedbackRequest, request: Request, actor: Principal = Depends(principal)):
    allowed(actor, "customer")
    item = await asyncio.to_thread(request.app.state.runs.repository.feedback, actor.user, payload)
    if item is None:
        raise HTTPException(404, "找不到可反馈的会话结果。")
    return item


@router.get("/api/feedback")
async def list_feedback(request: Request, actor: Principal = Depends(principal)):
    allowed(actor, "support", "ops")
    return {"items": await asyncio.to_thread(request.app.state.runs.repository.list_feedback)}


@router.patch("/api/feedback/{identifier}")
async def review_feedback(identifier: str, payload: FeedbackReview, request: Request, actor: Principal = Depends(principal)):
    allowed(actor, "support", "ops")
    item = await asyncio.to_thread(request.app.state.runs.repository.review, identifier, payload)
    if item is None:
        raise HTTPException(404, "找不到此反馈。")
    return item
