import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient


def build_app(data_dir, **overrides):
    from app.config import Settings
    from app.main import create_app

    return create_app(Settings(data_dir=data_dir, enable_faults=True, **overrides))


def headers(user="alice", fault=""):
    return {"Authorization": f"Bearer demo-{user}", "X-Demo-Fault": fault}


def read_events(response):
    assert response.status_code == 200, response.text
    events = []
    for frame in response.text.strip().split("\n\n"):
        event, data = frame.split("\n", 1)
        events.append((event.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return events


def chat(client, message, user="alice", session="shared", request_id=None, fault=""):
    return read_events(client.post("/api/chat/stream", headers=headers(user, fault), json={
        "message": message, "session_id": session, "request_id": request_id or str(uuid.uuid4()),
    }))


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    with TestClient(build_app(tmp_path_factory.mktemp("support"))) as client:
        yield client


def test_policy_reply_includes_the_valid_clause(client):
    events = chat(client, "普通商品七天无理由退货需要什么条件？")
    assert events[-1][0] == "done"
    result = events[-1][1]
    assert result["route"] == "policy"
    assert result["sources"][0]["id"] == "P-RETURN"
    assert "七天" in result["answer"]


def test_insufficient_evidence_creates_a_ticket(client):
    events = chat(client, "活动赠品拆封了，主商品还能退吗？")
    result = events[-1][1]
    assert result["route"] == "handoff"
    assert result["ticket"]["status"] == "pending"
    assert result["sources"] == []
    notice = next(data["text"] for event, data in events if event == "content")
    assert "没有检索到足以确认" in notice
    assert events.index(("content", {"text": notice})) < next(i for i, (event, _) in enumerate(events) if event == "ticket")


def test_supplement_can_cover_multiple_policy_questions(client):
    result = chat(client, "七天退货条件、退货运费和发票多久能开？")[-1][1]
    assert result["route"] == "policy"
    assert len(result["retrieval"]) == 2
    assert {source["id"] for source in result["sources"]} == {"P-RETURN", "P-SHIPPING", "P-INVOICE"}


def test_return_shipping_cost_is_policy_not_tracking(client):
    result = chat(client, "商品质量问题导致退货，谁出快递费？")[-1][1]
    assert result["route"] == "policy"
    assert [source["id"] for source in result["sources"]] == ["P-SHIPPING"]


@pytest.mark.parametrize(("question", "source", "fact"), [
    ("鞋买小了，能换大一码吗？", "P-EXCHANGE", "换货"),
    ("我填错收货地址了，还能改吗？", "P-ADDRESS", "未发货"),
    ("退的钱通常几天打回来？", "P-REFUND-TIME", "工作日"),
    ("箱子里少了一件东西怎么办？", "P-MISSING", "外包装"),
    ("快递显示签收，可我根本没拿到。", "P-DELIVERY-ISSUE", "签收"),
    ("我买重了，能把这单取消吗？", "P-CANCEL", "待发货"),
    ("报销填错抬头了，怎么重开？", "P-INVOICE-CHANGE", "原发票"),
    ("付钱时扣了两笔，我该怎么做？", "P-PAYMENT", "支付凭证"),
    ("拿到的杯子裂了，要留什么材料？", "P-DAMAGE", "开箱照片"),
    ("我买的蓝色，发来的居然是红色。", "P-WRONG", "商品标签"),
])
def test_new_store_questions_have_grounded_answers(client, question, source, fact):
    result = chat(client, question, session="natural-questions")[-1][1]
    assert result["route"] == "policy"
    assert [item["id"] for item in result["sources"]] == [source]
    assert fact in result["answer"]
    assert result["ticket"] is None


def test_policy_followup_stays_on_previous_subject(client):
    first = chat(client, "退款一般多久到账？", session="policy-followup")[-1][1]
    assert first["sources"][0]["id"] == "P-REFUND-TIME"
    second = chat(client, "那周末也算吗？", session="policy-followup")[-1][1]
    assert second["sources"][0]["id"] == "P-REFUND-TIME"
    assert "周末" in second["answer"]


def test_customer_can_describe_the_problem_instead_of_choosing_a_menu(client):
    result = chat(client, "我想问个事。", session="no-menu")[-1][1]
    assert result["route"] == "clarify"
    assert "发生了什么" in result["answer"]
    assert "请说明你要查询" not in result["answer"]


@pytest.mark.parametrize(("question", "reply", "needs_evidence"), [
    ("为什么天空是蓝色的？", "蓝光在大气中散射得更强。", False),
    ("我这种处方药能和布洛芬一起用吗？", "相互作用取决于具体药物和剂量。", True),
    ("给我写一封简短的请假邮件。", None, False),
])
def test_general_questions_answer_and_disclose_missing_evidence(client, monkeypatch, question, reply, needs_evidence):
    async def local_model(self, url, **kwargs):
        request = httpx.Request("POST", url, json=kwargs["json"])
        if reply is None:
            raise httpx.ConnectError("offline", request=request)
        assert url.endswith("/api/chat")
        assert question in request.content.decode()
        return httpx.Response(200, request=request, json={"message": {"content": reply}})

    monkeypatch.setattr(httpx.AsyncClient, "post", local_model)
    events = chat(client, question, session="general-questions")
    result = events[-1][1]
    assert result["route"] == "general"
    assert result["sources"] == []
    assert result["ticket"] is None
    chunks = [data["text"] for event, data in events if event == "content"]
    assert "".join(chunks) == result["answer"]
    if needs_evidence:
        assert "没有检索到" in chunks[0]
        assert reply in "".join(chunks[1:])
        assert len(result["retrieval"]) == 2
    elif reply:
        assert result["answer"].startswith(reply)
    else:
        assert result["degraded"] is True
        assert "暂时不可用" in result["answer"]


def test_product_parameters_without_a_source_are_not_guessed(client):
    result = chat(client, "你们这款电饭煲的额定功率是多少？", session="product-specs")[-1][1]
    assert result["answer"].startswith("没有检索到足以确认")
    assert result["sources"] == []
    assert result["ticket"]["status"] == "pending"


def test_same_session_id_does_not_share_order_or_history(client):
    alice = chat(client, "订单 ORD-1001 的物流到哪了？", session="tenant-proof")[-1][1]
    bob = chat(client, "帮我查一下订单 ORD-1001", user="bob", session="tenant-proof")[-1][1]
    assert "运输中" in alice["answer"]
    assert "未找到" in bob["answer"]
    assert "运输中" not in bob["answer"]
    bob_history = client.get("/api/sessions/tenant-proof/messages?limit=1", headers=headers("bob")).json()
    assert bob_history["items"][0]["role"] == "user"
    assert bob_history["next_offset"] == 1
    alice_history = client.get("/api/sessions/tenant-proof/messages", headers=headers()).json()
    assert "运输中" in alice_history["items"][-1]["content"]


def test_mcp_write_survives_checkpoint_resume_without_duplicate(tmp_path):
    with TestClient(build_app(tmp_path)) as first:
        events = chat(first, "请转人工", fault="after_ticket")
        run_id = events[0][1]["run_id"]
        assert events[-1][0] == "error"
        tickets = first.get("/api/tickets", headers=headers("support")).json()["items"]
        assert len(tickets) == 1
        ticket_id = tickets[0]["id"]
    with TestClient(build_app(tmp_path)) as second:
        assert second.post(f"/api/runs/{run_id}/resume", headers=headers("bob")).status_code == 404
        result = second.post(f"/api/runs/{run_id}/resume", headers=headers()).json()
        assert result["ticket"]["id"] == ticket_id
        tickets = second.get("/api/tickets", headers=headers("support")).json()["items"]
        assert len(tickets) == 1
        claimed = second.post(f"/api/tickets/{ticket_id}/claim", headers=headers("support"))
        assert claimed.json()["status"] == "claimed"
        history = second.get("/api/sessions/shared/messages", headers=headers()).json()["items"]
        assert [message["role"] for message in history] == ["user", "assistant"]


def test_repeated_request_returns_saved_run_without_more_messages(client):
    request_id = str(uuid.uuid4())
    args = {"session": "retry-proof", "request_id": request_id}
    first = chat(client, "请转人工", **args)[-1][1]
    second = chat(client, "请转人工", **args)[-1][1]
    assert first == second
    assert len(client.get("/api/sessions/retry-proof/messages", headers=headers()).json()["items"]) == 2


def test_order_reference_survives_other_turns(client):
    args = {"session": "memory-proof"}
    chat(client, "帮我查订单 ORD-1001", **args)
    for _ in range(4):
        chat(client, "普通商品七天无理由退货需要什么条件？", **args)
    result = chat(client, "上次说的那笔订单现在到哪了？", **args)[-1][1]
    assert result["route"] == "logistics"
    assert "运输中" in result["answer"]


def test_mcp_fault_returns_recoverable_degraded_response(client):
    result = chat(client, "订单 ORD-1001 的物流", fault="mcp")[-1][1]
    assert result["degraded"] is True
    assert result["status"] == "degraded"
    assert "可重试" in result["answer"]
    retry = client.post(f"/api/runs/{result['run_id']}/resume", headers=headers()).json()
    assert retry["status"] == "ok"
    assert "运输中" in retry["answer"]


def test_staff_can_review_linked_statement_and_order_facts(client):
    message = "数码商品收到破损，已多次催单。我想知道 ORD-1001 的保修期限。"
    result = chat(client, message, session="case-summary")[-1][1]
    assert result["route"] == "complex"
    items = client.get("/api/tickets", headers=headers("support")).json()["items"]
    ticket = next(item for item in items if item["id"] == result["ticket"]["id"])
    assert ticket["customer_message"] == message
    assert ticket["case_summary"]["verified_order"]["logistics_status"] == "运输中"
    assert [source["id"] for source in ticket["sources"]] == ["P-WARRANTY"]
    public = client.get("/api/tickets", headers=headers()).json()["items"]
    assert all("customer_message" not in item for item in public)


def test_conflicting_policy_does_not_block_order_queries(tmp_path):
    from app.config import Settings

    records = json.loads(Settings().knowledge_file.read_text(encoding="utf-8"))
    policy = next(record for record in records if record["id"] == "P-RETURN")
    records.append({**policy, "version": "conflict", "text": "期限为三天。"})
    knowledge = tmp_path / "conflict.json"
    knowledge.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    with TestClient(build_app(tmp_path, knowledge_file=knowledge)) as service:
        policy_result = chat(service, "普通商品七天无理由退货需要什么条件？")[-1][1]
        assert policy_result["route"] == "handoff"
        assert policy_result["reason"] == "conflicting_policy_versions"
        assert policy_result["sources"] == []
        order_result = chat(service, "帮我查一下订单 ORD-1001")[-1][1]
        assert "已支付" in order_result["answer"]


def test_analytics_and_ticket_listing_require_correct_roles(client):
    assert client.post("/api/analytics", headers=headers(), json={"question": "订单量"}).status_code == 403
    assert client.get("/api/tickets", headers=headers("ops")).status_code == 403
    assert client.get("/api/sessions").status_code == 401
