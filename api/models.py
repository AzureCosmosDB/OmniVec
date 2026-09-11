"""OmniVec Data Models"""

from enum import Enum
import re
from urllib.parse import urlsplit
from typing import Optional, List, Dict, Any, Union, Literal  # lgtm[py/unused-import]
from pydantic import BaseModel, Field, field_validator, model_validator
from datetime import datetime


# Optional informational metadata fields that destination writers may persist
# on each vector document. Functionally-required fields (id, vector,
# pipeline_id, source_id, content_hash, embedded_at, cfp_generation /
# pipeline_generation) are NOT toggleable because purge-by-source, CFP
# regeneration, and dashboards depend on them.
ALLOWED_METADATA_FIELDS = {"pipeline_name", "embedding_dims", "source_ref"}


# =============================================================================
# ENUMS
# =============================================================================

class SourceType(str, Enum):
    AZURE_BLOB = "azure-blob"
    COSMOSDB = "cosmosdb"
    POSTGRESQL = "postgresql"
    MSSQL = "mssql"
    S3 = "s3"
    HTTP = "http"
    DATABRICKS = "databricks"
    ONELAKE_ICEBERG = "onelake-iceberg"
    SHAREPOINT = "sharepoint"


class DestinationType(str, Enum):
    COSMOSDB_VECTOR = "cosmosdb-vector"
    PGVECTOR = "pgvector"
    MSSQL = "mssql"
    ONELAKE_ICEBERG = "onelake-iceberg"


class TriggerType(str, Enum):
    EVENT_GRID = "event-grid"      # Real-time blob events
    CHANGE_FEED = "change-feed"    # Real-time CosmosDB CDC
    SCHEDULE = "schedule"          # Cron-based
    MANUAL = "manual"              # On-demand


class JobStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PipelineStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    ERROR = "error"


class ContentStrategy(str, Enum):
    TRUNCATE = "truncate"   # Embed full text as single vector (default)
    CHUNK = "chunk"         # Split text into chunks, embed each separately


# =============================================================================
# SOURCE CONFIGURATIONS
# =============================================================================

class ContentType(str, Enum):
    """Supported content types for processing"""
    # Text formats
    TXT = "txt"
    JSON = "json"
    CSV = "csv"
    MD = "md"
    HTML = "html"
    XML = "xml"
    # Document formats
    PDF = "pdf"
    DOCX = "docx"
    PPTX = "pptx"
    XLSX = "xlsx"
    # Image formats (for vision models)
    PNG = "png"
    JPG = "jpg"
    JPEG = "jpeg"
    GIF = "gif"
    WEBP = "webp"
    # Audio formats
    MP3 = "mp3"
    WAV = "wav"
    M4A = "m4a"


class CosmosDBContentMode(str, Enum):
    """How to get content from CosmosDB documents"""
    FIELD = "field"           # Content is directly in a document field
    BLOB_URL = "blob_url"     # Field contains Azure Blob URL
    HTTP_URL = "http_url"     # Field contains HTTP/HTTPS URL
    S3_URL = "s3_url"         # Field contains S3 URL


class AzureBlobConfig(BaseModel):
    connection_string: Optional[str] = None
    account_url: Optional[str] = None  # For managed identity
    container: str
    prefix: Optional[str] = ""


class CosmosDBSourceConfig(BaseModel):
    endpoint: str
    database: str
    container: str
    query: Optional[str] = "SELECT * FROM c"
    use_change_feed: bool = True


class PostgreSQLSourceConfig(BaseModel):
    """PostgreSQL source configuration for reading rows as documents."""
    host: str
    port: int = 5432
    database: str
    user: Optional[str] = None  # Use managed identity if not provided
    password: Optional[str] = None
    ssl_mode: str = "require"  # disable, allow, prefer, require, verify-ca, verify-full
    table: str  # Table to read from
    id_column: str = "id"  # Primary key column
    timestamp_column: str = "updated_at"  # For change tracking (polling)
    query: Optional[str] = None  # Optional custom query instead of table
    poll_interval_seconds: int = 60  # How often to poll for changes
    batch_size: int = 100  # Rows per batch


class S3Config(BaseModel):
    bucket: str
    prefix: Optional[str] = ""
    region: str = "us-east-1"


class HTTPConfig(BaseModel):
    url: str
    method: str = "GET"
    headers: Dict[str, str] = {}
    auth_type: Optional[str] = None  # "bearer", "basic", "api-key"


class DatabricksSourceConfig(BaseModel):
    """Databricks Delta Lake source consumed via Change Data Feed (CDF).

    The connector polls `table_changes('<catalog>.<schema>.<table>', <since>+1)`
    on a SQL Warehouse and emits one ingest event per inserted/updated row.
    Deletes propagate as tombstone events (worker side removes the vector).

    auth_type:
      - "pat"               : personal access token, fetched at scan time via
                              `pat_secret_ref` (Azure Key Vault secret id) or
                              `DATABRICKS_TOKEN` env var fallback.
      - "managed-identity"  : workspace OAuth token from Azure AD using the
                              Databricks resource id `2ff814a6-3304-4ab8-85cb-cd0e6f879c1d`
                              and the worker pod's MI/Workload Identity.
    """
    workspace_url: str                 # https://adb-<id>.<region>.azuredatabricks.net
    http_path: str                     # /sql/1.0/warehouses/<warehouse-id>
    catalog: str
    # NB: field is named schema_name to avoid clashing with pydantic BaseModel.schema;
    # the JSON wire form uses the "schema" key (consumed by the .NET watcher).
    schema_name: str = Field(alias="schema")
    table: str
    auth_type: str = "managed-identity"  # "pat" | "managed-identity"
    pat_secret_ref: Optional[str] = None  # Key Vault secret URI when auth_type=pat
    content_column: str = "content"
    id_column: str = "id"
    poll_interval_seconds: int = 60
    batch_size: int = 200              # max rows per CDF page

    model_config = {"populate_by_name": True}


class OneLakeIcebergSourceConfig(BaseModel):
    """A read-only OneLake Iceberg REST catalog source.

    Authentication is always obtained through DefaultAzureCredential by the
    dedicated PyIceberg watcher; no catalog or storage secret is accepted.
    """
    catalog_uri: str = "https://onelake.table.fabric.microsoft.com/iceberg"
    warehouse: str  # <workspaceId>/<dataItemId>
    namespace: Union[str, List[str]]
    table: str
    content_fields: List[str] = ["content"]
    id_field: str = "id"
    poll_interval_seconds: int = 60
    batch_size: int = 200
    fabric_retry_interval_seconds: int = 900
    checkpoint_account_url: str = "https://onelake.dfs.fabric.microsoft.com"
    checkpoint_file_system: Optional[str] = None  # defaults to workspaceId
    checkpoint_path: str = ".omnivec/checkpoints"

    model_config = {"extra": "forbid"}

    @field_validator("warehouse")
    @classmethod
    def _validate_warehouse(cls, value: str) -> str:
        parts = value.strip("/").split("/")
        if len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z0-9-]+", part) for part in parts):
            raise ValueError("warehouse must be '<workspaceId>/<dataItemId>'")
        return "/".join(parts)

    @field_validator("catalog_uri")
    @classmethod
    def _validate_catalog_uri(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.hostname != "onelake.table.fabric.microsoft.com":
            raise ValueError("catalog_uri must use https://onelake.table.fabric.microsoft.com")
        return value.rstrip("/")

    @field_validator("checkpoint_account_url")
    @classmethod
    def _validate_checkpoint_account_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.hostname != "onelake.dfs.fabric.microsoft.com":
            raise ValueError("checkpoint_account_url must use https://onelake.dfs.fabric.microsoft.com")
        return value.rstrip("/")


class SharePointSourceConfig(BaseModel):
    """SharePoint Online document library accessed through Microsoft Graph."""
    site_id: str = Field(min_length=1, max_length=512)
    drive_id: str = Field(min_length=1, max_length=256)
    folder_path: str = Field(default="", max_length=1024)
    file_types: List[str] = Field(
        default_factory=lambda: ["txt", "json", "pdf", "docx", "md", "csv", "html", "xml"],
        max_length=50,
    )
    poll_interval_seconds: int = Field(default=60, ge=10, le=86400)
    max_file_size_bytes: int = Field(default=50 * 1024 * 1024, gt=0, le=50 * 1024 * 1024)
    auth_type: Literal["managed-identity"] = "managed-identity"

    @field_validator("site_id", "drive_id")
    @classmethod
    def strip_required_identifiers(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("folder_path")
    @classmethod
    def normalize_folder_path(cls, value: str) -> str:
        return value.strip().strip("/")

    @field_validator("file_types")
    @classmethod
    def normalize_file_types(cls, values: List[str]) -> List[str]:
        normalized = []
        for value in values:
            extension = value.strip().lower().lstrip(".")
            if not extension or "/" in extension or "\\" in extension:
                raise ValueError("file_types entries must be file extensions")
            if extension not in normalized:
                normalized.append(extension)
        return normalized


# =============================================================================
# DESTINATION CONFIGURATIONS
# =============================================================================

class CosmosDBVectorConfig(BaseModel):
    endpoint: str
    database: str
    container: str
    vector_field: str = "embedding"
    id_field: str = "id"
    metadata_fields: List[str] = ["source", "filename", "content_type"]
    vector_dimensions: int = 1536
    vector_index_type: str = "quantizedFlat"  # flat, quantizedFlat, diskANN


class PgVectorConfig(BaseModel):
    """pgvector destination configuration for storing embeddings."""
    host: str
    port: int = 5432
    database: str
    user: Optional[str] = None  # Use managed identity if not provided
    password: Optional[str] = None
    ssl_mode: str = "require"
    table: str  # Table to write vectors to
    id_column: str = "id"  # Primary key column
    vector_column: str = "embedding"  # Column for vector (type: vector(N))
    content_column: str = "content"  # Column for original text
    metadata_columns: List[str] = ["source_id", "source_ref", "created_at"]
    vector_dimensions: int = 1536
    index_type: str = "hnsw"  # ivfflat, hnsw
    index_lists: int = 100  # For ivfflat: number of lists
    hnsw_m: int = 16  # For hnsw: max connections per layer
    hnsw_ef_construction: int = 64  # For hnsw: size of dynamic candidate list


class OneLakeIcebergDestinationConfig(BaseModel):
    """Fabric Spark write-back destination backed by durable OneLake staging."""
    workspace_id: str
    lakehouse_item_id: str
    spark_job_definition_item_id: str
    staging_account_url: str = "https://onelake.dfs.fabric.microsoft.com"
    staging_file_system: str  # normally the Fabric workspace ID
    staging_path: str = ""
    target_table: str
    fabric_api_base_url: str = "https://api.fabric.microsoft.com/v1"
    spark_executable_file: Optional[str] = None
    writeback_columns: "OneLakeIcebergWritebackColumns" = Field(
        default_factory=lambda: OneLakeIcebergWritebackColumns()
    )
    mirror: Optional["OneLakeIcebergMirrorConfig"] = None

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def _default_staging_path(cls, value: Any) -> Any:
        if isinstance(value, dict) and str(value.get("staging_path", "")).strip("/") in ("", "Files/omnivec/staging"):
            lakehouse_item_id = str(value.get("lakehouse_item_id", "")).strip()
            if lakehouse_item_id:
                value = {**value, "staging_path": f"{lakehouse_item_id}/Files/omnivec/staging"}
        return value

    @field_validator("target_table")
    @classmethod
    def _validate_target_table(cls, value: str) -> str:
        if not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) for part in value.split(".")):
            raise ValueError("target_table must be a dot-qualified SQL identifier")
        return value

    @field_validator("fabric_api_base_url")
    @classmethod
    def _validate_fabric_api_base_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.hostname != "api.fabric.microsoft.com":
            raise ValueError("fabric_api_base_url must use https://api.fabric.microsoft.com")
        return value.rstrip("/")

    @field_validator("staging_account_url")
    @classmethod
    def _validate_staging_account_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.hostname != "onelake.dfs.fabric.microsoft.com":
            raise ValueError("staging_account_url must use https://onelake.dfs.fabric.microsoft.com")
        return value.rstrip("/")

    @field_validator("staging_path")
    @classmethod
    def _validate_staging_path(cls, value: str) -> str:
        normalized = value.strip("/")
        if not normalized or any(part in ("", ".", "..") for part in normalized.split("/")):
            raise ValueError("staging_path must be a safe OneLake-relative path")
        return normalized


class OneLakeIcebergWritebackColumns(BaseModel):
    """Column mapping for updating an existing source row, never inserting one."""
    id_field: str = "id"
    embedding_field: str = "embedding"
    content_hash_field: str = "content_hash"
    pipeline_id_field: str = "pipeline_id"
    pipeline_generation_field: str = "pipeline_generation"
    model_field: str = "embedding_model"
    source_id_field: str = "source_id"
    source_ref_field: str = "source_ref"
    writer_marker_field: str = "omnivec_writer_marker"
    run_id_field: str = "omnivec_run_id"
    embedded_at_field: str = "embedded_at"

    @field_validator("*")
    @classmethod
    def _validate_column_identifier(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError("write-back column names must be simple SQL identifiers")
        return value


class OneLakeIcebergMirrorConfig(BaseModel):
    """Optional immediate serving mirror for OneLake write-back embeddings."""
    type: Literal["garnet", "redis"]
    destination_id: Optional[str] = None
    config: Dict[str, Any]
    best_effort: bool = False

    model_config = {"extra": "forbid"}

    @field_validator("config")
    @classmethod
    def _validate_mirror_config(cls, value: Dict[str, Any], info) -> Dict[str, Any]:
        def config_bool(key: str, default: bool) -> bool:
            raw = value.get(key, default)
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str) and raw.strip().lower() in ("true", "false"):
                return raw.strip().lower() == "true"
            raise ValueError(f"{key} must be a boolean")

        mirror_type = info.data.get("type")
        if mirror_type == "garnet":
            endpoint = str(value.get("endpoint", "")).strip()
            if not endpoint:
                raise ValueError("Garnet mirror requires config.endpoint")
            vector_set = str(value.get("vector_set", "omnivec-vectors")).strip()
            if not re.fullmatch(r"[A-Za-z0-9:_-]{1,256}", vector_set):
                raise ValueError("Garnet vector_set must contain only letters, numbers, ':', '_' or '-'")
            value = {
                **value,
                "endpoint": endpoint,
                "vector_set": vector_set,
                "tls": config_bool("tls", True),
                "use_entra_auth": config_bool("use_entra_auth", False),
            }
        elif mirror_type == "redis":
            endpoint = str(value.get("endpoint", "")).strip()
            if not endpoint:
                raise ValueError("Redis mirror requires config.endpoint")
            value = {
                **value,
                "endpoint": endpoint,
                "tls": config_bool("tls", True),
                "use_entra_auth": config_bool("use_entra_auth", True),
            }
        return value


class ChunkConfig(BaseModel):
    """Configuration for text chunking when content_strategy='chunk'."""
    chunk_size: int = 1000         # Max characters per chunk
    chunk_overlap: int = 200       # Overlap between adjacent chunks
    chunk_unit: str = "chars"      # "chars" or "tokens"
    store_text: bool = False       # Store chunk text in vector docs
    text_field: str = "text"       # Field name for stored text in vector doc
    doc_id_pattern: str = "{source}-chunk-{chunk}"  # Template for chunk doc IDs
                                   # Variables: {source}, {source_ref}, {source_hash}, {chunk}, {pipeline}, {pipeline_hash}




# =============================================================================
# SOURCE & DESTINATION MODELS
# =============================================================================

class Source(BaseModel):
    id: Optional[str] = None
    name: str
    type: SourceType
    config: Dict[str, Any]
    triggers: List[TriggerType] = [TriggerType.MANUAL]
    schedule: Optional[str] = None  # Cron expression
    enabled: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @field_validator("triggers", mode="before")
    @classmethod
    def _normalize_triggers(cls, v):
        # Tolerate legacy persisted form "eventgrid" (no hyphen) so existing
        # documents created before the TriggerType enum was tightened still
        # validate. New writes use the canonical "event-grid".
        if isinstance(v, list):
            return [("event-grid" if t == "eventgrid" else t) for t in v]
        return v


class Destination(BaseModel):
    id: Optional[str] = None
    name: str
    type: DestinationType
    config: Dict[str, Any]
    enabled: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


# =============================================================================
# PIPELINE MODELS
# =============================================================================

class PipelineSource(BaseModel):
    source_id: str
    filters: Dict[str, Any] = {}  # Additional filters like file patterns
    # Content extraction config (how to read content from this source)
    content_fields: List[str] = ["content"]  # Field(s) to concatenate for embedding
    content_mode: str = "field"  # "field" (direct value), "blob_url", "http_url"
    url_content_types: List[str] = ["txt", "json", "pdf"]  # For URL modes
    content_type_field: Optional[str] = None  # Optional: field containing content type hint
    file_types: List[str] = ["txt", "json", "pdf", "docx", "md", "csv"]  # For blob/S3 sources: which file types to process


class Pipeline(BaseModel):
    id: Optional[str] = None
    name: str
    description: Optional[str] = ""
    sources: List[PipelineSource]
    docgrok_pipeline: str  # Name of DocGrok pipeline to use
    destination_id: str
    vector_index_path: str  # Selected from destination's vector indexing policy (e.g. "embedding", "content_vector")
    status: PipelineStatus = PipelineStatus.ACTIVE
    process_existing: bool = True  # Process existing documents on creation
    metadata_mapping: Dict[str, str] = {}  # Map source fields to destination
    processing_mode: str = "queue"  # "queue" = CFP→jobs→worker, "inline" = CFP processes directly
    content_strategy: str = "truncate"  # "truncate" or "chunk"
    chunk_config: Optional[ChunkConfig] = None
    doc_id_pattern: str = "{source}"  # Template for vector doc IDs: {source}, {source_ref}, {source_hash}, {pipeline}, {job}
    # When set, controls whether the (possibly truncated) text actually sent
    # to the embedding model is persisted to the destination alongside the
    # vector. None = per-destination default (Postgres/MsSql write content,
    # Cosmos does not — back-compat). True = always write. False = never write.
    # Only meaningful for queue-mode pipelines; ignored when source and
    # destination point at the same store (inline mode preserves the original
    # document content already).
    store_content: Optional[bool] = None
    # Name of the destination field that receives the embedded text when
    # store_content is true. Cosmos only — for Postgres/MsSql the column is
    # set on the destination as `content_column`. Default "content".
    content_field: str = "content"
    # Optional informational metadata fields to persist on the destination
    # document. None (default) → write all supported optional fields
    # (back-compat). [] → write none of them. List subset → write only those.
    # Allowed values: see ALLOWED_METADATA_FIELDS.
    metadata_fields: Optional[List[str]] = None
    generation: str = "1"  # Incremented on reset - docs with mismatched generation are reprocessed
    reset_at: Optional[datetime] = None  # Set when pipeline is reset for reprocessing
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @field_validator("metadata_fields")
    @classmethod
    def _validate_metadata_fields(cls, v):
        if v is None:
            return v
        bad = [f for f in v if f not in ALLOWED_METADATA_FIELDS]
        if bad:
            raise ValueError(
                f"Unknown metadata_fields {bad}. Allowed: {sorted(ALLOWED_METADATA_FIELDS)}"
            )
        # de-dupe while preserving order
        seen = set()
        return [f for f in v if not (f in seen or seen.add(f))]


# =============================================================================
# JOB MODELS
# =============================================================================

class Job(BaseModel):
    id: Optional[str] = None
    pipeline_id: str
    source_id: str
    source_ref: str  # Blob path, CosmosDB doc ID, etc.
    status: JobStatus = JobStatus.PENDING
    error: Optional[str] = None
    metadata: Dict[str, Any] = {}
    result: Optional[Dict[str, Any]] = None
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    retry_count: int = 0


class JobStats(BaseModel):
    total: int = 0
    pending: int = 0
    processing: int = 0
    completed: int = 0
    failed: int = 0


# =============================================================================
# METRICS MODELS
# =============================================================================

class DailyMetrics(BaseModel):
    processed: int = 0
    failed: int = 0
    processing_time_ms: float = 0.0


class PipelineMetrics(BaseModel):
    processed: int = 0
    failed: int = 0
    processing_time_ms: float = 0.0


class GlobalMetrics(BaseModel):
    events_processed: int = 0
    events_failed: int = 0
    total_processing_time_ms: float = 0.0
    last_updated: Optional[datetime] = None
    daily: Dict[str, DailyMetrics] = {}
    pipelines: Dict[str, PipelineMetrics] = {}


# =============================================================================
# API REQUEST/RESPONSE MODELS
# =============================================================================

class CreateSourceRequest(BaseModel):
    name: str
    type: SourceType
    config: Dict[str, Any]
    triggers: List[TriggerType] = []
    schedule: Optional[str] = None
    enabled: bool = True


class CreateDestinationRequest(BaseModel):
    name: str
    type: DestinationType
    config: Dict[str, Any]
    enabled: bool = True


class CreatePipelineRequest(BaseModel):
    name: str
    description: Optional[str] = ""
    sources: List[PipelineSource]
    docgrok_pipeline: str
    destination_id: str
    vector_index_path: str  # Must match a path in destination's vector indexing policy
    process_existing: bool = True
    metadata_mapping: Dict[str, str] = {}
    processing_mode: str = "queue"  # "queue" or "inline"
    content_strategy: str = "truncate"  # "truncate" or "chunk"
    chunk_config: Optional[Dict[str, Any]] = None
    doc_id_pattern: str = "{source}"  # Template for vector doc IDs: {source}, {source_ref}, {source_hash}, {pipeline}, {job}
    # Optional: persist the embedded text on the destination document.
    # See Pipeline.store_content for full semantics.
    store_content: Optional[bool] = None
    # Override the destination content field name. Cosmos only. Default "content".
    content_field: Optional[str] = None
    # Optional informational metadata fields to persist on destination docs.
    # See Pipeline.metadata_fields for semantics and ALLOWED_METADATA_FIELDS.
    metadata_fields: Optional[List[str]] = None

    @field_validator("metadata_fields")
    @classmethod
    def _validate_metadata_fields(cls, v):
        if v is None:
            return v
        bad = [f for f in v if f not in ALLOWED_METADATA_FIELDS]
        if bad:
            raise ValueError(
                f"Unknown metadata_fields {bad}. Allowed: {sorted(ALLOWED_METADATA_FIELDS)}"
            )
        seen = set()
        return [f for f in v if not (f in seen or seen.add(f))]


class SyncSourceRequest(BaseModel):
    full_sync: bool = False  # If true, reprocess all documents


class PipelineRunStats(BaseModel):
    pipeline_id: str
    pipeline_name: str
    jobs: JobStats
    last_run: Optional[datetime] = None
    documents_processed: int = 0
    source_doc_count: Optional[int] = None  # total docs in source container
    embedded_count: int = 0  # docs embedded with current generation/config
    lifetime_embedded_count: int = 0  # total docs ever embedded for this pipeline (ignores reset_at)
    completion_pct: Optional[float] = None  # embedded_count / source_doc_count * 100
    avg_processing_time_ms: Optional[float] = None
    throughput_docs_per_sec: Optional[float] = None
    recent_throughput_docs_per_sec: Optional[float] = None  # last 60s


# =============================================================================
# ASSISTANT MODELS
# =============================================================================

class ModelCategory(str, Enum):
    EMBEDDING = "embedding"
    CHAT = "chat"


class Assistant(BaseModel):
    id: Optional[str] = None
    name: str
    description: Optional[str] = ""
    model_id: str          # Chat/LLM model ID (mdl-ext-* or mdl-native-*)
    destination_ids: List[str] = []  # Vector indexes to search
    system_prompt: str = ""
    top_k: int = 5
    temperature: float = 0.7
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CreateAssistantRequest(BaseModel):
    name: str
    description: Optional[str] = ""
    model_id: str
    destination_ids: List[str] = []
    system_prompt: str = ""
    top_k: int = 5
    temperature: float = 0.7


class AssistantChatRequest(BaseModel):
    message: str
    conversation: List[Dict[str, str]] = []  # [{"role": "user"|"assistant", "content": "..."}]
