import os
from uuid import uuid4

import httpx
import pytest

from app.config import Settings
from app.core.observability import Observability
from app.services.retrieval import RetrievalService, reciprocal_rank_fusion


def test_reciprocal_rank_favors_items_found_on_both_paths():
    pairs = reciprocal_rank_fusion([("dense-first", 92), ("shared", 2)],
                                  [("sparse-first", 317), ("shared", 1)])
    assert [key for key, _ in pairs] == ["shared", "dense-first", "sparse-first"]


async def test_real_bge_embedding_and_milvus_bm25_search():
    uri = os.environ.get("TEST_MILVUS_URI")
    if not uri:
        pytest.skip("set TEST_MILVUS_URI and make BGE-M3 available in Ollama")
    settings = Settings(milvus_uri=uri, retrieval_variant="hybrid",
                        milvus_collection=f"ecommerce_test_{uuid4().hex}",
                        embedding_url=os.environ.get("TEST_EMBEDDING_URL", "http://127.0.0.1:11434"))
    obs = Observability()
    try:
        async with httpx.AsyncClient(timeout=45) as http:
            retrieval = RetrievalService(settings, obs, http)
            try:
                sources, rankings = await retrieval.search("普通商品七天无理由退货需要什么条件？")
                assert rankings["dense"][0]["id"] == "P-RETURN"
                assert rankings["bm25"][0]["id"] == "P-RETURN"
                assert rankings["rrf"][0]["id"] == "P-RETURN"
                assert sources[0]["version"] == "2026.11"
            finally:
                if retrieval.milvus is not None:
                    retrieval.milvus.drop_collection(settings.milvus_collection)
                    retrieval.milvus.close()
    finally:
        obs.close()
