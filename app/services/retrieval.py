import asyncio
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from time import perf_counter

import httpx


@dataclass(frozen=True)
class Policy:
    id: str
    title: str
    text: str
    version: str
    effective_from: str
    channel: str
    topic: str
    covers: tuple[str, ...] = ()

    def source(self, score: float) -> dict:
        return {**asdict(self), "score": round(score, 4)}


def load_policies(path) -> tuple[tuple[Policy, ...], set[str]]:
    by_id: defaultdict[str, list[Policy]] = defaultdict(list)
    for payload in json.loads(path.read_text(encoding="utf-8")):
        record = Policy(**{**payload, "covers": tuple(payload.get("covers", []))})
        effective = date.fromisoformat(record.effective_from)
        if not re.fullmatch(r"P-[A-Z0-9-]{1,55}", record.id) or len(record.title + record.text) > 2500:
            raise ValueError(f"Invalid policy record {record.id!r}")
        if effective <= date.today():
            by_id[record.id].append(record)
    if not by_id:
        raise ValueError("At least one policy record must be effective")
    active, conflicting_topics = [], set()
    for versions in by_id.values():
        latest = max(record.effective_from for record in versions)
        effective_versions = [record for record in versions if record.effective_from == latest]
        if len(effective_versions) == 1:
            active.append(effective_versions[0])
        else:
            conflicting_topics.update(record.topic for record in effective_versions)
    return tuple(active), conflicting_topics

_search_rules = json.loads((Path(__file__).resolve().parents[2] / "data" / "policy-search.json").read_text(encoding="utf-8"))
TOPIC_TERMS = _search_rules["topics"]
UNCOVERED = tuple(_search_rules["uncovered"])
PRODUCT_SPEC_TERMS = tuple(_search_rules["product_specs"])


def topics_for(text: str) -> set[str]:
    topics = {topic for topic, terms in TOPIC_TERMS.items() if any(term in text for term in terms)}
    if topics.intersection({"shipping", "exchange", "refund_time"}) and not any(
            term in text for term in ("七天", "退货期限", "退货条件", "能否退", "还能退")):
        topics.discard("return")
    if "invoice_change" in topics:
        topics.discard("invoice")
    return topics


def lexical_tokens(text: str) -> list[str]:
    parts = re.findall(r"[a-z0-9-]+|[\u3400-\u9fff]+", text.lower())
    tokens = []
    for part in parts:
        if part.isascii() or len(part) < 2:
            tokens.append(part)
        else:
            tokens.extend(part[i:i + 2] for i in range(len(part) - 1))
    return tokens


def bm25(query: str, records: tuple[Policy, ...]) -> list[tuple[str, float]]:
    documents = [Counter(lexical_tokens(record.title + record.text)) for record in records]
    average = sum(counts.total() for counts in documents) / len(documents)
    scores: defaultdict[str, float] = defaultdict(float)
    for token in set(lexical_tokens(query)):
        matching = sum(token in document for document in documents)
        inverse_frequency = math.log1p((len(records) - matching + 0.5) / (matching + 0.5))
        for record, counts in zip(records, documents, strict=True):
            if frequency := counts[token]:
                length_factor = 1.2 * (0.25 + 0.75 * counts.total() / average)
                scores[record.id] += inverse_frequency * (frequency * 2.2) / (frequency + length_factor)
    return sorted(scores.items(), key=lambda hit: (-hit[1], hit[0]))


def reciprocal_rank_fusion(*rankings: list[tuple[str, float]], k: int = 60) -> list[tuple[str, float]]:
    scores: defaultdict[str, float] = defaultdict(float)
    for ranking in rankings:
        for position, (item_id, _) in enumerate(ranking, start=1):
            scores[item_id] += 1 / (k + position)
    return sorted(scores.items(), key=lambda hit: (-hit[1], hit[0]))


class RetrievalService:
    def __init__(self, settings, observability, http: httpx.AsyncClient):
        self.settings = settings
        self.observability = observability
        self.http = http
        self.records, self.conflicting_topics = load_policies(settings.knowledge_file)
        self.policies = {policy.id: policy for policy in self.records}
        self.milvus = None
        self.initialize_lock = asyncio.Lock()

    def is_ambiguous(self, query: str) -> bool:
        topics = topics_for(query)
        return bool(self.conflicting_topics and (not topics or topics.intersection(self.conflicting_topics)))

    async def _embeddings(self, texts: list[str]) -> list[list[float]]:
        response = await self.http.post(self.settings.embedding_url.rstrip("/") + "/api/embed", json={
            "model": self.settings.embedding_model, "input": texts,
        })
        response.raise_for_status()
        vectors = response.json()["embeddings"]
        if len(vectors) != len(texts) or not vectors or not vectors[0]:
            raise ValueError("Embedding server returned unexpected dimensions")
        return vectors

    async def _initialize_milvus(self) -> None:
        if self.milvus is not None:
            return
        async with self.initialize_lock:
            if self.milvus is not None:
                return
            texts = [policy.title + "。" + policy.text for policy in self.records]
            vectors = await self._embeddings(texts)
            self.milvus = await asyncio.to_thread(self._index_milvus, vectors)

    def _index_milvus(self, vectors):
        from pymilvus import DataType, Function, FunctionType, MilvusClient

        name = self.settings.milvus_collection
        client = MilvusClient(uri=self.settings.milvus_uri, timeout=8)
        if not client.has_collection(name):
            schema = client.create_schema(auto_id=False)
            schema.add_field("id", DataType.VARCHAR, max_length=60, is_primary=True)
            schema.add_field("body", DataType.VARCHAR, max_length=8192, enable_analyzer=True,
                             analyzer_params={"type": "chinese"})
            schema.add_field("dense", DataType.FLOAT_VECTOR, dim=len(vectors[0]))
            schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
            schema.add_function(Function(name="policy_bm25", function_type=FunctionType.BM25,
                                         input_field_names=["body"], output_field_names=["sparse"]))
            indexes = client.prepare_index_params()
            indexes.add_index(field_name="dense", index_type="FLAT", metric_type="COSINE")
            indexes.add_index(field_name="sparse", index_type="SPARSE_INVERTED_INDEX", metric_type="BM25")
            client.create_collection(collection_name=name, schema=schema, index_params=indexes)
        rows = [{"id": policy.id, "body": policy.title + "。" + policy.text, "dense": vector}
                for policy, vector in zip(self.records, vectors, strict=True)]
        client.upsert(collection_name=name, data=rows)
        client.load_collection(name)
        return client

    async def _milvus_search(self, field: str, value, metric: str) -> list[tuple[str, float]]:
        rows = await asyncio.to_thread(
            self.milvus.search, collection_name=self.settings.milvus_collection, data=[value],
            anns_field=field, limit=4, search_params={"metric_type": metric}, consistency_level="Strong",
            filter=f"id in {json.dumps(list(self.policies))}",
        )
        return [(str(hit["id"]), float(hit["distance"])) for hit in rows[0] if hit["id"] in self.policies]

    async def _full_rerank(self, query: str, candidates: list[tuple[str, float]]) -> list[tuple[str, float]]:
        texts = [self.policies[item_id].text for item_id, _ in candidates]
        headers = {"Authorization": f"Bearer {self.settings.reranker_key}"} if self.settings.reranker_key else {}
        response = await self.http.post(self.settings.reranker_url, json={"query": query, "texts": texts}, headers=headers)
        response.raise_for_status()
        payload = response.json()
        rows = payload["results"] if isinstance(payload, dict) else payload
        scores = {int(hit["index"]): float(hit.get("score", hit.get("relevance_score"))) for hit in rows}
        if set(scores) != set(range(len(texts))) or not all(math.isfinite(score) for score in scores.values()):
            raise ValueError("Reranker must score every candidate exactly by index")
        return sorted(((item_id, scores[index]) for index, (item_id, _) in enumerate(candidates)),
                      key=lambda hit: (-hit[1], hit[0]))

    async def search(self, query: str) -> tuple[list[dict], dict]:
        started = perf_counter()
        outcome = "error"
        variant = self.settings.retrieval_variant
        try:
            with self.observability.span("retrieval.policy", variant=variant):
                if not self.records:
                    outcome = "empty"
                    return [], {name: [] for name in ("dense", "bm25", "rrf", "reranked")}
                if self.settings.milvus_uri:
                    await self._initialize_milvus()
                    vector = (await self._embeddings([query]))[0]
                    if variant == "dense":
                        dense, sparse = await self._milvus_search("dense", vector, "COSINE"), []
                    else:
                        dense, sparse = await asyncio.gather(self._milvus_search("dense", vector, "COSINE"),
                                                             self._milvus_search("sparse", query, "BM25"))
                else:
                    topics = topics_for(query)
                    dense = [(policy.id, 1.0) for policy in self.records if policy.topic in topics]
                    sparse = bm25(query, self.records) if variant != "dense" else []
                fused = reciprocal_rank_fusion(dense, sparse) if variant != "dense" else dense
                reranked = fused
                if variant == "rerank" and fused:
                    reranked = (await self._full_rerank(query, fused) if self.settings.reranker_url else
                                sorted(fused, key=lambda hit: (self.policies[hit[0]].topic not in topics_for(query), -hit[1])))
                outcome = "ok"
                rankings = {name: [{"id": item, "score": round(score, 4)} for item, score in ranking]
                            for name, ranking in (("dense", dense), ("bm25", sparse), ("rrf", fused), ("reranked", reranked))}
                return [self.policies[item].source(score) for item, score in reranked[:4]], rankings
        finally:
            self.observability.observe("retrieval", variant, outcome, perf_counter() - started)
