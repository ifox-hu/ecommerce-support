from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ECOMMERCE_", env_file=".env", extra="ignore")

    mode: Literal["demo", "full"] = "demo"
    data_dir: Path = Path(".runtime")
    frontend_dir: Path = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    enable_faults: bool = False
    identity_tokens: dict[str, str] = {}
    redis_url: str = ""
    mysql_url: str = ""
    analytics_mysql_url: str = ""
    milvus_uri: str = ""
    milvus_collection: str = "ecommerce_policies_v1"
    knowledge_file: Path = Path(__file__).resolve().parents[1] / "data" / "policies.json"
    embedding_url: str = "http://127.0.0.1:11434"
    embedding_model: str = "bge-m3"
    general_model_provider: Literal["ollama", "openai"] = "ollama"
    general_model_url: str = "http://127.0.0.1:11434"
    general_model: str = "deepseek-r1:7b"
    general_model_api_key: SecretStr = SecretStr("")
    reranker_url: str = ""
    reranker_key: str = ""
    chat_base_url: str = ""
    chat_api_key: str = ""
    chat_model: str = ""
    jev_api_key: str = ""
    jev_model: str = "jev-latest"
    retrieval_variant: Literal["dense", "hybrid", "rerank"] = "rerank"
    memory_variant: Literal["recent", "layered"] = "layered"
    otel_endpoint: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = ""

    def required_full_settings(self) -> None:
        if self.mode != "full":
            return
        required = ["identity_tokens", "redis_url", "mysql_url", "analytics_mysql_url", "milvus_uri",
                    "embedding_url", "chat_base_url", "chat_api_key", "chat_model", "jev_api_key"]
        if self.retrieval_variant == "rerank":
            required.append("reranker_url")
        missing = [name for name in required if not getattr(self, name)]
        if missing:
            variables = ", ".join(f"ECOMMERCE_{name.upper()}" for name in missing)
            raise ValueError(f"Full mode requires: {variables}")
