import asyncio
import json
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent
from opentelemetry.propagate import inject


logger = logging.getLogger(__name__)


class ToolUnavailable(RuntimeError):
    pass


def error_types(error: BaseException) -> str:
    if isinstance(error, BaseExceptionGroup):
        return ",".join(error_types(child) for child in error.exceptions)
    return type(error).__name__


class CommerceTools:
    def __init__(self, business_db: Path, mysql_url: str, observability, otel_endpoint: str = ""):
        self.parameters = StdioServerParameters(
            command=sys.executable, args=["-m", "mcp_servers.commerce"],
            env={"ECOMMERCE_TOOL_DB": str(business_db.resolve()), "ECOMMERCE_TOOL_MYSQL": mysql_url,
                 "ECOMMERCE_TOOL_OTEL": otel_endpoint, "PYTHONIOENCODING": "utf-8"},
        )
        self.observability = observability
        self.session: ClientSession | None = None
        self.ready = asyncio.Event()
        self.reconnect = asyncio.Event()

    async def _supervise(self):
        while True:
            try:
                async with stdio_client(self.parameters) as (read, write):
                    async with ClientSession(read, write) as session:
                        await asyncio.wait_for(session.initialize(), timeout=90)
                        self.session = session
                        self.ready.set()
                        await self.reconnect.wait()
            except Exception as exc:
                logger.warning("mcp_connection_failed error_type=%s", error_types(exc))
            finally:
                self.session = None
                self.ready.clear()
                self.reconnect.clear()
            await asyncio.sleep(0.5)

    @asynccontextmanager
    async def connect(self):
        task = asyncio.create_task(self._supervise())
        try:
            try:
                await asyncio.wait_for(self.ready.wait(), timeout=95)
            except TimeoutError:
                logger.warning("mcp_not_ready_at_startup")
            yield self
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def call(self, name: str, arguments: dict, *, simulate_failure: bool = False) -> dict:
        started = perf_counter()
        outcome = "error"
        try:
            with self.observability.span(f"mcp.{name}", tool=name):
                session = self.session
                if simulate_failure or session is None:
                    raise ToolUnavailable("MCP unavailable")
                carrier: dict[str, str] = {}
                inject(carrier)
                arguments = {**arguments, "traceparent": carrier.get("traceparent", "")}
                read_only = name == "lookup_order"
                timeout = 10 if read_only else 15
                for attempt in range(2 if read_only else 1):
                    try:
                        result = await asyncio.wait_for(session.call_tool(name, arguments), timeout=timeout)
                        payload = result.structuredContent
                        if not result.isError and payload is None and len(result.content) == 1 and isinstance(result.content[0], TextContent):
                            payload = json.loads(result.content[0].text)
                        if result.isError or not isinstance(payload, dict):
                            logger.warning("mcp_result_invalid tool=%s sdk_error=%s structured_type=%s", name,
                                           result.isError, type(payload).__name__)
                            raise ToolUnavailable(f"MCP rejected {name}")
                        outcome = "ok"
                        return payload
                    except (TimeoutError, OSError, ToolUnavailable) as exc:
                        if attempt == 0 and read_only:
                            continue
                        if isinstance(exc, TimeoutError):
                            outcome = "timeout"
                        if isinstance(exc, (TimeoutError, OSError)):
                            self.reconnect.set()
                        logger.warning("mcp_call_failed tool=%s error_type=%s", name, type(exc).__name__)
                        raise ToolUnavailable(f"MCP call failed: {name}") from exc
                    except Exception as exc:
                        self.reconnect.set()
                        logger.warning("mcp_call_failed tool=%s error_type=%s", name, type(exc).__name__)
                        raise ToolUnavailable(f"MCP call failed: {name}") from exc
                raise ToolUnavailable(f"MCP call failed: {name}")
        finally:
            self.observability.observe("tool", name, outcome, perf_counter() - started)
