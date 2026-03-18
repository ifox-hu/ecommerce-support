from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


DATA_VERSION = "synthetic-2026-11-24.1"
SEED = 20261124


@dataclass(frozen=True)
class Case:
    id: str
    category: str
    group: str
    split: str
    prompt: str
    expected: dict[str, Any]
    actor: str = "alice"
    endpoint: str = "chat"
    prelude: tuple[str, ...] = ()
    fault: str = ""
    smoke: bool = False


@dataclass(frozen=True)
class Scenario:
    group: str
    prompt: str
    expected: dict[str, Any]
    count: int = 20
    actor: str = "alice"
    endpoint: str = "chat"
    prelude: tuple[str, ...] = ()
    fault: str = ""
    smoke: bool = False


def policy(group: str, prompt: str, source: str, *facts: str | list[str], smoke: bool = False) -> Scenario:
    expected = {"route": "policy", "sources": [source]}
    if facts:
        expected["facts"] = list(facts)
    return Scenario(group, prompt, expected, smoke=smoke)


def analytics(group: str, prompt: str, rows: list[list[Any]], count: int = 20, smoke: bool = True) -> Scenario:
    return Scenario(group, prompt, {"status": "ok", "rows": rows, "sql": True}, count=count, actor="ops", endpoint="analytics", smoke=smoke)


def memory(group: str, order: str, prompt: str, route: str, *, actor: str = "alice", smoke: bool = False) -> Scenario:
    # These intervening turns never repeat the order identifier.
    prelude = (f"帮我查一下订单 {order}",) + (
        "普通商品七天无理由退货需要什么条件？",
        "质量问题导致退货时，运费由谁承担？",
        "电子发票如何开具？",
        "电子商品的质保期是多久？",
        "七天无理由退货的政策是什么？",
        "退货运费怎么划分？",
        "电子发票在订单完成后多久开具？",
        "电子产品可以保修多久？",
    )
    return Scenario(group, prompt, {"route": route, "facts": [order]}, actor=actor, prelude=prelude, smoke=smoke)


SCENARIOS = {
    "intent": (
        Scenario("order_lookup", "帮我查一下订单 ORD-1001", {"route": "order"}, smoke=True),
        Scenario("order_status", "帮我查一下订单 ORD-2001", {"route": "order"}, actor="bob"),
        Scenario("package_location", "订单 ORD-1001 的物流到哪了", {"route": "logistics"}, smoke=True),
        Scenario("courier_tracking", "查询 ORD-1002 的物流进度", {"route": "logistics"}),
        Scenario("refund_progress", "订单 ORD-1002 的退款进度", {"route": "aftersale"}, smoke=True),
        Scenario("aftersale_status", "帮我查一下 ORD-1002 的售后进度", {"route": "aftersale"}),
        Scenario("human_hard_rule", "请转人工", {"route": "handoff"}, smoke=True),
        Scenario("human_over_order", "订单 ORD-1001 有问题，请转人工", {"route": "handoff"}),
        Scenario("damage_dispute", "收到破损商品并且已经多次催单，帮我处理 ORD-1001", {"route": "complex"}, smoke=True),
        Scenario("return_question", "普通商品七天无理由退货需要什么条件？", {"route": "policy"}),
        Scenario("invoice_question", "订单完成后如何开具电子发票？", {"route": "policy"}),
        Scenario("ambiguous", "我想问个事。", {"route": "clarify"}, smoke=True),
        Scenario("missing_order", "帮我查一下订单", {"route": "clarify"}),
    ),
    "rag": (
        policy("return_conditions", "普通商品七天无理由退货需要什么条件？", "P-RETURN", ["七天", "7天", "七日"], ["完好", "完整"], smoke=True),
        policy("return_window", "七天无理由退货的政策是什么？", "P-RETURN", ["七天", "7天", "七日"], "签收"),
        policy("return_packing", "包装完整的普通商品如何申请七天无理由退货？", "P-RETURN", "订单详情"),
        policy("defective_shipping", "质量问题导致退货时，运费由谁承担？", "P-SHIPPING", ["商城", "商家"], smoke=True),
        policy("personal_shipping", "个人原因退货时，退货运费由谁承担？", "P-SHIPPING", ["买家", "消费者", "顾客", "用户"]),
        policy("shipping_policy", "退货运费怎么划分？", "P-SHIPPING", ["买家", "消费者", "顾客", "用户"], ["商城", "商家"]),
        policy("invoice_timing", "电子发票在订单完成后多久开具？", "P-INVOICE", ["四十八小时", "48小时", "48 小时"], smoke=True),
        policy("invoice_type", "订单完成后可以开电子发票吗？", "P-INVOICE", "电子发票"),
        policy("invoice_issuance", "电子发票如何开具？", "P-INVOICE", "订单详情"),
        policy("electronics_warranty", "电子商品的质保期是多久？", "P-WARRANTY", ["一年", "1年", "1 年"], smoke=True),
        policy("electronics_repair", "电子产品可以保修多久？", "P-WARRANTY", ["一年", "1年", "1 年"]),
        Scenario("opened_gift_evidence_gap", "活动赠品拆封了，主商品还能退吗？", {"route": "handoff", "sources": [], "ticket": True}, smoke=True),
        Scenario("unknown_gift_campaign", "海外渠道赠品活动的促销编号 PROMO-299 是否支持礼品折现？", {"route": "handoff", "sources": [], "ticket": True}),
    ),
    "workflow": (
        Scenario("alice_paid_order", "帮我查一下订单 ORD-1001", {"route": "order", "facts": ["ORD-1001", ["已付款", "已支付"]], "tool": "lookup_order"}, smoke=True),
        Scenario("alice_delivered_order", "帮我查一下订单 ORD-1002", {"route": "order", "facts": ["ORD-1002", ["已送达", "已签收", "已完成"]], "tool": "lookup_order"}),
        Scenario("bob_delivered_order", "帮我查一下订单 ORD-2001", {"route": "order", "facts": ["ORD-2001", ["已送达", "已签收", "已完成"]], "tool": "lookup_order"}, actor="bob"),
        Scenario("alice_in_transit", "订单 ORD-1001 的物流到哪了", {"route": "logistics", "facts": ["ORD-1001", ["运输中", "在途"]], "tool": "lookup_order"}, smoke=True),
        Scenario("alice_signed_delivery", "查询 ORD-1002 的物流进度", {"route": "logistics", "facts": ["ORD-1002", ["已送达", "已签收"]], "tool": "lookup_order"}),
        Scenario("bob_signed_delivery", "订单 ORD-2001 的物流到哪了", {"route": "logistics", "facts": ["ORD-2001", ["已送达", "已签收"]], "tool": "lookup_order"}, actor="bob"),
        Scenario("refund_in_progress", "订单 ORD-1002 的退款进度", {"route": "aftersale", "facts": ["REF-1002", ["处理中", "审核中", "进行中", "待审核"]], "tool": "lookup_order"}, smoke=True),
        Scenario("human_request", "请转人工", {"route": "handoff", "ticket": True, "tool": "create_ticket"}, smoke=True),
        Scenario("human_request_with_context", "订单 ORD-1002 有问题，请转人工", {"route": "handoff", "ticket": True, "tool": "create_ticket"}),
        Scenario("dispute_order_fact", "收到破损商品并且已经多次催单，帮我处理 ORD-1001", {"route": "complex", "ticket": True, "tool": "create_ticket"}, smoke=True),
        Scenario("dispute_delivered_order", "收到破损商品并且已经多次催单，帮我处理 ORD-2001", {"route": "complex", "ticket": True, "tool": "create_ticket"}, actor="bob"),
    ),
    "memory": (
        memory("first_alice_package", "ORD-1001", "刚才那笔订单的物流到哪了？", "logistics", smoke=True),
        memory("second_alice_package", "ORD-1002", "帮我查询刚才那笔订单的物流进度", "logistics"),
        memory("bob_package", "ORD-2001", "上次说的那笔订单现在的物流到哪了？", "logistics", actor="bob"),
        memory("alice_refund", "ORD-1002", "刚才那笔订单的退款进度如何？", "aftersale"),
        memory("first_alice_order", "ORD-1001", "再帮我查一下刚才那笔订单", "order"),
        memory("second_alice_order", "ORD-1002", "想再查一下刚才的订单", "order"),
        memory("bob_order", "ORD-2001", "刚才我的订单帮我再查一下", "order", actor="bob"),
        memory("alice_aftersale", "ORD-1002", "上一笔订单的售后进度怎么样？", "aftersale"),
    ),
    "analytics": (
        analytics("east_refund_rate", "上周华东渠道各品类退款率是多少？", [["家居", 0.0], ["数码", 0.5], ["鞋服", 0.5]]),
        analytics("last_week_order_volume", "上周订单量是多少？", [[10]]),
        analytics("last_week_channel_sales", "上周各渠道销售额", [["华东官方商城", 1430], ["华东微信小程序", 820], ["华南官方商城", 340]]),
        analytics("last_week_category_ranking", "上周品类销售额排行", [["数码", 1500], ["鞋服", 660], ["家居", 430]]),
        analytics("south_refund_rate", "上周华南各品类退款率", [["家居", 1.0], ["鞋服", 0.0]]),
        analytics("east_order_volume", "上周华东订单量", [[8]]),
        analytics("south_sales", "上周华南销售额", [[340]]),
        analytics("shoe_and_clothing_sales", "上周鞋服销售额", [[660]]),
        Scenario("metric_undefined", "上周经营得怎么样？", {"status": "clarify"}, actor="ops", endpoint="analytics", smoke=True),
    ),
    "fault_isolation": (
        Scenario("cross_customer_order", "帮我查一下订单 ORD-2001", {"route": "order", "facts": ["未找到此订单"], "facts_absent": ["已签收", "已送达"], "ticket": False}, count=15, smoke=True),
        Scenario("cross_customer_package", "订单 ORD-1001 的物流到哪了", {"route": "logistics", "facts": ["未找到此订单"], "facts_absent": ["运输中", "在途"], "ticket": False}, count=15, actor="bob", smoke=True),
        Scenario("cross_role_analytics", "上周订单量是多少？", {"http_status": 403}, count=15, endpoint="analytics", smoke=True),
        Scenario("cross_customer_session", "帮我查一下订单 ORD-1001", {"isolated_session": True}, count=15, endpoint="session_isolation", smoke=True),
        Scenario("cross_customer_run", "帮我查一下订单 ORD-1001", {"http_status": 404}, count=15, endpoint="run_isolation", smoke=True),
        Scenario("model_timeout", "普通商品七天无理由退货需要什么条件？", {"route": "handoff", "degraded": True, "ticket": True, "sources": []}, count=15, fault="model", smoke=True),
        Scenario("mcp_unavailable", "帮我查一下订单 ORD-1001", {"degraded": True, "facts_absent": ["已支付"], "ticket": False}, count=15, fault="mcp", smoke=True),
        Scenario("ticket_recovery", "请转人工", {"route": "handoff", "ticket": True, "recovered": True}, count=15, fault="after_ticket", smoke=True),
    ),
}

PREFIXES = ("", "你好，", "麻烦看看，", "请帮忙确认：", "想咨询一下，", "客服你好，")
SUFFIXES = ("", " 谢谢。", " 麻烦了。", " 多谢。", " 谢谢帮忙。", " 请确认一下。")
EXPRESSIONS = {
    "帮我查一下订单": ("查询订单", "帮我看下订单状态"),
    "物流到哪了": ("快递到哪里了", "配送进度如何"),
    "物流进度": ("配送进度", "快递情况"),
    "退款进度": ("退款状态", "退款结果"),
    "售后进度": ("售后状态", "退款进展"),
    "请转人工": ("请找人工客服", "我需要真人客服"),
    "七天无理由": ("7天无理由", "七日无理由"),
    "运费由谁承担": ("邮费由谁出", "谁出快递费"),
    "开具电子发票": ("申请电子发票", "开电子发票"),
    "质保期": ("保修期", "维修期"),
    "上周": ("上一周", "上星期"),
    "退款率": ("退款比例", "退款占比"),
    "订单量": ("订单数量", "订单数"),
    "销售额": ("销售金额", "成交额"),
}


def _variants(scenario: Scenario, rng: random.Random) -> list[str]:
    base = scenario.prompt
    alternatives = [base.replace(original, alternative, 1) for original, forms in EXPRESSIONS.items()
                    if original in base for alternative in forms]
    forms = list(dict.fromkeys([base, *alternatives]))
    wrapped = [f"{prefix}{form}{suffix}" for form in forms for prefix in PREFIXES for suffix in SUFFIXES]
    extra = [variant for variant in wrapped if variant not in forms]
    rng.shuffle(extra)
    return [*forms, *extra][:scenario.count]


def build_cases(seed: int = SEED) -> list[Case]:
    rng = random.Random(seed)
    cases = []
    frequency = Counter(scenario.prompt for scenarios in SCENARIOS.values() for scenario in scenarios)
    holdout = set(rng.sample(list(frequency), k=len(frequency) // 3))
    for scenarios in SCENARIOS.values():
        origins = {scenario.prompt for scenario in scenarios}
        # Move the least shared original when a category has only one split.
        representative = min(origins, key=lambda prompt: (frequency[prompt], prompt))
        if not origins & holdout:
            holdout.add(representative)
        if origins <= holdout:
            holdout.discard(representative)

    origin_groups: dict[str, str] = {}
    for category, scenarios in SCENARIOS.items():
        for scenario in scenarios:
            case_group = f"{category}.{scenario.group}"
            group = origin_groups.setdefault(scenario.prompt, case_group)
            split = "holdout" if scenario.prompt in holdout else "tune"
            for number, prompt in enumerate(_variants(scenario, rng), start=1):
                cases.append(Case(
                    f"{case_group}.{number:02}", category, group, split, prompt, scenario.expected,
                    actor=scenario.actor, endpoint=scenario.endpoint, prelude=scenario.prelude,
                    fault=scenario.fault, smoke=scenario.smoke and number == 1,
                ))
    return cases


def select_cases(cases: list[Case], profile: str) -> list[Case]:
    if profile == "smoke":
        return [case for case in cases if case.smoke]
    if profile in {"tune", "holdout"}:
        return [case for case in cases if case.split == profile]
    return cases


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the versioned synthetic evaluation dataset.")
    parser.add_argument("--output", type=Path, default=Path("evals/cases.jsonl"))
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="\n") as destination:
        for case in build_cases():
            destination.write(json.dumps(asdict(case), ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
