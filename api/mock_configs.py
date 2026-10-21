"""Validated benchmark-only component settings; no real document storage."""
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal


class MockSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_count: int = Field(default=100000, ge=1, le=10000000)
    document_size_bytes: int = Field(default=8, ge=1, le=1048576)
    documents_per_second: float = Field(default=0, ge=0, le=10000000, allow_inf_nan=False)
    batch_size: int = Field(default=50, ge=1, le=2048)
    seed: int = Field(default=0, ge=0, le=2147483647)
    embedding_transport: Literal["json", "fp32"] = "json"
    runner_shard: int = Field(default=0, ge=0, le=31)


class MockSinkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted_documents_per_second: float = Field(default=0, ge=0, le=10000000, allow_inf_nan=False)
    burst_documents: int = Field(default=2048, ge=1, le=1000000)
    embedding_dimensions: int = Field(default=1024, ge=1, le=65536)
    receiver_shard: int = Field(default=0, ge=0, le=31)


class MockEmbeddingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    embedding_dim: int = Field(default=1024, ge=1, le=65536)
    latency_ms: float = Field(default=0, ge=0, le=60000, allow_inf_nan=False)
