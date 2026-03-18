import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from opentelemetry.context import attach, detach
from opentelemetry.propagate import extract

from app.core.observability import Observability
from app.services.business import BusinessRepository


def main() -> None:
    repo = BusinessRepository(Path(os.environ["ECOMMERCE_TOOL_DB"]), os.getenv("ECOMMERCE_TOOL_MYSQL", ""))
    repo.initialize()
    mcp = FastMCP("ecommerce-tools")
    observability = Observability(otel_endpoint=os.getenv("ECOMMERCE_TOOL_OTEL", ""))

    @mcp.tool(structured_output=True)
    def lookup_order(user_id: str, order_id: str, view: str, traceparent: str = "") -> dict[str, Any]:
        """Read order, delivery or refund facts owned by the verified user."""
        token = attach(extract({"traceparent": traceparent}))
        try:
            with observability.span("db.lookup_order", operation="lookup_order"):
                row = repo.lookup_order(user_id, order_id)
        finally:
            detach(token)
        if row is None:
            return {"found": False}
        fields = {
            "order": ("id", "product", "status", "amount_cents"),
            "logistics": ("id", "logistics_status", "tracking", "logistics_detail"),
            "aftersale": ("id", "refund_id", "refund_status"),
            "complex": ("id", "product", "status", "logistics_status", "refund_status"),
        }[view]
        return {"found": True, **{key: row[key] for key in fields}}

    @mcp.tool(structured_output=True)
    def create_ticket(user_id: str, run_id: str, reason: str, order_id: str | None = None,
                      traceparent: str = "") -> dict[str, Any]:
        """Create one after-sales ticket for the verified user and run."""
        token = attach(extract({"traceparent": traceparent}))
        try:
            with observability.span("db.create_ticket", operation="create_ticket", run_id=run_id):
                if order_id and not repo.lookup_order(user_id, order_id):
                    order_id = None
                return repo.create_ticket(user_id, run_id, reason, order_id)
        finally:
            detach(token)

    try:
        mcp.run(transport="stdio")
    finally:
        observability.close()


if __name__ == "__main__":
    main()
