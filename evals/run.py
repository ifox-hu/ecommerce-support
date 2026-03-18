from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from statistics import mean
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from evals.dataset import Case, DATA_VERSION, SEED, build_cases, select_cases
from evals.judge import Judge, JudgeSettings, calibration_summary, parse_scores


RETRIEVAL_STAGES = {
    "dense": ("dense",),
    "hybrid": ("dense", "bm25", "rrf"),
    "rerank": ("dense", "bm25", "rrf", "reranked"),
}


class ProtocolError(Exception):
    pass


def parse_sse(text: str) -> list[dict[str, Any]]:
    events = []
    event_name = "message"
    payload = []
    for line in [*text.split("\n"), ""]:
        line = line.removesuffix("\r")
        if not line:
            if payload:
                events.append({"event": event_name, "data": json.loads("\n".join(payload))})
            event_name, payload = "message", []
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "event":
            event_name = value
        elif field == "data":
            payload.append(value)
    return events


def _event(events: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((event["data"] for event in reversed(events) if event["event"] == name), None)


def _source_ids(sources: list[Any]) -> set[str]:
    ids = {source for source in sources if isinstance(source, str)}
    ids.update(str(source.get("document_id") or source.get("id") or "") for source in sources if isinstance(source, dict))
    return ids


def check_case(case: Case, result: dict[str, Any]) -> dict[str, bool]:
    expected = case.expected
    if "http_status" in expected:
        return {"http_status": result.get("http_status") == expected["http_status"]}

    checks = {}
    if "http_status" in result:
        checks["http_status"] = result["http_status"] == 200
    for key in ("route", "status", "degraded", "recovered", "isolated_session"):
        if key in expected:
            checks[key] = result.get(key) == expected[key]
    if expected.get("route") == "handoff":
        checks["handoff"] = result.get("route") == "handoff"
    if "ticket" in expected:
        checks["ticket"] = bool(result.get("ticket")) is expected["ticket"]
    if "sources" in expected:
        actual = _source_ids(result.get("sources") or [])
        checks["citations"] = actual == set(expected["sources"])
    if "sources_include" in expected:
        actual = _source_ids(result.get("sources") or [])
        checks["citations"] = set(expected["sources_include"]).issubset(actual)
    if "facts" in expected:
        answer = str(result.get("answer") or "").casefold()
        for index, fact in enumerate(expected["facts"], 1):
            alternatives = [fact] if isinstance(fact, str) else fact
            matches = any(alternative.casefold() in answer for alternative in alternatives)
            checks[f"fact_{index}"] = matches and (case.category != "memory" or checks.get("route", False))
    if "facts_absent" in expected:
        content = json.dumps(result, ensure_ascii=False).casefold()
        checks["isolation"] = all(fact.casefold() not in content for fact in expected["facts_absent"])
    if "rows" in expected:
        checks["sql_result"] = result.get("rows") == expected["rows"]
    if expected.get("sql"):
        checks["sql_returned"] = bool(result.get("sql"))
    if "tool" in expected:
        checks["tool_success"] = any(
            tool.get("name") == expected["tool"] and tool.get("outcome") in {"ok", "success"}
            for tool in result.get("tools", [])
        )
    return checks


class Client:
    def __init__(self, base_url: str, timeout: float = 45, *, mode: str = "demo") -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.tokens_by_actor = {
            actor: os.getenv(f"EVAL_TOKEN_{actor.upper()}") or (f"demo-{actor}" if mode == "demo" else "")
            for actor in ("alice", "bob", "support", "ops")
        }

    def request(
        self, path: str, *, method: str = "GET", actor: str = "", payload: dict[str, Any] | None = None,
        fault: str = "",
    ) -> tuple[int, str]:
        headers = {"Content-Type": "application/json"}
        if actor:
            if not self.tokens_by_actor[actor]:
                raise ProtocolError(f"missing credential EVAL_TOKEN_{actor.upper()}")
            headers["Authorization"] = f"Bearer {self.tokens_by_actor[actor]}"
        if fault:
            headers["X-Demo-Fault"] = fault
        body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        request = Request(f"{self.base_url}{path}", data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read().decode()
        except HTTPError as response:
            with response:
                return response.code, response.read().decode(errors="replace")

    def json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        status, body = self.request(path, **kwargs)
        if status == 200:
            return {**json.loads(body), "http_status": status}
        return {"http_status": status}

    def chat(self, actor: str, session_id: str, prompt: str, fault: str = "") -> tuple[int, list[dict[str, Any]]]:
        status, body = self.request(
            "/api/chat/stream", method="POST", actor=actor, fault=fault,
            payload={"session_id": session_id, "message": prompt, "request_id": str(uuid.uuid4())},
        )
        return status, parse_sse(body) if status == 200 else []

    def tokens(self) -> int | None:
        try:
            status, body = self.request("/metrics")
            if status != 200:
                return None
            tokens = re.findall(r"^ecommerce_tokens_total\{[^\n]*?\}\s+(\S+)", body, re.MULTILINE)
            return int(sum(float(value) for value in tokens))
        except (URLError, OSError, OverflowError, ValueError):
            return None


def _recover_ticket(client: Client, actor: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    error = _event(events, "error") or {}
    run_id = error.get("run_id")
    if not run_id:
        raise ProtocolError("interruption did not expose a recoverable run_id")
    path = f"/api/runs/{quote(run_id, safe='')}/resume"
    result = client.json(path, method="POST", actor=actor)
    repeated = client.json(path, method="POST", actor=actor)
    tickets = client.json("/api/tickets", actor=actor).get("items", [])
    ticket = result.get("ticket") or {}
    ticket_id = ticket.get("id") or ticket.get("ticket_id")
    repeated_ticket = repeated.get("ticket") or {}
    repeated_id = repeated_ticket.get("id") or repeated_ticket.get("ticket_id")
    matching = [item for item in tickets if item.get("run_id") == run_id]
    result["recovered"] = bool(
        result.get("http_status") == 200 and ticket_id and repeated_id == ticket_id
        and len(matching) == 1 and (matching[0].get("id") or matching[0].get("ticket_id")) == ticket_id
    )
    return result


def execute_case(client: Client, case: Case) -> dict[str, Any]:
    if case.endpoint == "analytics":
        return client.json("/api/analytics", method="POST", actor=case.actor, payload={"question": case.prompt})

    session_id = f"eval-{uuid.uuid4()}"
    for entry in case.prelude:
        status, events = client.chat(case.actor, session_id, entry)
        if status != 200 or _event(events, "done") is None:
            raise ProtocolError("conversation setup did not complete")

    status, events = client.chat(case.actor, session_id, case.prompt, case.fault)
    if status != 200:
        return {"http_status": status}
    if case.fault == "after_ticket":
        return _recover_ticket(client, case.actor, events)
    result = _event(events, "done")
    if result is None:
        raise ProtocolError("chat stream did not contain a done event")
    if case.endpoint == "session_isolation":
        path = f"/api/sessions/{session_id}/messages"
        owner = client.json(path, actor=case.actor)
        other = client.json(path, actor="bob")
        return {"http_status": other["http_status"], "isolated_session": bool(owner.get("items")) and other.get("items") == []}
    if case.endpoint == "run_isolation":
        path = f"/api/runs/{quote(result['run_id'], safe='')}/resume"
        return client.json(path, method="POST", actor="bob")
    route = _event(events, "route") or {}
    return {
        **result, "http_status": status, "intent": route.get("intent"),
        "tools": [event["data"] for event in events if event["event"] == "tool"],
    }


def _retrieval_scores(case: Case, result: dict[str, Any], variant: str) -> dict[str, dict[str, float]]:
    expected = set(case.expected.get("sources") or [])
    if not expected:
        return {}
    attempts = result.get("retrieval") or []
    rankings = attempts[-1].get("rankings", {}) if attempts else {}
    scores = {}
    for stage in RETRIEVAL_STAGES[variant]:
        ids = [item["id"] for item in rankings.get(stage, [])]
        first_relevant = next((index for index, item in enumerate(ids, start=1) if item in expected), None)
        scores[stage] = {
            "mrr": 1 / first_relevant if first_relevant else 0.0,
            "recall_at_1": len(expected.intersection(ids[:1])) / len(expected),
            "recall_at_3": len(expected.intersection(ids[:3])) / len(expected),
        }
    return scores


def evaluate(client: Client, case: Case, *, faults: bool, judge: Judge | None, retrieval_variant: str) -> dict[str, Any]:
    outcome: dict[str, Any] = {"id": case.id, "category": case.category, "group": case.group, "split": case.split}
    if "route" in case.expected:
        outcome["expected_route"] = case.expected["route"]
    if case.fault and not faults:
        return {**outcome, "state": "skipped", "reason": "demo fault injection requires --faults and an enabled backend"}
    start = time.perf_counter()
    try:
        result = execute_case(client, case)
        checks = check_case(case, result)
        scores = _retrieval_scores(case, result, retrieval_variant)
    except (URLError, TimeoutError, OSError, ValueError, KeyError, TypeError, ProtocolError) as error:
        reason = str(error) if isinstance(error, ProtocolError) else type(error).__name__
        return {**outcome, "state": "error", "reason": reason, "retrieval": _retrieval_scores(case, {}, retrieval_variant)}
    outcome.update(
        state="passed" if all(checks.values()) else "failed", checks=checks,
        actual_route=result.get("route"), actual_status=result.get("status"), run_id=result.get("run_id"),
        duration_seconds=round(time.perf_counter() - start, 6),
    )
    if scores:
        outcome["retrieval"] = scores
    if judge is not None and case.category in {"rag", "workflow"} and result.get("answer"):
        try:
            outcome["judge"] = judge.score(case, result)
        except (URLError, TimeoutError, OSError, ValueError, KeyError, IndexError, TypeError, RuntimeError) as error:
            outcome["judge_error"] = type(error).__name__
    return outcome


def _rate(values: list[bool]) -> dict[str, Any]:
    return {"correct": sum(values), "count": len(values), "rate": round(mean(values), 6)}


def summarize(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, Counter] = defaultdict(Counter)
    categories: dict[str, Counter] = defaultdict(Counter)
    checks: dict[str, list[bool]] = defaultdict(list)
    recalls: dict[str, list[bool]] = defaultdict(list)
    reasons: dict[str, Counter] = defaultdict(Counter)
    failed_checks: Counter = Counter()
    judge: dict[str, list[float]] = defaultdict(list)
    latencies: dict[str, list[float]] = defaultdict(list)
    retrieval: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for case in outcomes:
        category = case["category"]
        categories[category][case["state"]] += 1
        groups[case.get("group", category)][case["state"]] += 1
        if case.get("reason"):
            reasons[case["state"]][case["reason"]] += 1
        if case.get("judge_error"):
            reasons["judge_error"][case["judge_error"]] += 1
        for check, value in case.get("checks", {}).items():
            if not value:
                failed_checks[check] += 1
            if check.startswith("fact_"):
                checks["memory_order_reference" if category == "memory" else "answer_fact"].append(value)
            elif check != "route" or category == "intent":
                checks[check].append(value)
        if category == "intent" and "route" in case.get("checks", {}):
            recalls[case["expected_route"]].append(case["checks"]["route"])
        for check, score in case.get("judge", {}).items():
            judge[check].append(score)
        for stage, values in case.get("retrieval", {}).items():
            for metric, value in values.items():
                retrieval[stage][metric].append(value)
        if "duration_seconds" in case:
            latencies[category].append(case["duration_seconds"])

    def latency(values: list[float]) -> dict[str, Any]:
        ordered = sorted(values)
        return {"p50": round(ordered[(len(ordered) - 1) // 2], 6), "p95": round(ordered[(95 * len(ordered) - 1) // 100], 6)}

    return {
        "states": dict(Counter(case["state"] for case in outcomes)),
        "categories": dict(categories), "groups": dict(groups),
        "checks": {name: _rate(values) for name, values in checks.items()},
        "intent_recall": {name: _rate(values) for name, values in recalls.items()},
        "latency_seconds": {name: latency(values) for name, values in latencies.items()},
        "judge": {name: {"count": len(values), "mean_out_of_2": round(mean(values), 4)} for name, values in judge.items()},
        "retrieval": {stage: {metric: {"count": len(values), "mean": round(mean(values), 6)} for metric, values in metrics.items()} for stage, metrics in retrieval.items()},
        "failed_checks": dict(failed_checks),
        "skip_reasons": dict(reasons["skipped"]), "error_reasons": dict(reasons["error"]),
        "judge_error_reasons": dict(reasons["judge_error"]),
    }


def _comparison(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    if baseline.get("cases_sha256") != current.get("cases_sha256"):
        raise ValueError("evaluation reports use different case sets")
    first, second = baseline["summary"], current["summary"]
    percentages = {
        name: round((second["checks"][name]["rate"] - value["rate"]) * 100, 4)
        for name, value in first["checks"].items()
        if name in second["checks"] and value["count"] == second["checks"][name]["count"]
    }
    memory_before = first["checks"].get("memory_order_reference", {}).get("rate")
    memory_after = second["checks"].get("memory_order_reference", {}).get("rate")
    relative = round((memory_after / memory_before - 1) * 100, 4) if memory_before and memory_after is not None else None
    latencies = {
        category: round(second["latency_seconds"][category]["p95"] - values["p95"], 6)
        for category, values in first["latency_seconds"].items() if category in second["latency_seconds"]
    }
    before_tokens, after_tokens = baseline.get("observed_tokens"), current.get("observed_tokens")
    before_stage = RETRIEVAL_STAGES.get(baseline["runtime"].get("retrieval_variant"), ("",))[-1]
    after_stage = RETRIEVAL_STAGES.get(current["runtime"].get("retrieval_variant"), ("",))[-1]
    before_retrieval = first["retrieval"].get(before_stage, {})
    after_retrieval = second["retrieval"].get(after_stage, {})
    retrieval_delta = {
        metric: round((after_retrieval[metric]["mean"] - result["mean"]) * 100, 4)
        for metric, result in before_retrieval.items()
        if metric in after_retrieval and result["count"] == after_retrieval[metric]["count"]
    }
    return {
        "baseline_runtime": baseline["runtime"], "check_delta_percentage_points": percentages,
        "memory_delta_relative_percent": relative, "p95_delta_seconds": latencies,
        "retrieval_delta_percentage_points": retrieval_delta,
        "token_delta": after_tokens - before_tokens if before_tokens is not None and after_tokens is not None else None,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run labeled ecommerce evaluations against the backend HTTP API.")
    parser.add_argument("--base-url", default=os.getenv("EVAL_BASE_URL", "http://127.0.0.1:8765"))
    parser.add_argument("--profile", default="smoke", choices=("smoke", "tune", "holdout", "all"))
    parser.add_argument("--category", action="append", choices=("intent", "rag", "workflow", "memory", "analytics", "fault_isolation"))
    parser.add_argument("--case", action="append", help="Case ID or group prefix. May be repeated.")
    parser.add_argument("--output", type=Path, default=Path("evals/report.json"))
    parser.add_argument("--compare", type=Path, help="Baseline report for the same case set.")
    parser.add_argument("--faults", action="store_true", help="Execute cases that require the demo fault switch.")
    parser.add_argument("--judge", action="store_true", help="Use configured EVAL_JUDGE_* credentials.")
    parser.add_argument("--judge-calibration", type=Path, help="JSON array of manually scored responses for calibration.")
    parser.add_argument("--require-mode", choices=("demo", "full"))
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--fail-on-regression", action="store_true")
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    cases = build_cases()
    selected = select_cases(cases, "all" if args.case else args.profile)
    if args.category:
        selected = [case for case in selected if case.category in args.category]
    if args.case:
        selected = [case for case in selected if any(case.id.startswith(prefix) or case.group == prefix for prefix in args.case)]
    if not selected or args.workers < 1:
        parser.error("selection must include cases and workers must be positive")

    try:
        runtime = Client(args.base_url, args.timeout).json("/api/health")
    except (URLError, TimeoutError, ValueError) as error:
        parser.error(f"backend health check failed ({type(error).__name__})")
    if runtime["http_status"] != 200:
        parser.error("backend health check failed")
    if args.require_mode and runtime.get("mode") != args.require_mode:
        parser.error(f"backend must report {args.require_mode} mode")
    retrieval_variant = runtime.get("retrieval_variant", "rerank")
    if retrieval_variant not in RETRIEVAL_STAGES:
        parser.error("backend reported an unknown retrieval variant")
    client = Client(args.base_url, args.timeout, mode=runtime.get("mode", "demo"))
    try:
        judge = Judge(JudgeSettings.from_environment(), timeout=args.timeout) if args.judge else None
    except (ValueError, ImportError) as error:
        parser.error(str(error))
    calibration = None
    if args.judge_calibration:
        if judge is None:
            parser.error("--judge-calibration requires --judge")
        cases_by_id = {case.id: case for case in cases}
        try:
            manual = json.loads(args.judge_calibration.read_text(encoding="utf-8"))
            pairs = [
                (parse_scores(json.dumps(sample["human"])), judge.score(cases_by_id[sample["case_id"]], sample["response"]))
                for sample in manual
            ]
            calibration = calibration_summary(pairs)
        except (KeyError, TypeError, ValueError, OSError) as error:
            parser.error(f"judge calibration could not complete ({type(error).__name__})")
    before_tokens = client.tokens()
    outcomes = []
    with ThreadPoolExecutor(max_workers=args.workers) as workers:
        results = workers.map(
            lambda case: evaluate(client, case, faults=args.faults, judge=judge, retrieval_variant=retrieval_variant), selected,
        )
        for index, outcome in enumerate(results, start=1):
            outcomes.append(outcome)
            if index % 100 == 0 and index < len(selected):
                print(f"evaluated {index}/{len(selected)} cases", file=sys.stderr, flush=True)
    after_tokens = client.tokens()
    if judge is not None:
        judge.close()

    summary = summarize(outcomes)
    report = {
        "dataset_version": DATA_VERSION, "dataset_seed": SEED, "dataset_size": len(cases),
        "cases_sha256": sha256(json.dumps([asdict(case) for case in selected], ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
        "profile": "explicit" if args.case else args.profile,
        "runtime": runtime, "model_version": os.getenv("EVAL_MODEL_VERSION", runtime.get("model_version", "unreported")),
        "prompt_version": os.getenv("EVAL_PROMPT_VERSION", runtime.get("prompt_version", "unreported")),
        "knowledge_version": os.getenv("EVAL_KNOWLEDGE_VERSION", runtime.get("knowledge_version", "unreported")),
        "judge_model": judge.settings.model if judge else None,
        "judge_skip_reason": None if judge else os.getenv("EVAL_JUDGE_SKIP_REASON", "no Judge requested"),
        "judge_rubric_version": judge.rubric_version if judge else None,
        "judge_calibration": calibration,
        "observed_tokens": after_tokens - before_tokens if before_tokens is not None and after_tokens is not None and after_tokens >= before_tokens else None,
        "summary": summary, "outcomes": outcomes,
    }
    if args.compare:
        baseline = json.loads(args.compare.read_text(encoding="utf-8"))
        report["comparison"] = _comparison(baseline, report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(args.output), "mode": runtime.get("mode"), **summary["states"]}, ensure_ascii=False))
    if args.fail_on_regression:
        return int(bool(summary["states"].get("failed") or summary["states"].get("error") or summary["judge_error_reasons"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
