from hashlib import sha256
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from redis.asyncio import Redis

from app.agent.mcp_client import CommerceTools
from app.agent.workflow import SupportWorkflow
from app.api.platform import router
from app.config import Settings
from app.core.observability import Observability
from app.services.business import BusinessRepository
from app.services.decisions import DecisionService
from app.services.memory import ChatMemoryRepository, ConversationMemory
from app.services.retrieval import RetrievalService
from app.services.runs import RunRepository, RunService


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(application):
        from app.services.analytics import AnalyticsService

        settings.required_full_settings()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        state = application.state
        async with AsyncExitStack() as stack:
            obs = Observability(settings.otel_endpoint, settings.langfuse_public_key,
                                settings.langfuse_secret_key, settings.langfuse_host)
            stack.callback(obs.close)
            state.observability = obs
            http = await stack.enter_async_context(httpx.AsyncClient(timeout=20))
            decisions = DecisionService(settings, obs, http)
            retrieval = RetrievalService(settings, obs, http)
            state.knowledge_version = sha256(settings.knowledge_file.read_bytes()).hexdigest()
            redis = Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=4) if settings.redis_url else None
            if redis is not None:
                stack.push_async_callback(redis.aclose)
            memory_repo = ChatMemoryRepository(settings.data_dir / "memory.sqlite3", redis)
            memory_repo.initialize()
            memory = ConversationMemory(memory_repo, decisions, settings.memory_variant)
            business_db = settings.data_dir / "business.sqlite3"
            state.business = BusinessRepository(business_db, settings.mysql_url)
            state.business.initialize()
            tools = CommerceTools(business_db, settings.mysql_url, obs, settings.otel_endpoint)
            await stack.enter_async_context(tools.connect())
            state.tools = tools
            run_repo = RunRepository(settings.data_dir / "runs.sqlite3")
            run_repo.initialize()
            saver = await stack.enter_async_context(AsyncSqliteSaver.from_conn_string(str(settings.data_dir / "checkpoints.sqlite3")))
            workflow = SupportWorkflow(retrieval, decisions, tools, obs, saver)
            state.runs = RunService(run_repo, workflow, memory, obs)
            stack.push_async_callback(state.runs.close)
            state.analytics = AnalyticsService(settings.data_dir / "analytics.sqlite3", settings.analytics_mysql_url,
                                               decisions.chat_model, observability=obs)
            yield

    application = FastAPI(title="智能电商客服 Demo", lifespan=lifespan)
    application.state.settings = settings
    application.include_router(router)
    assets = settings.frontend_dir / "assets"
    if assets.is_dir():
        application.mount("/assets", StaticFiles(directory=assets), name="frontend-assets")

    @application.get("/", include_in_schema=False)
    async def index():
        entry = settings.frontend_dir / "index.html"
        if entry.is_file():
            return FileResponse(entry)
        return {"message": "后端已启动。进入 frontend 目录运行 npm ci 和 npm run build，即可加载工作台。",
                "api_docs": "/docs"}

    return application


app = create_app()
