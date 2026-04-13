"""OmniVec Data Models"""

from enum import Enum
from typing import Optional, List, Dict, Any, Union
from pydantic import BaseModel
from datetime import datetime


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


class DestinationType(str, Enum):
    COSMOSDB_VECTOR = "cosmosdb-vector"
    PGVECTOR = "pgvector"
    MSSQL = "mssql"


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
    vector_dimensions: int = 1024
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
    vector_dimensions: int = 1024
    index_type: str = "ivfflat"  # ivfflat, hnsw
    index_lists: int = 100  # For ivfflat: number of lists
    hnsw_m: int = 16  # For hnsw: max connections per layer
    hnsw_ef_construction: int = 64  # For hnsw: size of dynamic candidate list


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
    generation: str = "1"  # Incremented on reset - docs with mismatched generation are reprocessed
    reset_at: Optional[datetime] = None  # Set when pipeline is reset for reprocessing
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


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
