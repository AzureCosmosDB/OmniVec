"""Pydantic schemas for the OmniVec Search API.

Self-describing multi-index search request: each index carries its own store
config, vector column config, and embedding policy. The search service has no
knowledge of OmniVec "destinations" or "pipelines" — that resolution is the
caller's responsibility (done client-side by the Playground UI, or by an
admin-tier resolver that hydrates index specs from stored destination docs).
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator


# -----------------------------------------------------------------------------
# Store auth
# -----------------------------------------------------------------------------


class ManagedIdentityAuth(BaseModel):
    mode: Literal["managed_identity"] = "managed_identity"


class KeyAuth(BaseModel):
    mode: Literal["key"] = "key"
    # "kv://<vault-name>/<secret-name>" — resolved server-side via Key Vault
    secret_ref: str


StoreAuth = Union[ManagedIdentityAuth, KeyAuth]


# -----------------------------------------------------------------------------
# Stores
# -----------------------------------------------------------------------------


class CosmosStore(BaseModel):
    type: Literal["cosmosdb"] = "cosmosdb"
    endpoint: str
    database: str
    container: str
    partition_key: Optional[str] = None
    auth: StoreAuth = Field(default_factory=ManagedIdentityAuth)


class PgVectorStore(BaseModel):
    type: Literal["pgvector"] = "pgvector"
    # Either dsn OR dsn_secret_ref OR individual host/user/password/database
    dsn: Optional[str] = None
    dsn_secret_ref: Optional[str] = None
    host: Optional[str] = None
    port: int = 5432
    user: Optional[str] = None
    password: Optional[str] = None
    database: Optional[str] = None
    ssl_mode: str = "require"

    table: str
    id_column: str = "id"
    content_column: str = "content"
    metadata_columns: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_connectivity(self):
        if not (self.dsn or self.dsn_secret_ref or (self.host and self.database)):
            raise ValueError("pgvector store requires dsn, dsn_secret_ref, or host+database")
        return self


StoreConfig = Union[CosmosStore, PgVectorStore]


# -----------------------------------------------------------------------------
# Vector + embedding policy
# -----------------------------------------------------------------------------


class VectorConfig(BaseModel):
    """Where the vector lives inside a document and its shape."""
    field: str = "embedding"  # Cosmos path or pg column name
    dims: Optional[int] = None  # advisory; validated against query embedding
    metric: Literal["cosine", "l2", "dot"] = "cosine"


class ModelEmbedding(BaseModel):
    policy: Literal["model"] = "model"
    model_id: str  # e.g. "mdl-bge-large" — routed to DocGrok by model_id
    normalize: bool = False
    input_modality: Literal["text", "image"] = "text"


class PipelineEmbedding(BaseModel):
    policy: Literal["pipeline"] = "pipeline"
    pipeline: str  # named DocGrok pipeline
    normalize: bool = False
    input_modality: Literal["text", "image"] = "text"


class PrecomputedEmbedding(BaseModel):
    policy: Literal["precomputed"] = "precomputed"
    vector: List[float]  # skip embedding call entirely for this index


EmbeddingPolicy = Union[ModelEmbedding, PipelineEmbedding, PrecomputedEmbedding]


# -----------------------------------------------------------------------------
# Optional per-index filter (opaque to server; passed through to store)
# -----------------------------------------------------------------------------


class IndexFilter(BaseModel):
    """Pre-filter applied at the store layer.

    CosmosDB: `where` is a WHERE-clause fragment (no WHERE keyword) with
      @-named params. The alias is `c` (the container document).
      Example: `c.tenant = @tenant AND c.status = 'published'`
    pgvector: `where` is a SQL fragment with $N positional params; N starts
      at $3 because $1=vector, $2=top_k are reserved.
    """
    where: str
    params: Union[Dict[str, Any], List[Any]] = Field(default_factory=dict)


# -----------------------------------------------------------------------------
# Index spec
# -----------------------------------------------------------------------------


class IndexSpec(BaseModel):
    id: str = Field(..., description="Caller-supplied label; echoed in results")
    store: StoreConfig = Field(..., discriminator="type")
    vector: VectorConfig = Field(default_factory=VectorConfig)
    embedding: EmbeddingPolicy = Field(..., discriminator="policy")
    content_fields: List[str] = Field(default_factory=lambda: ["content"])
    return_fields: List[str] = Field(default_factory=list)
    filter: Optional[IndexFilter] = None
    top_k: Optional[int] = Field(default=None, ge=1, le=500)
    pipeline_id: Optional[str] = Field(default=None, description="Source pipeline id; surfaced into result metadata for blob preview")


# -----------------------------------------------------------------------------
# Merge + include
# -----------------------------------------------------------------------------


class MergeConfig(BaseModel):
    strategy: Literal["rrf", "score", "round_robin", "per_index"] = "rrf"
    rrf_k: int = Field(default=60, ge=1, le=1000)
    per_index_top_k: int = Field(default=20, ge=1, le=500)


class IncludeOptions(BaseModel):
    vectors: bool = False
    scores: bool = True
    debug: bool = False


# -----------------------------------------------------------------------------
# Request / response
# -----------------------------------------------------------------------------


class SearchRequest(BaseModel):
    query: Optional[str] = None
    # Base64-encoded image bytes to use as query for image-modality indexes.
    query_image_b64: Optional[str] = None
    top_k: int = Field(default=10, ge=1, le=100)
    merge: MergeConfig = Field(default_factory=MergeConfig)
    indexes: List[IndexSpec] = Field(..., min_length=1, max_length=10)
    include: IncludeOptions = Field(default_factory=IncludeOptions)
    request_id: Optional[str] = None
    # If true, reject the whole request on any per-index failure.
    # Default false → partial failures return 200 with errors in per_index.
    strict: bool = False

    @model_validator(mode="after")
    def _require_query_when_needed(self):
        # Need at least one query input.
        if not (self.query or self.query_image_b64):
            raise ValueError("provide either 'query' (text) or 'query_image_b64' (image)")
        # Image-modality indexes accept either a text query (embedded via
        # CLIP text encoder) or an image query. Text-modality indexes
        # require a text query — they will be skipped at search time
        # if only an image is provided.
        return self


class SearchResult(BaseModel):
    index_id: str
    id: Any
    rank: int
    score: Optional[float] = None
    rrf_score: Optional[float] = None
    text: str = ""
    text_parts: Optional[List[Dict[str, str]]] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    vector: Optional[List[float]] = None
    source: Optional[str] = None
    source_ref: Optional[str] = None
    # "text" | "image" | "video" — derived from the index's embedding modality
    # and from per-doc hints (e.g. blob filename extension, frame markers).
    entity_type: str = "text"


class PerIndexInfo(BaseModel):
    index_id: str
    embedding_model: Optional[str] = None
    embedding_dims: Optional[int] = None
    embedding_ms: Optional[int] = None
    search_ms: Optional[int] = None
    result_count: int = 0
    error: Optional[str] = None


class SearchTiming(BaseModel):
    embed: int = 0
    search: int = 0
    merge: int = 0
    total: int = 0


class SearchResponse(BaseModel):
    request_id: str
    query: Optional[str] = None
    results: List[SearchResult]
    per_index: List[PerIndexInfo]
    merge: MergeConfig
    warnings: List[str] = Field(default_factory=list)
    timing_ms: SearchTiming
