"""Verify a real backend restart and one ticket for the interrupted run."""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

import httpx


ROOT = Path(__file__).resolve().parents[1]


def wait_for_mcp(client, process):
    deadline = monotonic() + 110
    while monotonic() < deadline and process.poll() is None:
        try:
            response = client.get("/api/health", timeout=1)
            if response.is_success and response.json()["mcp"]:
                return
        except httpx.RequestError:
            pass
        sleep(0.2)
    raise RuntimeError("Backend did not start with MCP connected")


def launch(port, state, log):
    command = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)]
    env = {**os.environ, "ECOMMERCE_MODE": "demo", "ECOMMERCE_DATA_DIR": str(state), "ECOMMERCE_ENABLE_FAULTS": "1"}
    return subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=log,
                            stdin=subprocess.DEVNULL, start_new_session=os.name != "nt")


def stop(process):
    if os.name == "nt":
        if process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], check=True, capture_output=True)
    else:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=15)


def ticket_for_run(client, run_id):
    items = client.get("/api/tickets").raise_for_status().json()["items"]
    matches = [item for item in items if item["run_id"] == run_id]
    assert len(matches) == 1, matches
    return matches[0]["id"]


def main() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="ecommerce-recovery-", ignore_cleanup_errors=True) as directory:
        state = Path(directory)
        with (state / "server.log").open("w+", encoding="utf-8") as log, httpx.Client(
            base_url=base_url, headers={"Authorization": "Bearer demo-alice"},
        ) as client:
            process = launch(port, state, log)
            try:
                wait_for_mcp(client, process)
                session = uuid4().hex
                body = {"session_id": session, "request_id": uuid4().hex, "message": "请转人工"}
                response = client.post("/api/chat/stream", json=body, headers={"X-Demo-Fault": "after_ticket"})
                frames = response.raise_for_status().text.strip().split("\n\n")
                assert frames[0].startswith("event: started\n"), frames
                assert frames[-1].startswith("event: error\n"), frames
                run_id = json.loads(frames[0].split("data: ", 1)[1])["run_id"]
                ticket = ticket_for_run(client, run_id)
                first_pid = process.pid
                stop(process)
                with socket.socket() as verification_socket:
                    verification_socket.settimeout(1)
                    assert verification_socket.connect_ex(("127.0.0.1", port)) != 0, "Original backend still listening"
                process = launch(port, state, log)
                wait_for_mcp(client, process)
                assert process.pid != first_pid
                result = client.post(f"/api/runs/{run_id}/resume").raise_for_status().json()
                assert result["ticket"]["id"] == ticket == ticket_for_run(client, run_id), result
                history = client.get(f"/api/sessions/{session}/messages").raise_for_status().json()["items"]
                assert [message["role"] for message in history] == ["user", "assistant"], history
                print(f"Verified backend restart {first_pid} -> {process.pid}, ticket {ticket}, one saved reply.")
            except Exception:
                log.seek(0)
                print(log.read()[-6000:], file=sys.stderr)
                raise
            finally:
                if process.poll() is None:
                    stop(process)


if __name__ == "__main__":
    main()
