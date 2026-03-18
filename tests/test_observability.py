import gzip
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from app.core.observability import Observability


def test_scrape_has_operation_duration_and_tokens_without_request_labels():
    first = Observability()
    second = Observability()
    with first.span("api.chat", run_id="run-local", message="private prompt"):
        first.observe("model", "chat", "ok", duration=0.4, tokens=16)

    metrics = first.metrics().decode()
    assert 'ecommerce_operations_total{kind="model",name="chat",outcome="ok"} 1.0' in metrics
    assert 'ecommerce_operation_duration_seconds_count{kind="model",name="chat",outcome="ok"} 1.0' in metrics
    assert 'ecommerce_tokens_total{kind="model",name="chat"} 16.0' in metrics
    assert 'kind="model",name="chat",outcome="ok"' not in second.metrics().decode()
    assert 'run-local' not in metrics
    assert 'private prompt' not in metrics


def test_span_failure_logs_run_and_error_type_without_exception_text(caplog):
    telemetry = Observability()
    with caplog.at_level(logging.WARNING), pytest.raises(TimeoutError):
        with telemetry.span("api.chat", run_id="run-local"):
            with telemetry.span("mcp.call"):
                raise TimeoutError("private exception detail")

    assert "run_id=run-local" in caplog.text
    assert "span=mcp.call" in caplog.text
    assert "error_type=TimeoutError" in caplog.text
    assert "private exception detail" not in caplog.text
    assert telemetry.langchain_config() == {}


def test_handled_timeout_still_records_operation_and_run(caplog):
    telemetry = Observability()
    with caplog.at_level(logging.WARNING), telemetry.span("graph.classify", run_id="run-recoverable"):
        telemetry.observe("model", "chat", "timeout")
    assert "run_id=run-recoverable" in caplog.text
    assert "operation=chat" in caplog.text
    assert "outcome=timeout" in caplog.text


@pytest.mark.asyncio
async def test_langfuse_traces_direct_async_models_under_same_redacted_run():
    langfuse = pytest.importorskip("langfuse")
    pytest.importorskip("langchain")
    from langchain_openai import ChatOpenAI
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    received = []

    class LocalService(BaseHTTPRequestHandler):
        def do_POST(self):
            request = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path.endswith("/chat/completions"):
                payload = json.dumps({
                    "id": "chatcmpl-synthetic", "object": "chat.completion", "created": 1, "model": "synthetic-chat",
                    "choices": [{"index": 0, "finish_reason": "stop", "message": {
                        "role": "assistant", "content": "private-answer-probe",
                    }}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 6, "total_tokens": 17},
                }).encode()
                content_type = "application/json"
            else:
                if self.headers.get("Content-Encoding") == "gzip":
                    request = gzip.decompress(request)
                if self.path.endswith("/v1/traces"):
                    batch = ExportTraceServiceRequest.FromString(request)
                    received.extend(span for item in batch.resource_spans for group in item.scope_spans for span in group.spans)
                payload, content_type = b"", "application/x-protobuf"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), LocalService) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        telemetry = Observability(
            langfuse_public_key="pk-lf-local-callback-test",
            langfuse_secret_key="sk-lf-local-callback-test",
            langfuse_host=url,
        )
        model = ChatOpenAI(model="synthetic-chat", api_key="synthetic-key", base_url=url + "/v1", max_retries=0)
        try:
            for restored in (False, True):
                with telemetry.span("api.chat", run_id="observability-test-run", session_id="private-session-probe", restored=restored):
                    with telemetry.langchain_span() as config:
                        response = await model.ainvoke("private-input-probe", config=config)
                        assert response.usage_metadata["total_tokens"] == 17
            telemetry.close()
        finally:
            server.shutdown()
            thread.join()

    trace_id = langfuse.Langfuse.create_trace_id(seed="observability-test-run")
    assert len(received) == 4
    assert {span.trace_id.hex() for span in received} == {trace_id}
    parents = {span.span_id for span in received if span.name == "ecommerce.model"}
    generations = [span for span in received if span.parent_span_id in parents]
    assert len(generations) == 2
    assert all(b"session.id" in span.SerializeToString() for span in received if span.span_id in parents)
    assert all(b"langfuse.observation.usage_details" in span.SerializeToString() for span in generations)
    assert all(b"private-" not in span.SerializeToString() for span in received)
