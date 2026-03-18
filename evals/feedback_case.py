from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from evals.dataset import Case
from evals.run import Client, ProtocolError, check_case, execute_case


DEFAULT_FIXTURE = Path(__file__).parent / "reviews" / "gift-return.json"


def amend_knowledge(records: list[dict[str, Any]], amendment: dict[str, Any]) -> list[dict[str, Any]]:
    if any(record["id"] == amendment["id"] for record in records):
        raise ValueError("review policy is already present in the baseline")
    return [*records, amendment]


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _feedback(client: Client, run_id: str, fixture: dict[str, Any]) -> str:
    submitted = client.json(
        "/api/feedback", method="POST", actor="alice",
        payload={"run_id": run_id, "rating": 1, "comment": fixture["feedback_comment"]},
    )
    identifier = submitted.get("id")
    if not identifier:
        raise ProtocolError("synthetic feedback was not saved")
    records = client.json("/api/feedback", actor="support").get("items", [])
    if not any(record.get("id") == identifier and record.get("status") == "open" for record in records):
        raise ProtocolError("support could not retrieve the open feedback")
    reviewed = client.json(
        f"/api/feedback/{quote(identifier, safe='')}", method="PATCH", actor="support",
        payload={"status": "reviewed", "note": fixture["review_note"]},
    )
    if reviewed.get("status") != "reviewed" or reviewed.get("run_id") != run_id:
        raise ProtocolError("support review was not persisted against the same run")
    return identifier


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay a reviewed synthetic feedback case across two demo instances.")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Build a standalone candidate policy fixture.")
    prepare.add_argument("--knowledge", type=Path, default=Path("data/policies.json"))
    prepare.add_argument("--output", required=True, type=Path)
    replay = commands.add_parser("replay", help="Submit, review and replay the synthetic feedback.")
    replay.add_argument("--baseline-url", required=True)
    replay.add_argument("--candidate-url", required=True)
    replay.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    if args.command == "prepare":
        if args.knowledge.resolve() == args.output.resolve():
            parser.error("write the candidate to a separate file")
        records = json.loads(args.knowledge.read_text(encoding="utf-8"))
        _write(args.output, amend_knowledge(records, fixture["policy"]))
        return 0

    if args.baseline_url.rstrip("/") == args.candidate_url.rstrip("/"):
        parser.error("two separate demo instances are required")
    baseline, candidate = Client(args.baseline_url), Client(args.candidate_url)
    before_meta, after_meta = baseline.json("/api/health"), candidate.json("/api/health")
    if not all(meta.get("mode") == "demo" for meta in (before_meta, after_meta)):
        parser.error("feedback replay requires two demo-mode instances")

    case = Case(**fixture["case"])
    before = execute_case(baseline, case)
    if not before.get("run_id"):
        raise ProtocolError("baseline feedback requires a completed run")
    feedback_id = _feedback(baseline, before["run_id"], fixture)
    after = execute_case(candidate, case)
    checks_before, checks_after = check_case(case, before), check_case(case, after)
    changed = before.get("route") == "handoff" and not all(checks_before.values()) and all(checks_after.values())
    report = {
        "case_id": case.id, "feedback_id": feedback_id, "feedback_state": "reviewed", "passed": changed,
        "amendment": {key: fixture["policy"][key] for key in ("id", "version", "effective_from")},
        "baseline": {"run_id": before.get("run_id"), "route": before.get("route"), "checks": checks_before, "runtime": before_meta},
        "candidate": {"run_id": after.get("run_id"), "route": after.get("route"), "checks": checks_after, "runtime": after_meta},
    }
    _write(args.output, report)
    print(json.dumps({"report": str(args.output), "passed": changed}))
    return int(not changed)


if __name__ == "__main__":
    raise SystemExit(main())
