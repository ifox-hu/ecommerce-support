from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class Metric:
    name: str
    aliases: tuple[str, ...]
    definition: str
    tables: tuple[str, ...]
    expression: str
    example: str


METRICS = {
    "refund_rate": Metric(
        "退款率",
        ("退款率", "退款占比", "退款比例", "退单率"),
        "按下单日期统计，分母为已支付订单数，分子为其中至少有一笔成功退款的订单数。同一订单重复退款只计一次，结果以 0 到 1 的比例表示。",
        ("orders", "payments", "refunds"),
        "ROUND(1.0 * SUM(CASE WHEN EXISTS (SELECT 1 FROM refunds r "
        "WHERE r.order_id = o.order_id AND r.status = 'succeeded') THEN 1 ELSE 0 END) "
        "/ NULLIF(COUNT(*), 0), 4)",
        "SELECT 1.0 * SUM(CASE WHEN EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.order_id "
        "AND r.status = 'succeeded') THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0) AS 退款率 "
        "FROM orders o JOIN payments p ON p.order_id = o.order_id AND p.status = 'paid'",
    ),
    "order_count": Metric(
        "订单量",
        ("订单量", "订单数量", "订单数", "下单量", "下了多少单", "多少笔订单", "订单笔数", "订单总量", "订单有多少", "下单笔数", "多少订单"),
        "按下单日期统计生成的订单笔数，包括待支付及已取消的订单。",
        ("orders",),
        "COUNT(*)",
        "SELECT COUNT(*) AS 订单量 FROM orders o WHERE o.order_date >= '2026-11-16' AND o.order_date < '2026-11-23'",
    ),
    "sales": Metric(
        "销售额",
        ("销售额", "销售金额", "成交额", "支付金额", "支付总额", "实付金额", "营业额", "gmv", "分类排行", "品类排行", "分类排名", "品类排名", "品类榜"),
        "按下单日期统计已完成支付的人民币金额合计，退款金额单独核算。",
        ("orders", "payments"),
        "COALESCE(SUM(p.amount), 0)",
        "SELECT c.name AS 渠道, SUM(p.amount) AS 销售额 FROM orders o JOIN payments p "
        "ON o.order_id = p.order_id AND p.status = 'paid' JOIN channels c "
        "ON c.channel_id = o.channel_id GROUP BY c.name ORDER BY 销售额 DESC, 渠道",
    ),
}

SCHEMA = {
    "orders": {"order_id": "INT", "order_date": "DATE", "product_id": "INT", "channel_id": "INT", "status": "TEXT"},
    "payments": {"order_id": "INT", "amount": "DECIMAL", "status": "TEXT"},
    "refunds": {"refund_id": "INT", "order_id": "INT", "amount": "DECIMAL", "status": "TEXT"},
    "products": {"product_id": "INT", "category": "TEXT", "name": "TEXT"},
    "channels": {"channel_id": "INT", "name": "TEXT", "region": "TEXT"},
}

TABLE_DESCRIPTIONS = {
    "orders": "订单与下单日期。product_id 和 channel_id 分别连接商品表和渠道表。",
    "payments": "支付记录，每个 order_id 至多一条。status = 'paid' 表示已支付。amount 单位为人民币元。",
    "refunds": "可有多笔退款。按 order_id 连接订单，status = 'succeeded' 表示成功。",
    "products": "商品 name 与品类 category。示例品类为家居、数码、鞋服。",
    "channels": "渠道 name 与所属区域 region。示例区域为华东、华南。",
}

TABLE_TERMS = {
    "orders": ("订单", "下单", "上周", "上月", "日期", "本周"),
    "payments": ("支付", "销售", "成交", "金额", "退款率"),
    "refunds": ("退款", "退单", "退货"),
    "products": ("商品", "分类", "品类", "家居", "数码", "鞋服", "类目"),
    "channels": ("渠道", "平台", "区域", "地区", "华东", "华南", "商城", "小程序"),
}

SYNONYM_GROUPS = tuple(metric.aliases for metric in METRICS.values()) + (
    ("品类", "类别", "分类", "类目"),
    ("渠道", "平台", "来源"),
    ("区域", "地区", "地域"),
)


@dataclass(frozen=True)
class Document:
    key: str
    terms: tuple[str, ...]
    body: str


CATALOG = {
    "指标词典": tuple(Document(key, metric.aliases, metric.name) for key, metric in METRICS.items()),
    "表字段元数据": tuple(
        Document(table, TABLE_TERMS[table], f"{table} ({', '.join(f'{key} {kind}' for key, kind in columns.items())})。{TABLE_DESCRIPTIONS[table]}")
        for table, columns in SCHEMA.items()
    ),
    "业务口径": tuple(Document(key, metric.aliases, metric.definition) for key, metric in METRICS.items()),
    "示例 SQL": tuple(Document(key, metric.aliases, metric.example) for key, metric in METRICS.items()),
}


@dataclass(frozen=True)
class Period:
    start: date | None = None
    stop: date | None = None

    @property
    def label(self) -> str:
        if self.start is None or self.stop is None:
            return "全部下单日期"
        return f"{self.start.isoformat()} 至 {(self.stop - timedelta(days=1)).isoformat()}（含首尾）"


def date_period(question: str, today: date) -> Period:
    dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", question, flags=re.ASCII)
    if dates:
        if len(dates) > 2:
            raise ValueError("请指定一个连续的下单日期区间。")
        start, end = date.fromisoformat(dates[0]), date.fromisoformat(dates[-1])
        if end < start:
            raise ValueError("下单结束日期应晚于或等于开始日期。")
        return Period(start, end + timedelta(days=1))

    week = today - timedelta(days=today.weekday())
    if "上周" in question or "上一周" in question or "上星期" in question:
        return Period(week - timedelta(days=7), week)
    if "本周" in question or "这周" in question or "这星期" in question:
        return Period(week, today + timedelta(days=1))
    if "上月" in question or "上个月" in question:
        month = today.replace(day=1)
        return Period((month - timedelta(days=1)).replace(day=1), month)
    if "本月" in question or "这个月" in question:
        return Period(today.replace(day=1), today + timedelta(days=1))
    if "昨天" in question or "昨日" in question:
        return Period(today - timedelta(days=1), today)
    if "今天" in question or "今日" in question:
        return Period(today, today + timedelta(days=1))

    days = re.search(r"(?:近|过去)\s*(\d{1,3}|七|三十)\s*天", question)
    if days:
        count = {"七": 7, "三十": 30}.get(days[1]) or int(days[1])
        if count < 1:
            raise ValueError("请指定至少一天的下单日期范围。")
        return Period(today - timedelta(days=count - 1), today + timedelta(days=1))
    if re.search(r"\d{1,4}\s*[年月日]|去年|今年|季度|半年|前天", question):
        raise ValueError("请用 YYYY-MM-DD 或两个日期指定要查询的下单日期范围。")
    return Period()


def demo_sql(metric_id: str, question: str, period: Period) -> str:
    metric = METRICS[metric_id]
    projections = []
    joins = []
    filters = []
    group = []

    if "payments" in metric.tables:
        joins.append("JOIN payments p ON p.order_id = o.order_id AND p.status = 'paid'")
    categories = [value for value in ("家居", "数码", "鞋服") if value in question]
    by_category = any(term in question for term in ("品类", "分类", "类目", "类别")) and not categories
    if by_category or categories:
        joins.append("JOIN products item ON item.product_id = o.product_id")
    if by_category:
        projections.append("item.category AS 品类")
        group.append("item.category")
    if categories:
        filters.append("item.category IN (" + ", ".join(f"'{value}'" for value in categories) + ")")

    regions = [region for region in ("华东", "华南") if region in question]
    channels = [fragment for fragment in ("官方商城", "微信小程序") if fragment in question]
    by_channel = any(term in question for term in ("各渠道", "分渠道", "按渠道", "每个渠道", "各平台", "按平台"))
    by_channel = by_channel or ("渠道" in question and not (regions or channels or by_category))
    by_region = any(term in question for term in ("各区域", "分区域", "按区域", "各地区", "按地区"))
    if by_channel or by_region or regions or channels:
        joins.append("JOIN channels c ON c.channel_id = o.channel_id")
    if by_channel:
        projections.append("c.name AS 渠道")
        group.append("c.name")
    if by_region:
        projections.append("c.region AS 区域")
        group.append("c.region")
    if regions:
        filters.append("c.region IN (" + ", ".join(f"'{value}'" for value in regions) + ")")
    if channels:
        filters.append("(" + " OR ".join(f"c.name LIKE '%{value}'" for value in channels) + ")")

    if period.start and period.stop:
        filters.extend((f"o.order_date >= '{period.start.isoformat()}'", f"o.order_date < '{period.stop.isoformat()}'"))

    projections.append(f"{metric.expression} AS {metric.name}")
    parts = [f"SELECT {', '.join(projections)} FROM orders o", *joins]
    if filters:
        parts.append("WHERE " + " AND ".join(filters))
    if group:
        parts.append("GROUP BY " + ", ".join(group))
        ordering = [] if metric_id == "refund_rate" and not re.search(r"排行|排名", question) else [f"{metric.name} DESC"]
        parts.append("ORDER BY " + ", ".join([*ordering, *group]))
    return "\n".join(parts)
