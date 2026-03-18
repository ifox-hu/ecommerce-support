from collections import Counter, defaultdict

import pytest

from evals.dataset import build_cases, select_cases
from evals.feedback_case import amend_knowledge
from evals.run import _retrieval_scores, check_case, parse_sse, summarize
from evals.judge import calibration_summary, parse_scores


def test_case_generator_has_reproducible_counts_and_keeps_paraphrases_together():
    cases = build_cases()
    assert cases == build_cases()
    assert Counter(case.category for case in cases) == {
        "intent": 260, "rag": 260, "workflow": 220, "memory": 160,
        "analytics": 180, "fault_isolation": 120,
    }
    assert len({case.id for case in cases}) == 1200
    splits = defaultdict(set)
    category_splits = defaultdict(set)
    for case in cases:
        splits[case.group].add(case.split)
        category_splits[case.category].add(case.split)
    assert all(len(values) == 1 for values in splits.values())
    assert set(case.split for case in cases) == {"tune", "holdout"}
    assert all(values == {"tune", "holdout"} for values in category_splits.values())
    assert len(select_cases(cases, "smoke")) >= 12


def test_programmatic_checks_detect_wrong_route_citation_and_sql_results():
    policy = next(case for case in build_cases() if case.expected.get("sources") == ["P-RETURN"])
    assert check_case(policy, {"route": "policy", "answer": "退货", "sources": [{"document_id": "P-RETURN"}]})["citations"]
    checks = check_case(policy, {"route": "handoff", "answer": "退货", "sources": []})
    assert checks["route"] is False
    assert checks["citations"] is False

    sales = next(case for case in build_cases() if case.expected.get("rows") == [[10]])
    assert check_case(sales, {"status": "ok", "rows": [[10]], "sql": "SELECT COUNT(*) FROM orders"})["sql_result"]
    assert not check_case(sales, {"status": "ok", "rows": [[9]], "sql": "SELECT COUNT(*) FROM orders"})["sql_result"]


def test_a_clarification_example_does_not_count_as_memory_recall():
    case = next(case for case in build_cases() if case.id == "memory.first_alice_package.01")
    checks = check_case(case, {"route": "clarify", "answer": "请提供订单号，例如 ORD-1001。"})
    assert checks["fact_1"] is False


def test_enabled_empty_retrieval_stages_contribute_zero_and_disabled_stages_are_omitted():
    case = next(case for case in build_cases() if case.id == "rag.return_conditions.01")
    result = {"retrieval": [{"rankings": {"dense": [{"id": "P-RETURN"}], "bm25": [], "rrf": [], "reranked": [{"id": "P-RETURN"}]}}]}
    scores = _retrieval_scores(case, result, "hybrid")
    assert set(scores) == {"dense", "bm25", "rrf"}
    assert scores["dense"]["mrr"] == 1
    assert scores["bm25"] == {"mrr": 0.0, "recall_at_1": 0.0, "recall_at_3": 0.0}


def test_sse_handles_split_json_and_preserves_event_order():
    response = 'event: route\ndata: {"route":"policy"}\n\nevent: done\ndata: {"status":"ok",\ndata: "run_id":"run-a"}\n\n'
    events = parse_sse(response)
    assert [event["event"] for event in events] == ["route", "done"]
    assert events[-1]["data"] == {"status": "ok", "run_id": "run-a"}


def test_report_counts_recall_and_skips_without_claiming_passes():
    outcomes = [
        {"category": "intent", "expected_route": "policy", "checks": {"route": True}, "state": "passed"},
        {"category": "intent", "expected_route": "policy", "checks": {"route": False}, "state": "failed"},
        {"category": "fault_isolation", "state": "skipped", "reason": "needs fault enabled"},
    ]
    report = summarize(outcomes)
    assert report["states"] == {"passed": 1, "failed": 1, "skipped": 1}
    assert report["intent_recall"]["policy"]["rate"] == 0.5
    assert report["failed_checks"] == {"route": 1}
    assert report["skip_reasons"] == {"needs fault enabled": 1}


def test_judge_parser_accepts_rubric_scores_and_rejects_out_of_range_values():
    scores = '{"factual":2,"complete":1,"supported":2,"fallback":0}'
    assert parse_scores(scores) == {"factual": 2, "complete": 1, "supported": 2, "fallback": 0}
    with pytest.raises(ValueError, match="integer from 0 to 2"):
        parse_scores(scores.replace('"factual":2', '"factual":3'))


def test_judge_calibration_summary_compares_human_and_model_ratings():
    human = {"factual": 2, "complete": 1, "supported": 1, "fallback": 2}
    model = {"factual": 2, "complete": 0, "supported": 2, "fallback": 2}
    summary = calibration_summary([(human, model)])
    assert summary["count"] == 1
    assert summary["dimensions"]["factual"] == {"exact_agreement": 1.0, "mean_absolute_error": 0.0}
    assert summary["dimensions"]["supported"] == {"exact_agreement": 0.0, "mean_absolute_error": 1.0}


def test_review_policy_applies_to_candidate_copy_with_stable_source_id():
    before = [{"id": "P-RETURN", "version": "2026.11"}]
    policy = {"id": "P-GIFT-RETURN", "version": "2026.11-review1", "covers": ["赠品"]}
    after = amend_knowledge(before, policy)
    assert [item["id"] for item in before] == ["P-RETURN"]
    assert [item["id"] for item in after] == ["P-RETURN", "P-GIFT-RETURN"]
    assert after[-1]["covers"] == ["赠品"]
    with pytest.raises(ValueError, match="already present"):
        amend_knowledge(after, policy)
