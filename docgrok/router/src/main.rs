use axum::{
    body::{Body, Bytes},
    extract::{DefaultBodyLimit, Json, Path, Query, Request, State},
    http::{header, Method, StatusCode, Uri},
    response::{IntoResponse, Response},
    routing::{delete, get, post, put},
    Router,
};
use dashmap::DashMap;
use reqwest::Client;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{collections::{HashMap, HashSet}, env, sync::Arc, time::Duration};
use tokio::sync::{Mutex, RwLock};
use tower_http::{cors::CorsLayer, compression::CompressionLayer, limit::RequestBodyLimitLayer};
use tracing::{error, info, warn};

// 50 MiB raw input expands to ~66.7 MiB base64, plus JSON metadata.
const MAX_REQUEST_BODY_BYTES: usize = 70 * 1024 * 1024;

fn with_request_body_limits<S: Clone + Send + Sync + 'static>(router: Router<S>) -> Router<S> {
    router
        .layer(DefaultBodyLimit::max(MAX_REQUEST_BODY_BYTES))
        .layer(RequestBodyLimitLayer::new(MAX_REQUEST_BODY_BYTES))
}

// ============================================================================
// Types
// ============================================================================

#[derive(Clone)]
struct AppState {
    /// model_id -> config (e.g. "mdl-ext-fb8c70b0" -> {name, type, endpoint, ...})
    registry: Arc<DashMap<String, Value>>,
    /// pipeline_name -> config
    pipelines: Arc<DashMap<String, Value>>,
    /// native model name -> URL (e.g. "bge-small" -> "http://bge-small-svc:8000")
    native_urls: Arc<HashMap<String, String>>,
    /// Shared HTTP client with connection pooling
    http: Client,
    /// K8s client (None if not in cluster)
    k8s: Option<kube::Client>,
    /// K8s namespace for model deployments
    namespace: String,
    /// CosmosDB config for model persistence
    cosmos: Option<CosmosConfig>,
    /// Pre-serialized mock embedding JSON: "[[0.123,0.456,...]]" (1536 dims)
    mock_1536_single: Arc<String>,
    /// Pre-serialized mock embedding JSON: "[[0.1,0.2,...]]" (128 dims)
    mock_128_single: Arc<String>,
    /// URL to reach the DocGrok controller (router mode only)
    controller_url: String,
    /// URL to reach the pipeline-worker (used to forward blob/data
    /// requests when callers send model_id without going via a routing
    /// pipeline). Defaults to http://pipeline-worker-svc:8080.
    pipeline_worker_url: String,
    /// Model health results from controller health loop
    model_health: Arc<DashMap<String, Value>>,
    /// Timestamp of last health check run
    last_health_check: Arc<RwLock<Option<std::time::Instant>>>,
    metadata_lock: Arc<Mutex<()>>,
}

#[derive(Clone)]
struct CosmosConfig {
    endpoint: String,
    database: String,
    container: String,
    api_key: String,
}

// ============================================================================
// Request / Response types
// ============================================================================

#[derive(Deserialize)]
struct EmbedRequest {
    text: Option<String>,
    model_id: Option<String>,
    pipeline: Option<String>,
    data: Option<String>,
    #[serde(rename = "requestId", default)]
    request_id: String,
    #[serde(rename = "blobUrl")]
    blob_url: Option<String>,
    blob_name: Option<String>,
    blob_container: Option<String>,
    blob_account_url: Option<String>,
    blob_connection_string: Option<String>,
    #[serde(rename = "contentTypeHint")]
    content_type_hint: Option<String>,
    transform_name: Option<String>,
    transform: Option<Value>,
    expected_dim: Option<i64>,
}

#[derive(Deserialize)]
struct EmbedBatchRequest {
    texts: Vec<String>,
    model_id: Option<String>,
    pipeline: Option<String>,
}

#[derive(Deserialize)]
struct RegisterModelRequest {
    #[serde(default)]
    id: Option<String>,
    name: String,
    #[serde(rename = "type")]
    model_type: String,
    endpoint: String,
    #[serde(default)]
    deployment: String,
    #[serde(default)]
    api_key: String,
    #[serde(default)]
    auth_type: Option<String>,
    #[serde(default)]
    client_id: Option<String>,
    #[serde(default)]
    api_version: String,
    #[serde(default)]
    embedding_dim: u32,
}

#[derive(Deserialize)]
struct ScaleRequest {
    replicas: i32,
}

#[derive(Deserialize)]
struct PipelineRequest {
    #[serde(flatten)]
    config: Value,
}

// ============================================================================
// Error type
// ============================================================================

struct AppError(StatusCode, String);

impl IntoResponse for AppError {
    fn into_response(self) -> axum::response::Response {
        let body = json!({"detail": self.1});
        (self.0, Json(body)).into_response()
    }
}

impl From<reqwest::Error> for AppError {
    fn from(e: reqwest::Error) -> Self {
        AppError(StatusCode::BAD_GATEWAY, format!("Backend error: {e}"))
    }
}

// ============================================================================
// Main
// ============================================================================

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "docgrok_router=info".into()),
        )
        .init();

    let mode = env::var("DOCGROK_MODE").unwrap_or_else(|_| "router".into());
    let port: u16 = env::var("PORT").ok().and_then(|p| p.parse().ok()).unwrap_or(
        if mode == "controller" { 8081 } else { 8080 }
    );

    info!("DocGrok starting in {mode} mode on port {port}");

    // Build HTTP client with generous connection pool
    let http = Client::builder()
        .pool_max_idle_per_host(100)
        .pool_idle_timeout(Duration::from_secs(90))
        .connect_timeout(Duration::from_secs(10))
        .timeout(Duration::from_secs(300))
        .build()
        .expect("Failed to build HTTP client");

    // Native model URLs
    let native_urls: HashMap<String, String> = [
        ("dse-qwen2", env::var("DSE_QWEN2_URL").unwrap_or_else(|_| "http://dse-qwen2-svc:8000".into())),
        ("clip", env::var("CLIP_URL").unwrap_or_else(|_| "http://clip-svc:8000".into())),
        ("bge", env::var("BGE_URL").unwrap_or_else(|_| "http://bge-svc:8000".into())),
        ("bge-small", env::var("BGE_SMALL_URL").unwrap_or_else(|_| "http://bge-small-svc:8000".into())),
    ]
    .into_iter()
    .map(|(k, v)| (k.to_string(), v))
    .collect();

    // K8s client
    let k8s = match kube::Client::try_default().await {
        Ok(c) => {
            info!("K8s client initialized");
            Some(c)
        }
        Err(e) => {
            warn!("K8s client not available: {e}");
            None
        }
    };

    let namespace = env::var("NAMESPACE").unwrap_or_else(|_| "docgrok".into());
    let controller_url = env::var("DOCGROK_CONTROLLER_URL")
        .unwrap_or_else(|_| "http://docgrok-controller:8081".into());
    let pipeline_worker_url = env::var("PIPELINE_WORKER_URL")
        .unwrap_or_else(|_| "http://pipeline-worker-svc:8080".into());
    info!("Pipeline worker URL: {pipeline_worker_url}");

    // CosmosDB config for model persistence
    let cosmos = match (
        env::var("COSMOS_ENDPOINT"),
        env::var("COSMOS_DATABASE"),
        env::var("COSMOS_CONTAINER"),
    ) {
        (Ok(endpoint), Ok(database), Ok(container)) => {
            let api_key = env::var("COSMOS_KEY").unwrap_or_default();
            info!("CosmosDB model store: {endpoint}/{database}/{container}");
            Some(CosmosConfig { endpoint, database, container, api_key })
        }
        _ if ["COSMOS_ENDPOINT", "COSMOS_DATABASE", "COSMOS_CONTAINER"]
            .iter().any(|key| env::var_os(key).is_some()) => {
            error!("Incomplete Cosmos registry configuration: endpoint, database and container are all required");
            std::process::exit(1);
        }
        _ => {
            warn!("CosmosDB config not set, model persistence disabled");
            None
        }
    };

    let registry = Arc::new(DashMap::new());
    let pipelines = Arc::new(DashMap::new());
    let model_health: Arc<DashMap<String, Value>> = Arc::new(DashMap::new());
    let last_health_check: Arc<RwLock<Option<std::time::Instant>>> = Arc::new(RwLock::new(None));

    // Pre-compute mock embedding vectors once at startup (avoids per-request alloc + RNG + serialization)
    let mock_1536_vec: Vec<f64> = (0..1536).map(|i| (i as f64 * 0.001).sin()).collect();
    let mock_1536_single = Arc::new(serde_json::to_string(&json!([mock_1536_vec])).unwrap());
    let mock_128_vec: Vec<f64> = (0..128).map(|i| (i as f64 * 0.01).sin()).collect();
    let mock_128_single = Arc::new(serde_json::to_string(&json!([mock_128_vec])).unwrap());
    info!("Pre-computed mock embeddings: 1536-dim ({}B), 128-dim ({}B)", mock_1536_single.len(), mock_128_single.len());

    let state = AppState {
        registry: registry.clone(),
        pipelines: pipelines.clone(),
        native_urls: Arc::new(native_urls),
        http: http.clone(),
        k8s,
        namespace,
        cosmos,
        mock_1536_single,
        mock_128_single,
        controller_url,
        pipeline_worker_url,
        model_health,
        last_health_check,
        metadata_lock: Arc::new(Mutex::new(())),
    };

    if let Err(e) = initialize_registry(&state).await {
        error!("Cannot start with an unavailable model registry: {e}");
        std::process::exit(1);
    }
    if state.cosmos.is_some() {
        let registry_state = state.clone();
        tokio::spawn(async move {
            loop {
                tokio::time::sleep(Duration::from_secs(15)).await;
                if let Err(e) = load_models_from_cosmos(&registry_state).await {
                    error!("Model registry refresh failed; retaining last successful state: {e}");
                }
                if let Err(e) = load_pipelines_from_cosmos(&registry_state).await {
                    error!("Pipeline registry refresh failed; retaining last successful state: {e}");
                }
            }
        });
    }

    if mode == "controller" {
        // ── Controller mode ──────────────────────────────────────────────
        // Spawn background health check loop
        let health_state = state.clone();
        tokio::spawn(async move {
            let interval = Duration::from_secs(
                env::var("HEALTH_CHECK_INTERVAL").ok().and_then(|v| v.parse().ok()).unwrap_or(60)
            );
            info!("Controller health loop starting (interval: {}s)", interval.as_secs());
            loop {
                run_model_health_checks(&health_state).await;
                tokio::time::sleep(interval).await;
            }
        });

        // Lightweight HTTP server for controller endpoints
        let app = Router::new()
            .route("/health", get(controller_health))
            .route("/admin/health/models", get(get_model_health))
            .layer(CorsLayer::permissive())
            .with_state(state);

        let listener = tokio::net::TcpListener::bind(format!("0.0.0.0:{port}"))
            .await
            .expect("Failed to bind");
        info!("DocGrok Controller listening on 0.0.0.0:{port}");
        axum::serve(listener, app).await.unwrap();
    } else {
        // ── Router mode ──────────────────────────────────────────────────
        let app = Router::new()
            // Hot path — embed
            .route("/embed", post(handle_embed))
            .route("/embed/batch", post(handle_embed_batch))
            // Health
            .route("/health", get(handle_health))
            // Admin — model registry
            .route(
                "/admin/models/registry",
                get(list_registry_models).post(register_model),
            )
            .route(
                "/admin/models/registry/{model_id}",
                get(get_registry_model).delete(delete_registry_model),
            )
            .route(
                "/admin/models/registry/{model_id}/chat",
                post(chat_via_model),
            )
            .route(
                "/admin/models/registry/{model_id}/healthcheck",
                post(healthcheck_model),
            )
            // Admin — K8s models
            .route("/admin/models", get(list_k8s_models))
            .route("/admin/models/{name}/scale", post(scale_model))
            .route("/admin/models/{name}/enable", post(enable_model))
            .route("/admin/models/{name}/disable", post(disable_model))
            .route("/admin/models/{name}/restart", post(restart_model))
            .route("/admin/logs/{name}", get(get_logs))
            // Admin — system
            .route("/admin/system", get(system_info))
            // Admin — deployments (queries K8s for DocGrok-related deployments)
            .route("/admin/deployments", get(list_deployments))
            .route("/admin/deployments/{name}/scale", post(scale_model))
            .route("/admin/deployments/{name}/restart", post(restart_model))
            // Admin — model health (proxies to controller)
            .route("/admin/health/models", get(proxy_model_health))
            // Admin — pipelines
            .route(
                "/admin/pipelines",
                get(list_pipelines).post(create_pipeline),
            )
            .route("/admin/pipelines/options", get(pipeline_options))
            .route(
                "/admin/pipelines/{name}",
                get(get_pipeline).put(update_pipeline).delete(delete_pipeline),
            )
            .route("/admin/pipelines/{name}/reset", post(reset_pipeline))
            // Pipeline-worker proxy — keeps "all requests go through the
            // router" invariant. The pipeline-worker is not exposed directly.
            .route(
                "/transforms",
                get(proxy_pipeline_worker).post(proxy_pipeline_worker),
            )
            .route(
                "/transforms/{*rest}",
                get(proxy_pipeline_worker)
                    .post(proxy_pipeline_worker)
                    .put(proxy_pipeline_worker)
                    .delete(proxy_pipeline_worker),
            )
            .route("/pipeline/recipe", get(proxy_pipeline_worker))
            .route("/pipeline/stages/catalog", get(proxy_pipeline_worker))
            .route("/process", post(proxy_pipeline_worker))
            .route("/process/blob", post(proxy_pipeline_worker))
            .layer(CompressionLayer::new().gzip(true))
            .layer(CorsLayer::permissive())
            .with_state(state);
        let app = with_request_body_limits(app);

        let listener = tokio::net::TcpListener::bind(format!("0.0.0.0:{port}"))
            .await
            .expect("Failed to bind");
        info!("DocGrok Router listening on 0.0.0.0:{port}");
        axum::serve(listener, app).await.unwrap();
    }
}

// ============================================================================
// CosmosDB model persistence (using REST API with key auth)
// ============================================================================

async fn cosmos_query(
    state: &AppState,
    query: &str,
    parameters: &[(&str, &str)],
) -> Result<Vec<Value>, String> {
    let cosmos = state.cosmos.as_ref().ok_or("No CosmosDB config")?;
    let url = format!(
        "{}/dbs/{}/colls/{}/docs",
        cosmos.endpoint, cosmos.database, cosmos.container
    );

    let mut params = Vec::new();
    for (name, value) in parameters {
        params.push(json!({"name": name, "value": value}));
    }

    let body = json!({
        "query": query,
        "parameters": params,
    });

    // Use AAD token (managed identity)
    let token = get_cosmos_token(state).await?;

    let mut documents = Vec::new();
    let mut continuation: Option<String> = None;
    let mut seen = HashSet::new();
    loop {
        let mut request = state.http.post(&url)
            .timeout(Duration::from_secs(30))
            .header("Authorization", format!("type=aad&ver=1.0&sig={token}"))
            .header("Content-Type", "application/query+json")
            .header("x-ms-version", "2020-07-15")
            .header("x-ms-documentdb-isquery", "true")
            .header("x-ms-documentdb-query-enablecrosspartition", "true")
            .json(&body);
        if let Some(value) = &continuation {
            request = request.header("x-ms-continuation", value);
        }
        let resp = request.send().await
            .map_err(|e| format!("CosmosDB request failed: {e}"))?;
        if !resp.status().is_success() {
            let status = resp.status();
            let text = resp.text().await.unwrap_or_default();
            return Err(format!("CosmosDB query failed ({status}): {text}"));
        }
        continuation = resp.headers().get("x-ms-continuation")
            .map(|value| value.to_str().map(str::to_owned))
            .transpose().map_err(|e| format!("Invalid Cosmos continuation header: {e}"))?
            .filter(|value| !value.is_empty());
        let data: Value = resp.json().await
            .map_err(|e| format!("Failed to parse CosmosDB response: {e}"))?;
        documents.extend(data["Documents"].as_array()
            .ok_or("CosmosDB query response is missing Documents")?.iter().cloned());
        match &continuation {
            None => return Ok(documents),
            Some(value) if !seen.insert(value.clone()) =>
                return Err("CosmosDB query repeated a continuation token".into()),
            _ => {}
        }
    }
}

async fn cosmos_upsert(state: &AppState, doc: &Value) -> Result<(), String> {
    cosmos_write(state, doc, RegistryWrite::Upsert).await
}

enum RegistryWrite {
    Upsert,
    Create,
    Replace(String),
}

async fn cosmos_write(state: &AppState, doc: &Value, mode: RegistryWrite) -> Result<(), String> {
    let cosmos = state.cosmos.as_ref().ok_or("No CosmosDB config")?;
    let url = format!(
        "{}/dbs/{}/colls/{}/docs",
        cosmos.endpoint, cosmos.database, cosmos.container
    );

    let token = get_cosmos_token(state).await?;
    let doc_type = doc["doc_type"].as_str().unwrap_or("unknown");

    let request = match mode {
        RegistryWrite::Upsert => state.http.post(&url).header("x-ms-documentdb-is-upsert", "true"),
        RegistryWrite::Create => state.http.post(&url),
        RegistryWrite::Replace(etag) => {
            let id = doc["id"].as_str().ok_or("Registry document requires an id")?;
            let mut item_url = reqwest::Url::parse(&url).map_err(|e| e.to_string())?;
            item_url.path_segments_mut().map_err(|_| "Invalid CosmosDB URL")?.push(id);
            state.http.put(item_url).header("If-Match", etag)
        }
    };
    let resp = request
        .timeout(Duration::from_secs(30))
        .header("Authorization", format!("type=aad&ver=1.0&sig={token}"))
        .header("Content-Type", "application/json")
        .header("x-ms-version", "2020-07-15")
        .header("x-ms-documentdb-partitionkey", format!("[\"{doc_type}\"]"))
        .json(doc)
        .send()
        .await
        .map_err(|e| format!("CosmosDB write failed: {e}"))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let text = resp.text().await.unwrap_or_default();
        return Err(format!("CosmosDB write failed ({status}): {text}"));
    }
    Ok(())
}

async fn cosmos_delete(state: &AppState, id: &str, doc_type: &str) -> Result<(), String> {
    let cosmos = state.cosmos.as_ref().ok_or("No CosmosDB config")?;
    let url = format!(
        "{}/dbs/{}/colls/{}/docs/{id}",
        cosmos.endpoint, cosmos.database, cosmos.container
    );

    let token = get_cosmos_token(state).await?;

    let resp = state
        .http
        .delete(&url)
        .timeout(Duration::from_secs(30))
        .header("Authorization", format!("type=aad&ver=1.0&sig={token}"))
        .header("x-ms-version", "2020-07-15")
        .header("x-ms-documentdb-partitionkey", format!("[\"{doc_type}\"]"))
        .send()
        .await
        .map_err(|e| format!("CosmosDB delete failed: {e}"))?;

    if !resp.status().is_success() && resp.status().as_u16() != 404 {
        let status = resp.status();
        let text = resp.text().await.unwrap_or_default();
        return Err(format!("CosmosDB delete failed ({status}): {text}"));
    }
    Ok(())
}

/// AAD token cache
static TOKEN_CACHE: std::sync::OnceLock<RwLock<(String, std::time::Instant)>> =
    std::sync::OnceLock::new();

async fn get_cosmos_token(state: &AppState) -> Result<String, String> {
    let cache = TOKEN_CACHE.get_or_init(|| RwLock::new((String::new(), std::time::Instant::now())));

    // Check cache (tokens valid for ~1 hour, refresh after 50 min)
    {
        let cached = cache.read().await;
        if !cached.0.is_empty() && cached.1.elapsed() < Duration::from_secs(3000) {
            return Ok(cached.0.clone());
        }
    }

    // Get fresh token via IMDS (managed identity)
    let cosmos = state.cosmos.as_ref().ok_or("No CosmosDB config")?;

    // Extract account name from endpoint for resource scope
    let resource = "https://cosmos.azure.com";

    let token = fetch_aad_token(&state.http, resource).await?;

    // Cache it
    let mut cached = cache.write().await;
    *cached = (token.clone(), std::time::Instant::now());

    Ok(token)
}

/// AAD token for Azure OpenAI (cognitive services scope)
static AOAI_TOKEN_CACHE: std::sync::OnceLock<RwLock<(String, std::time::Instant)>> =
    std::sync::OnceLock::new();

async fn get_aoai_token(http: &Client) -> Result<String, String> {
    let cache =
        AOAI_TOKEN_CACHE.get_or_init(|| RwLock::new((String::new(), std::time::Instant::now())));

    {
        let cached = cache.read().await;
        if !cached.0.is_empty() && cached.1.elapsed() < Duration::from_secs(3000) {
            return Ok(cached.0.clone());
        }
    }

    let token = fetch_aad_token(http, "https://cognitiveservices.azure.com").await?;

    let mut cached = cache.write().await;
    *cached = (token.clone(), std::time::Instant::now());
    Ok(token)
}

async fn fetch_aad_token(http: &Client, resource: &str) -> Result<String, String> {
    // Try workload identity first (AKS)
    if let (Ok(authority), Ok(token_file), Ok(client_id)) = (
        env::var("AZURE_AUTHORITY_HOST"),
        env::var("AZURE_FEDERATED_TOKEN_FILE"),
        env::var("AZURE_CLIENT_ID"),
    ) {
        let tenant = env::var("AZURE_TENANT_ID").unwrap_or_default();
        let federated_token = tokio::fs::read_to_string(&token_file)
            .await
            .map_err(|e| format!("Cannot read federated token: {e}"))?;

        let authority = authority.trim_end_matches('/');
        let url = format!("{authority}/{tenant}/oauth2/token");
        let resp = http
            .post(&url)
            .form(&[
                ("grant_type", "client_credentials"),
                (
                    "client_assertion_type",
                    "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                ),
                ("client_assertion", &federated_token),
                ("client_id", &client_id),
                ("resource", resource),
            ])
            .send()
            .await
            .map_err(|e| format!("Token exchange failed: {e}"))?;

        if resp.status().is_success() {
            let body: Value = resp.json().await.map_err(|e| format!("Parse token: {e}"))?;
            return body["access_token"]
                .as_str()
                .map(|s| s.to_string())
                .ok_or_else(|| "No access_token in response".into());
        } else {
            let status = resp.status();
            let body = resp.text().await.unwrap_or_default();
            warn!("Workload identity token exchange failed ({status}): {body}; falling back to IMDS");
        }
    }

    // Fall back to IMDS (VM managed identity)
    let url = format!(
        "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource={resource}"
    );
    let resp = http
        .get(&url)
        .header("Metadata", "true")
        .send()
        .await
        .map_err(|e| format!("IMDS token request failed: {e}"))?;

    if !resp.status().is_success() {
        return Err(format!("IMDS returned {}", resp.status()));
    }

    let body: Value = resp.json().await.map_err(|e| format!("Parse IMDS: {e}"))?;
    body["access_token"]
        .as_str()
        .map(|s| s.to_string())
        .ok_or_else(|| "No access_token in IMDS response".into())
}

async fn load_models_from_cosmos(state: &AppState) -> Result<(), String> {
    load_registry_from_cosmos(state, "docgrok_model", &state.registry).await
}

async fn load_pipelines_from_cosmos(state: &AppState) -> Result<(), String> {
    load_registry_from_cosmos(state, "docgrok_pipeline", &state.pipelines).await
}

async fn load_registry_from_cosmos(
    state: &AppState, doc_type: &str, cache: &DashMap<String, Value>,
) -> Result<(), String> {
    if state.cosmos.is_none() {
        return Ok(());
    }
    let _guard = state.metadata_lock.lock().await;
    let docs = cosmos_query(state, "SELECT * FROM c WHERE c.doc_type = @type", &[("@type", doc_type)]).await?;
    let snapshot: HashMap<String, Value> = docs.into_iter().map(|doc| {
        let id = doc["id"].as_str().filter(|id| !id.is_empty())
            .ok_or_else(|| format!("Invalid {doc_type} document: missing id"))?.to_owned();
        Ok((id, doc))
    }).collect::<Result<_, String>>()?;
    let ids: HashSet<String> = snapshot.keys().cloned().collect();
    for (id, doc) in snapshot {
        cache.insert(id, doc);
    }
    // Publish only a complete successful snapshot; never clear a working cache
    // before all pages have arrived or expose a transient empty registry.
    cache.retain(|id, _| ids.contains(id));
    Ok(())
}

async fn initialize_registry(state: &AppState) -> Result<(), String> {
    for attempt in 1..=5 {
        let result = async {
            load_models_from_cosmos(state).await?;
            load_pipelines_from_cosmos(state).await
        }.await;
        match result {
            Ok(()) => return Ok(()),
            Err(e) if attempt == 5 => return Err(e),
            Err(e) => warn!("Registry startup load failed (attempt {attempt}/5): {e}"),
        }
        tokio::time::sleep(Duration::from_secs(5)).await;
    }
    unreachable!()
}

fn registry_unavailable(error: String) -> AppError {
    error!("Registry operation failed: {error}");
    AppError(StatusCode::SERVICE_UNAVAILABLE, "Model/pipeline registry storage is unavailable; retry the operation".into())
}

async fn save_registry_document(
    state: &AppState, cache: &DashMap<String, Value>, doc: &Value,
) -> Result<(), AppError> {
    let id = doc["id"].as_str()
        .ok_or_else(|| AppError(StatusCode::BAD_REQUEST, "Registry document requires an id".into()))?;
    let _guard = state.metadata_lock.lock().await;
    if state.cosmos.is_some() {
        cosmos_upsert(state, doc).await.map_err(registry_unavailable)?;
    }
    cache.insert(id.to_owned(), doc.clone());
    Ok(())
}

async fn remove_registry_document(
    state: &AppState, cache: &DashMap<String, Value>, id: &str, doc_type: &str,
) -> Result<(), AppError> {
    let _guard = state.metadata_lock.lock().await;
    if !cache.contains_key(id) {
        return Err(AppError(StatusCode::NOT_FOUND, format!("Registry entry '{id}' not found")));
    }
    if state.cosmos.is_some() {
        cosmos_delete(state, id, doc_type).await.map_err(registry_unavailable)?;
    }
    cache.remove(id);
    Ok(())
}

// ============================================================================
// Embed hot path
// ============================================================================

async fn call_model(
    state: &AppState,
    model_id: &str,
    text: TextInput,
) -> Result<Value, AppError> {
    if model_id.starts_with("mdl-native-") {
        let name = &model_id["mdl-native-".len()..];
        let url = state
            .native_urls
            .get(name)
            .ok_or_else(|| AppError(StatusCode::BAD_REQUEST, format!("Unknown native model: '{name}'")))?;

        match &text {
            TextInput::Single(t) => {
                let payload = json!({"text": t, "model_id": model_id});
                let resp = state
                    .http
                    .post(format!("{url}/embed"))
                    .json(&payload)
                    .send()
                    .await?;
                let result: Value = resp.json().await?;
                let pages = result.get("pages").or_else(|| result.get("embeddings"));
                Ok(json!({
                    "model_id": model_id,
                    "pages": pages,
                    "model": {"name": name},
                }))
            }
            TextInput::Batch(texts) => {
                // Try batch endpoint first
                let payload = json!({"texts": texts, "model_id": model_id});
                let resp = state
                    .http
                    .post(format!("{url}/embed/batch"))
                    .json(&payload)
                    .send()
                    .await;

                match resp {
                    Ok(r) if r.status().is_success() => {
                        let r: Value = r.json().await?;
                        Ok(json!({
                            "embeddings": r.get("embeddings").unwrap_or(&json!([])),
                            "model_id": model_id,
                        }))
                    }
                    _ => {
                        // Fallback: call one at a time
                        let mut results = Vec::with_capacity(texts.len());
                        for t in texts {
                            let payload = json!({"text": t});
                            let resp = state
                                .http
                                .post(format!("{url}/embed"))
                                .json(&payload)
                                .send()
                                .await?;
                            let r: Value = resp.json().await?;
                            let embedding = r
                                .get("pages")
                                .or_else(|| r.get("embeddings"))
                                .and_then(|v| v.as_array())
                                .and_then(|a| a.first())
                                .cloned()
                                .unwrap_or(json!([]));
                            results.push(embedding);
                        }
                        Ok(json!({
                            "embeddings": results,
                            "model_id": model_id,
                        }))
                    }
                }
            }
        }
    } else if model_id.starts_with("mdl-ext-") {
        if !state.registry.contains_key(model_id) {
            load_models_from_cosmos(state).await.map_err(registry_unavailable)?;
        }
        let cfg = state
            .registry
            .get(model_id)
            .ok_or_else(|| {
                AppError(
                    StatusCode::NOT_FOUND,
                    format!("Model '{model_id}' not found in registry"),
                )
            })?
            .value()
            .clone();

        let model_type = cfg["type"].as_str().unwrap_or("");
        let endpoint = cfg["endpoint"]
            .as_str()
            .ok_or_else(|| AppError(StatusCode::INTERNAL_SERVER_ERROR, "Missing endpoint".into()))?;
        let deployment = cfg["deployment"]
            .as_str()
            .or_else(|| cfg["name"].as_str())
            .unwrap_or("");
        let api_version = cfg["api_version"].as_str().unwrap_or("2024-06-01");
        let api_key = cfg["api_key"].as_str().unwrap_or("");
        let embedding_dim = cfg["embedding_dim"].as_u64().unwrap_or(0);
        let name = cfg["name"].as_str().unwrap_or(model_id);

        let input_data = match &text {
            TextInput::Single(t) => json!([t]),
            TextInput::Batch(ts) => json!(ts),
        };

        let (url, headers) = match model_type {
            "azure-openai" => {
                let url = format!(
                    "{endpoint}/openai/deployments/{deployment}/embeddings?api-version={api_version}"
                );
                let mut h = reqwest::header::HeaderMap::new();
                if !api_key.is_empty() {
                    h.insert("api-key", api_key.parse().unwrap());
                } else {
                    // Use managed identity token
                    let token = get_aoai_token(&state.http).await.map_err(|e| {
                        AppError(
                            StatusCode::INTERNAL_SERVER_ERROR,
                            format!("Failed to get AOAI token: {e}"),
                        )
                    })?;
                    h.insert(
                        "Authorization",
                        format!("Bearer {token}").parse().unwrap(),
                    );
                }
                (url, h)
            }
            "openai" => {
                let url = format!("{endpoint}/embeddings");
                let mut h = reqwest::header::HeaderMap::new();
                h.insert(
                    "Authorization",
                    format!("Bearer {api_key}").parse().unwrap(),
                );
                (url, h)
            }
            other => {
                return Err(AppError(
                    StatusCode::BAD_REQUEST,
                    format!("Unsupported model type: {other}"),
                ));
            }
        };

        let mut payload = json!({"input": input_data});
        if model_type != "azure-openai" {
            payload["model"] = json!(name);
        }

        let resp = state
            .http
            .post(&url)
            .headers(headers)
            .json(&payload)
            .timeout(Duration::from_secs(120))
            .send()
            .await?;

        if !resp.status().is_success() {
            let status = resp.status();
            let text = resp.text().await.unwrap_or_default();
            return Err(AppError(
                StatusCode::from_u16(status.as_u16()).unwrap_or(StatusCode::BAD_GATEWAY),
                format!("External API error: {text}"),
            ));
        }

        let result: Value = resp.json().await?;
        let embeddings: Vec<Value> = result["data"]
            .as_array()
            .map(|arr| arr.iter().map(|item| item["embedding"].clone()).collect())
            .unwrap_or_default();
        let usage = result.get("usage").cloned().unwrap_or(json!({}));

        match &text {
            TextInput::Batch(_) => Ok(json!({
                "embeddings": embeddings,
                "model_id": model_id,
                "usage": usage,
            })),
            TextInput::Single(_) => Ok(json!({
                "model_id": model_id,
                "pages": embeddings,
                "model": {
                    "name": name,
                    "deployment": deployment,
                    "embeddingDim": embedding_dim,
                },
                "usage": usage,
            })),
        }
    } else {
        Err(AppError(
            StatusCode::BAD_REQUEST,
            format!("Invalid model ID format: '{model_id}'"),
        ))
    }
}

enum TextInput {
    Single(String),
    Batch(Vec<String>),
}

fn pipeline_model_id(config: &Value) -> Option<&str> {
    config["model_id"].as_str()
        .or_else(|| config["model"].as_str())
        .or_else(|| {
            config["steps"].as_array().and_then(|steps| {
                steps.iter().find_map(|step| {
                    if step["type"].as_str() == Some("model") {
                        step["model_id"].as_str().or_else(|| step["model"].as_str())
                    } else {
                        None
                    }
                })
            })
        })
        .or_else(|| config["steps"][0]["model"].as_str())
}

fn request_model_id(req: &EmbedRequest, pipelines: &DashMap<String, Value>) -> Option<String> {
    req.model_id.clone().or_else(|| {
        req.pipeline.as_ref().and_then(|pipeline| {
            pipelines.get(pipeline)
                .and_then(|config| pipeline_model_id(&config).map(str::to_owned))
        })
    })
}

async fn handle_embed(
    State(state): State<AppState>,
    Json(req): Json<EmbedRequest>,
) -> Result<impl IntoResponse, AppError> {
    if req.model_id.is_none() && req.pipeline.as_ref().is_some_and(|id| !state.pipelines.contains_key(id)) {
        load_pipelines_from_cosmos(&state).await.map_err(registry_unavailable)?;
    }
    let resolved_model_id = request_model_id(&req, &state.pipelines);
    // Blob/data requests always go through the pipeline-worker so that
    // its transform dispatcher can pick the right per-blob recipe (pdf,
    // text, image, video, ...) by extension. The model_id (if any) is
    // passed through for transforms whose `embed` stage needs it; image
    // and video transforms ignore it.
    let has_blob_or_data = req.blob_url.is_some()
        || req.blob_name.is_some()
        || req.data.is_some();
    if has_blob_or_data {
        let body = serde_json::json!({
            "data": req.data,
            "text": req.text,
            "pipeline": req.pipeline,
            "requestId": req.request_id,
            "model_id": resolved_model_id,
            "blob_url": req.blob_url,
            "blob_name": req.blob_name,
            "blob_container": req.blob_container,
            "blob_account_url": req.blob_account_url,
            "blob_connection_string": req.blob_connection_string,
            "transform_name": req.transform_name,
            "transform": req.transform,
            "expected_dim": req.expected_dim,
        });
        let url = format!("{}/process", state.pipeline_worker_url.trim_end_matches('/'));
        let resp = state
            .http
            .post(&url)
            .json(&body)
            .send()
            .await
            .map_err(|e| AppError(StatusCode::BAD_GATEWAY, format!("Pipeline worker error: {e}")))?;
        if !resp.status().is_success() {
            let status = resp.status();
            let body_text = resp.text().await.unwrap_or_default();
            return Err(AppError(
                StatusCode::from_u16(status.as_u16()).unwrap_or(StatusCode::BAD_GATEWAY),
                format!("Pipeline worker returned {status}: {body_text}"),
            ));
        }
        let mut result: Value = resp
            .json()
            .await
            .map_err(|e| AppError(StatusCode::BAD_GATEWAY, format!("Pipeline worker response error: {e}")))?;
        result["_routed_to"] = serde_json::json!("pipeline-worker");
        return Ok(Json(result));
    }

    // Transform-driven text routing (e.g. CLIP text encoder for image
    // indexes). When the caller specifies a transform_name/transform but
    // only sends `text`, forward to the pipeline-worker so that the
    // transform's embed stage (e.g. image_embed text mode) handles it.
    let has_transform = req.transform_name.is_some() || req.transform.is_some();
    if has_transform && req.text.is_some() {
        let body = serde_json::json!({
            "text": req.text,
            "pipeline": req.pipeline,
            "requestId": req.request_id,
            "model_id": resolved_model_id,
            "transform_name": req.transform_name,
            "transform": req.transform,
            "expected_dim": req.expected_dim,
        });
        let url = format!("{}/process", state.pipeline_worker_url.trim_end_matches('/'));
        let resp = state
            .http
            .post(&url)
            .json(&body)
            .send()
            .await
            .map_err(|e| AppError(StatusCode::BAD_GATEWAY, format!("Pipeline worker error: {e}")))?;
        if !resp.status().is_success() {
            let status = resp.status();
            let body_text = resp.text().await.unwrap_or_default();
            return Err(AppError(
                StatusCode::from_u16(status.as_u16()).unwrap_or(StatusCode::BAD_GATEWAY),
                format!("Pipeline worker returned {status}: {body_text}"),
            ));
        }
        let mut result: Value = resp
            .json()
            .await
            .map_err(|e| AppError(StatusCode::BAD_GATEWAY, format!("Pipeline worker response error: {e}")))?;
        result["_routed_to"] = serde_json::json!("pipeline-worker");
        return Ok(Json(result));
    }

    // Model ID routing (text-only)
    if let Some(model_id) = &req.model_id {
        let text = req
            .text
            .as_ref()
            .ok_or_else(|| AppError(StatusCode::BAD_REQUEST, "'text' field is required".into()))?;
        let mut result = call_model(&state, model_id, TextInput::Single(text.clone())).await?;
        result["_routed_to"] = json!(model_id);
        result["requestId"] = json!(req.request_id);
        return Ok(Json(result));
    }

    // Mock pipeline routing
    if let Some(pipeline) = &req.pipeline {
        if pipeline == "mock-embedding" {
            let dim: usize = env::var("MOCK_EMBEDDING_DIM")
                .ok()
                .and_then(|v| v.parse().ok())
                .unwrap_or(128);
            let vec: Vec<f64> = (0..dim).map(|_| rand::random_range(0.0..1.0f64)).collect();
            return Ok(Json(json!({
                "output": [vec],
                "requestId": req.request_id,
                "pipeline": "mock-embedding",
                "model": {"name": "mock", "embeddingDim": dim},
            })));
        }
        if pipeline == "mock-1536" {
            let vec: Vec<f64> = (0..1536).map(|_| rand::random_range(0.0..1.0f64)).collect();
            return Ok(Json(json!({
                "output": [vec],
                "requestId": req.request_id,
                "pipeline": "mock-1536",
                "model": {"name": "mock-1536", "embeddingDim": 1536},
            })));
        }

        // Real pipeline — look up config
        let all_keys: Vec<String> = state.pipelines.iter().map(|e| e.key().clone()).collect();
        info!("Looking for pipeline '{}', available pipelines: {:?}", pipeline, all_keys);
        let pipeline_cfg = state.pipelines.get(pipeline).map(|v| v.value().clone());
        if let Some(cfg) = pipeline_cfg {
            // Resolve the embedding model from pipeline config or steps
            let pipeline_model_id = pipeline_model_id(&cfg);

            // Text-only request: call the embedding model directly (skip pipeline worker)
            if req.text.is_some() && req.data.is_none() && req.blob_url.is_none() {
                if let Some(model) = pipeline_model_id {
                    let text = req.text.unwrap();
                    let mut result = call_model(&state, model, TextInput::Single(text)).await?;
                    result["_routed_to"] = json!(model);
                    result["_resolved_from_pipeline"] = json!(pipeline.as_str());
                    result["requestId"] = json!(req.request_id);
                    return Ok(Json(result));
                }
            }

            // Route A: Pipeline has worker_url → forward full request to pipeline worker
            if let Some(worker_url) = cfg["worker_url"].as_str() {
                let body = serde_json::json!({
                    "data": req.data,
                    "text": req.text,
                    "pipeline": pipeline,
                    "requestId": req.request_id,
                    "model_id": pipeline_model_id,
                    "blob_url": req.blob_url,
                    "blob_name": req.blob_name,
                    "blob_container": req.blob_container,
                    "blob_account_url": req.blob_account_url,
                    "blob_connection_string": req.blob_connection_string,
                });
                let resp = state
                    .http
                    .post(format!("{}/process", worker_url.trim_end_matches('/')))
                    .json(&body)
                    .send()
                    .await
                    .map_err(|e| AppError(StatusCode::BAD_GATEWAY, format!("Pipeline worker error: {e}")))?;
                let mut result: Value = resp
                    .json()
                    .await
                    .map_err(|e| AppError(StatusCode::BAD_GATEWAY, format!("Pipeline worker response error: {e}")))?;
                result["_routed_to"] = serde_json::json!(format!("pipeline:{}", pipeline));
                return Ok(Json(result));
            }

            // Route B: Pipeline references a model — call model directly
            if let Some(model) = pipeline_model_id
            {
                let text = req
                    .text
                    .or(req.data)
                    .ok_or_else(|| {
                        AppError(StatusCode::BAD_REQUEST, "Pipeline requires 'text' or 'data'".into())
                    })?;
                let result = call_model(&state, model, TextInput::Single(text)).await?;
                return Ok(Json(result));
            }
        }

        return Err(AppError(
            StatusCode::NOT_FOUND,
            format!("Pipeline '{pipeline}' not found"),
        ));
    }

    // Legacy content-type routing
    let content_type = req.content_type_hint.as_deref().unwrap_or("");
    let (backend_name, backend_url) = get_backend_for_content_type(content_type, &state.native_urls);

    let body = json!({
        "text": req.text,
        "blobUrl": req.blob_url,
        "contentTypeHint": content_type,
        "requestId": req.request_id,
    });

    let resp = state
        .http
        .post(format!("{backend_url}/embed"))
        .json(&body)
        .send()
        .await?;
    let mut result: Value = resp.json().await?;
    result["_routed_to"] = json!(backend_name);
    Ok(Json(result))
}

async fn handle_embed_batch(
    State(state): State<AppState>,
    Json(req): Json<EmbedBatchRequest>,
) -> Result<Response, AppError> {
    if req.texts.is_empty() {
        return Err(AppError(
            StatusCode::BAD_REQUEST,
            "'texts' list is required and must be non-empty".into(),
        ));
    }

    // Mock pipeline routing — uses pre-computed embeddings, builds response via string concat
    // Zero per-request allocation: no RNG, no Value tree, no serde serialization
    if let Some(pipeline) = &req.pipeline {
        if pipeline == "mock-embedding" || pipeline == "mock-1536" {
            let single = if pipeline == "mock-1536" {
                &state.mock_1536_single
            } else {
                &state.mock_128_single
            };
            let n = req.texts.len();
            let mut buf = String::with_capacity(single.len() * n + 100);
            buf.push_str(r#"{"outputs":["#);
            for i in 0..n {
                if i > 0 { buf.push(','); }
                buf.push_str(single);
            }
            buf.push_str(r#"],"pipeline":""#);
            buf.push_str(pipeline);
            buf.push_str(r#"","batch_size":"#);
            buf.push_str(&n.to_string());
            buf.push('}');
            return Ok((
                [(header::CONTENT_TYPE, "application/json")],
                buf,
            ).into_response());
        }

        // Real pipeline — resolve model_id from pipeline config and batch embed via model
        if !state.pipelines.contains_key(pipeline.as_str()) {
            load_pipelines_from_cosmos(&state).await.map_err(registry_unavailable)?;
        }
        let pipeline_cfg = state.pipelines.get(pipeline.as_str()).map(|v| v.value().clone());
        if let Some(cfg) = pipeline_cfg {
            let resolved_model = pipeline_model_id(&cfg);
            if let Some(model_id) = resolved_model {
                let result =
                    call_model(&state, model_id, TextInput::Batch(req.texts.clone())).await?;
                let embeddings = result
                    .get("embeddings")
                    .and_then(|v| v.as_array())
                    .cloned()
                    .unwrap_or_default();
                let outputs: Vec<Value> = embeddings.iter().map(|e| json!([e])).collect();
                return Ok(Json(json!({
                    "outputs": outputs,
                    "model_id": model_id,
                    "pipeline": pipeline,
                    "batch_size": req.texts.len(),
                    "usage": result.get("usage").unwrap_or(&json!({})),
                    "model": result.get("model").unwrap_or(&json!({})),
                })).into_response());
            }
        }
        return Err(AppError(
            StatusCode::NOT_FOUND,
            format!("Pipeline '{pipeline}' not found or has no embedding model"),
        ));
    }

    // Model ID routing
    if let Some(model_id) = &req.model_id {
        let result =
            call_model(&state, model_id, TextInput::Batch(req.texts.clone())).await?;
        let embeddings = result
            .get("embeddings")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default();
        let outputs: Vec<Value> = embeddings.iter().map(|e| json!([e])).collect();
        return Ok(Json(json!({
            "outputs": outputs,
            "model_id": model_id,
            "batch_size": req.texts.len(),
            "usage": result.get("usage").unwrap_or(&json!({})),
            "model": result.get("model").unwrap_or(&json!({})),
        })).into_response());
    }

    Err(AppError(
        StatusCode::BAD_REQUEST,
        "Either 'model_id' or 'pipeline' is required".into(),
    ))
}

fn get_backend_for_content_type<'a>(
    content_type: &str,
    urls: &'a HashMap<String, String>,
) -> (&'a str, &'a str) {
    let ct = content_type.to_lowercase();
    if ct.starts_with("image/") {
        if let Some(url) = urls.get("clip") {
            return ("clip", url.as_str());
        }
    }
    if ct.starts_with("text/") || ct.contains("json") || ct.contains("xml") {
        if let Some(url) = urls.get("bge") {
            return ("bge", url.as_str());
        }
    }
    urls.get("dse-qwen2")
        .map(|u| ("dse-qwen2", u.as_str()))
        .unwrap_or(("unknown", "http://localhost:8000"))
}

// ============================================================================
// Health
// ============================================================================

async fn handle_health(State(state): State<AppState>) -> impl IntoResponse {
    let mut backends = json!({});
    for (name, url) in state.native_urls.iter() {
        let health = state
            .http
            .get(format!("{url}/health"))
            .timeout(Duration::from_secs(3))
            .send()
            .await;
        match health {
            Ok(r) if r.status().is_success() => {
                if let Ok(body) = r.json::<Value>().await {
                    backends[name] = body;
                } else {
                    backends[name] = json!({"status": "ok"});
                }
            }
            Ok(r) => backends[name] = json!({"error": format!("HTTP {}", r.status())}),
            Err(e) => backends[name] = json!({"error": e.to_string()}),
        }
    }

    let mut external_models = json!({});
    for item in state.registry.iter() {
        let (id, cfg) = item.pair();
        external_models[id] = json!({
            "status": "configured",
            "name": cfg["name"].as_str().unwrap_or(""),
            "type": cfg["type"].as_str().unwrap_or(""),
        });
    }

    Json(json!({
        "status": "healthy",
        "service": "DocGrok",
        "version": "7.0.0-rust",
        "backends": backends,
        "external_models": external_models,
    }))
}

// ============================================================================
// Admin — Model Registry
// ============================================================================

async fn list_registry_models(State(state): State<AppState>) -> Result<impl IntoResponse, AppError> {
    load_models_from_cosmos(&state).await.map_err(registry_unavailable)?;
    let mut models = Vec::new();

    // Native models from K8s — only deployments with omnivec/role=model label
    if let Some(k8s) = &state.k8s {
        let api: kube::Api<k8s_openapi::api::apps::v1::Deployment> =
            kube::Api::namespaced(k8s.clone(), &state.namespace);
        let lp = kube::api::ListParams::default().labels("omnivec/role=model");
        let list = api.list(&lp).await
            .map_err(|e| registry_unavailable(format!("Native model discovery failed: {e}")))?;
        {
            for dep in list.items {
                let name = dep.metadata.name.clone().unwrap_or_default();

                let spec = dep.spec.as_ref();
                let replicas = spec.map(|s| s.replicas.unwrap_or(0)).unwrap_or(0);
                let ready = dep
                    .status
                    .as_ref()
                    .and_then(|s| s.ready_replicas)
                    .unwrap_or(0);

                let container = spec
                    .and_then(|s| s.template.spec.as_ref())
                    .and_then(|ps| ps.containers.first());

                let image = container.map(|c| c.image.clone().unwrap_or_default()).unwrap_or_default();
                let resources = container.and_then(|c| c.resources.as_ref());
                let gpu = resources
                    .and_then(|r| r.requests.as_ref())
                    .and_then(|req| req.get("nvidia.com/gpu"))
                    .map(|q| q.0.clone())
                    .unwrap_or_else(|| "0".into());
                let memory = resources
                    .and_then(|r| r.requests.as_ref())
                    .and_then(|req| req.get("memory"))
                    .map(|q| q.0.clone())
                    .unwrap_or_else(|| "unknown".into());

                let model_type = if name == "dse-qwen2" || name == "clip" {
                    "vision"
                } else {
                    "text"
                };

                models.push(json!({
                    "id": format!("mdl-native-{name}"),
                    "name": name,
                    "kind": "native",
                    "model_type": model_type,
                    "status": if ready > 0 { "running" } else { "stopped" },
                    "replicas": replicas,
                    "ready_replicas": ready,
                    "image": image,
                    "gpu": gpu,
                    "memory": memory,
                }));
            }
        }
    }

    // External models from registry
    for item in state.registry.iter() {
        let (id, cfg) = item.pair();
        models.push(json!({
            "id": id,
            "name": cfg["name"].as_str().unwrap_or(""),
            "kind": "external",
            "type": cfg["type"].as_str().unwrap_or(""),
            "endpoint": cfg["endpoint"].as_str().unwrap_or(""),
            "deployment": cfg["deployment"].as_str().unwrap_or(""),
            "embedding_dim": cfg["embedding_dim"].as_u64().unwrap_or(0),
            "api_version": cfg["api_version"].as_str().unwrap_or(""),
            "status": "available",
        }));
    }

    Ok(Json(json!({"models": models})))
}

async fn register_model(
    State(state): State<AppState>,
    Json(req): Json<RegisterModelRequest>,
) -> Result<impl IntoResponse, AppError> {
    let model_id = req.id.unwrap_or_else(|| {
        let hash = format!("{:x}", md5_hash(&req.name));
        format!("mdl-ext-{}", &hash[..8])
    });

    let _guard = state.metadata_lock.lock().await;
    let mut cfg = if state.cosmos.is_some() {
        let mut docs = cosmos_query(
            &state,
            "SELECT * FROM c WHERE c.doc_type = @type AND c.id = @id",
            &[("@type", "docgrok_model"), ("@id", &model_id)],
        ).await.map_err(registry_unavailable)?;
        if docs.len() > 1 || docs.first().is_some_and(|doc|
            doc["id"].as_str() != Some(model_id.as_str()) || doc["doc_type"] != "docgrok_model")
        {
            return Err(registry_unavailable("Invalid model registry lookup response".into()));
        }
        docs.pop().unwrap_or_else(|| json!({}))
    } else {
        state.registry.get(&model_id).map(|entry| entry.value().clone())
            .unwrap_or_else(|| json!({}))
    };
    let write_mode = if state.cosmos.is_some() && cfg.get("id").is_some() {
        RegistryWrite::Replace(cfg["_etag"].as_str()
            .ok_or_else(|| registry_unavailable("Stored model is missing its ETag".into()))?.to_owned())
    } else {
        RegistryWrite::Create
    };
    let updates = json!({
        "id": model_id,
        "doc_type": "docgrok_model",
        "name": req.name,
        "type": req.model_type,
        "endpoint": req.endpoint,
        "deployment": if req.deployment.is_empty() { req.name.clone() } else { req.deployment },
        "api_version": if req.api_version.is_empty() { "2024-06-01".to_string() } else { req.api_version },
        "embedding_dim": req.embedding_dim,
    });
    let fields = cfg.as_object_mut()
        .ok_or_else(|| registry_unavailable("Invalid stored model document".into()))?;
    fields.extend(updates.as_object().unwrap().clone());
    // Empty/omitted keys are non-credential edits, not credential deletion.
    if !req.api_key.is_empty() || !fields.contains_key("api_key") {
        fields.insert("api_key".into(), json!(req.api_key));
    }
    if let Some(auth_type) = req.auth_type {
        fields.insert("auth_type".into(), json!(auth_type));
    }
    if let Some(client_id) = req.client_id {
        fields.insert("client_id".into(), json!(client_id));
    }

    // Keep lookup/merge/persist/cache under one lock without recursively calling
    // the locking load/save helpers. Never publish an unpersisted cache update.
    if state.cosmos.is_some() {
        cosmos_write(&state, &cfg, write_mode).await.map_err(registry_unavailable)?;
    }
    state.registry.insert(model_id.clone(), cfg);

    info!("Registered model: {model_id} ({})", req.name);
    Ok((
        StatusCode::CREATED,
        Json(json!({"id": model_id, "name": req.name})),
    ))
}

async fn get_registry_model(
    State(state): State<AppState>,
    Path(model_id): Path<String>,
) -> Result<impl IntoResponse, AppError> {
    // Check external registry
    if !state.registry.contains_key(&model_id) && !model_id.starts_with("mdl-native-") {
        load_models_from_cosmos(&state).await.map_err(registry_unavailable)?;
    }
    if let Some(entry) = state.registry.get(&model_id) {
        return Ok(Json(entry.value().clone()));
    }

    // Check native models
    if model_id.starts_with("mdl-native-") {
        let name = &model_id["mdl-native-".len()..];
        if let Some(k8s) = &state.k8s {
            let api: kube::Api<k8s_openapi::api::apps::v1::Deployment> =
                kube::Api::namespaced(k8s.clone(), &state.namespace);
            if let Ok(dep) = api.get(name).await {
                let replicas = dep.spec.as_ref().map(|s| s.replicas.unwrap_or(0)).unwrap_or(0);
                let ready = dep
                    .status
                    .as_ref()
                    .and_then(|s| s.ready_replicas)
                    .unwrap_or(0);
                let status = if ready > 0 { "running" } else { "stopped" };
                return Ok(Json(json!({
                    "id": model_id,
                    "name": name,
                    "kind": "native",
                    "status": status,
                    "replicas": replicas,
                    "ready_replicas": ready,
                })));
            }
        }
    }

    Err(AppError(
        StatusCode::NOT_FOUND,
        format!("Model '{model_id}' not found"),
    ))
}

async fn delete_registry_model(
    State(state): State<AppState>,
    Path(model_id): Path<String>,
) -> Result<impl IntoResponse, AppError> {
    load_models_from_cosmos(&state).await.map_err(registry_unavailable)?;
    remove_registry_document(&state, &state.registry, &model_id, "docgrok_model").await?;

    info!("Deleted model: {model_id}");
    Ok(Json(json!({"deleted": model_id})))
}

// Proxy an OpenAI-compatible chat-completion call through a registered
// external model. Body: {messages, tools?, tool_choice?, temperature?, max_tokens?}.
// Returns: {model_id, content, tool_calls, role, finish_reason, usage}.
// Perform a real probe of an external model's embedding endpoint to determine
// live health. Mirrors the inline check inside `run_health_check_loop` so
// per-model health calls reflect actual upstream behavior (auth + reachability).
// Returns {"ok": bool, "status": <upstream HTTP code>, "detail": "..."}.
async fn healthcheck_model(
    State(state): State<AppState>,
    Path(model_id): Path<String>,
) -> Result<impl IntoResponse, AppError> {
    if !state.registry.contains_key(&model_id) {
        load_models_from_cosmos(&state).await.map_err(registry_unavailable)?;
    }
    let cfg = state
        .registry
        .get(&model_id)
        .ok_or_else(|| AppError(StatusCode::NOT_FOUND, format!("Model '{model_id}' not found")))?
        .value()
        .clone();

    let model_type = cfg["type"].as_str().unwrap_or("").to_string();
    let endpoint = cfg["endpoint"].as_str().unwrap_or("").trim_end_matches('/').to_string();
    let deployment = cfg["deployment"]
        .as_str()
        .or_else(|| cfg["name"].as_str())
        .unwrap_or("")
        .to_string();
    let api_version = cfg["api_version"].as_str().unwrap_or("2024-06-01").to_string();
    let api_key = cfg["api_key"].as_str().unwrap_or("").to_string();

    if endpoint.is_empty() {
        return Ok(Json(json!({
            "ok": false,
            "status": 0,
            "detail": "No endpoint configured"
        })));
    }

    let test_url = match model_type.as_str() {
        "azure-openai" => format!(
            "{endpoint}/openai/deployments/{deployment}/embeddings?api-version={api_version}"
        ),
        "openai" => format!("{endpoint}/embeddings"),
        _ => endpoint.clone(),
    };

    let mut req = state
        .http
        .post(&test_url)
        .timeout(Duration::from_secs(10))
        .json(&json!({"input": "health", "model": &deployment}));

    if model_type == "azure-openai" {
        if !api_key.is_empty() {
            req = req.header("api-key", &api_key);
        } else if let Ok(token) = get_aoai_token(&state.http).await {
            req = req.header("Authorization", format!("Bearer {token}"));
        }
    } else if !api_key.is_empty() {
        req = req.header("Authorization", format!("Bearer {api_key}"));
    }

    match req.send().await {
        Ok(r) => {
            let status_code = r.status().as_u16();
            // 200 = pass, 429 = throttled but reachable + authed = pass.
            let ok = r.status().is_success() || status_code == 429;
            let body = r.text().await.unwrap_or_default();
            let detail = if ok {
                format!("HTTP {status_code}")
            } else {
                format!("HTTP {status_code}: {}", &body[..body.len().min(150)])
            };
            Ok(Json(json!({
                "ok": ok,
                "status": status_code,
                "detail": detail,
            })))
        }
        Err(e) => Ok(Json(json!({
            "ok": false,
            "status": 0,
            "detail": format!("Cannot reach endpoint: {e}"),
        }))),
    }
}

// Proxy an OpenAI-compatible chat-completion call through a registered
// external model. Body: {messages, tools?, tool_choice?, temperature?, max_tokens?}.
// Returns: {model_id, content, tool_calls, role, finish_reason, usage}.
async fn chat_via_model(
    State(state): State<AppState>,
    Path(model_id): Path<String>,
    Json(body): Json<Value>,
) -> Result<impl IntoResponse, AppError> {
    if !state.registry.contains_key(&model_id) {
        load_models_from_cosmos(&state).await.map_err(registry_unavailable)?;
    }
    let cfg = state
        .registry
        .get(&model_id)
        .ok_or_else(|| AppError(StatusCode::NOT_FOUND, format!("Model '{model_id}' not found")))?
        .value()
        .clone();

    let model_type = cfg["type"].as_str().unwrap_or("");
    if model_type != "azure-openai" {
        return Err(AppError(
            StatusCode::BAD_REQUEST,
            format!("Chat unsupported for model type '{model_type}'"),
        ));
    }
    let endpoint = cfg["endpoint"].as_str().unwrap_or("").trim_end_matches('/');
    let deployment = cfg["deployment"]
        .as_str()
        .or_else(|| cfg["name"].as_str())
        .unwrap_or("");
    let api_version = cfg["api_version"]
        .as_str()
        .unwrap_or("2024-08-01-preview");
    let api_key = cfg["api_key"].as_str().unwrap_or("");

    let messages = body.get("messages").cloned().ok_or_else(|| {
        AppError(StatusCode::BAD_REQUEST, "'messages' is required".into())
    })?;
    let mut payload = json!({
        "messages": messages,
        "temperature": body.get("temperature").and_then(|v| v.as_f64()).unwrap_or(0.1),
    });
    if let Some(tools) = body.get("tools").cloned() {
        if !tools.is_null() {
            payload["tools"] = tools;
            payload["tool_choice"] = body
                .get("tool_choice")
                .cloned()
                .unwrap_or_else(|| json!("auto"));
        }
    }
    if let Some(mt) = body.get("max_tokens").and_then(|v| v.as_u64()) {
        payload["max_tokens"] = json!(mt);
    }

    let url = format!(
        "{endpoint}/openai/deployments/{deployment}/chat/completions?api-version={api_version}"
    );
    let mut headers = reqwest::header::HeaderMap::new();
    if !api_key.is_empty() {
        headers.insert("api-key", api_key.parse().unwrap());
    } else {
        let token = get_aoai_token(&state.http).await.map_err(|e| {
            AppError(
                StatusCode::INTERNAL_SERVER_ERROR,
                format!("Failed to get AOAI token: {e}"),
            )
        })?;
        headers.insert(
            "Authorization",
            format!("Bearer {token}").parse().unwrap(),
        );
    }

    let resp = state
        .http
        .post(&url)
        .headers(headers)
        .json(&payload)
        .send()
        .await
        .map_err(|e| AppError(StatusCode::BAD_GATEWAY, format!("Upstream error: {e}")))?;
    let status = resp.status();
    if !status.is_success() {
        let text = resp.text().await.unwrap_or_default();
        return Err(AppError(
            StatusCode::from_u16(status.as_u16()).unwrap_or(StatusCode::BAD_GATEWAY),
            format!("Upstream {status}: {text}"),
        ));
    }
    let data: Value = resp
        .json()
        .await
        .map_err(|e| AppError(StatusCode::BAD_GATEWAY, format!("Decode error: {e}")))?;

    let choice = data
        .get("choices")
        .and_then(|c| c.get(0))
        .cloned()
        .unwrap_or_else(|| json!({}));
    let msg = choice.get("message").cloned().unwrap_or_else(|| json!({}));
    Ok(Json(json!({
        "model_id": model_id,
        "content": msg.get("content").and_then(|v| v.as_str()).unwrap_or(""),
        "tool_calls": msg.get("tool_calls").cloned().unwrap_or_else(|| json!([])),
        "role": msg.get("role").and_then(|v| v.as_str()).unwrap_or("assistant"),
        "finish_reason": choice.get("finish_reason").and_then(|v| v.as_str()).unwrap_or("stop"),
        "usage": data.get("usage").cloned().unwrap_or_else(|| json!({})),
    })))
}

// ============================================================================
// Admin — K8s Model Management
// ============================================================================

async fn list_k8s_models(State(state): State<AppState>) -> impl IntoResponse {
    list_registry_models(State(state)).await
}

async fn scale_model(
    State(state): State<AppState>,
    Path(name): Path<String>,
    Json(req): Json<ScaleRequest>,
) -> Result<impl IntoResponse, AppError> {
    let k8s = state
        .k8s
        .as_ref()
        .ok_or_else(|| AppError(StatusCode::SERVICE_UNAVAILABLE, "K8s not available".into()))?;

    let api: kube::Api<k8s_openapi::api::apps::v1::Deployment> =
        kube::Api::namespaced(k8s.clone(), &state.namespace);

    let patch = json!({
        "spec": {"replicas": req.replicas}
    });

    api.patch(
        &name,
        &kube::api::PatchParams::apply("docgrok-router"),
        &kube::api::Patch::Merge(&patch),
    )
    .await
    .map_err(|e| AppError(StatusCode::INTERNAL_SERVER_ERROR, format!("Scale failed: {e}")))?;

    info!("Scaled {name} to {} replicas", req.replicas);
    Ok(Json(json!({"name": name, "replicas": req.replicas})))
}

async fn enable_model(
    State(state): State<AppState>,
    Path(name): Path<String>,
) -> Result<impl IntoResponse, AppError> {
    scale_model_to(state, &name, 1).await
}

async fn disable_model(
    State(state): State<AppState>,
    Path(name): Path<String>,
) -> Result<impl IntoResponse, AppError> {
    scale_model_to(state, &name, 0).await
}

async fn scale_model_to(state: AppState, name: &str, replicas: i32) -> Result<impl IntoResponse, AppError> {
    let k8s = state
        .k8s
        .as_ref()
        .ok_or_else(|| AppError(StatusCode::SERVICE_UNAVAILABLE, "K8s not available".into()))?;

    let api: kube::Api<k8s_openapi::api::apps::v1::Deployment> =
        kube::Api::namespaced(k8s.clone(), &state.namespace);

    let patch = json!({"spec": {"replicas": replicas}});
    api.patch(
        name,
        &kube::api::PatchParams::apply("docgrok-router"),
        &kube::api::Patch::Merge(&patch),
    )
    .await
    .map_err(|e| AppError(StatusCode::INTERNAL_SERVER_ERROR, format!("Failed: {e}")))?;

    Ok(Json(json!({"name": name, "replicas": replicas})))
}

async fn restart_model(
    State(state): State<AppState>,
    Path(name): Path<String>,
) -> Result<impl IntoResponse, AppError> {
    let k8s = state
        .k8s
        .as_ref()
        .ok_or_else(|| AppError(StatusCode::SERVICE_UNAVAILABLE, "K8s not available".into()))?;

    let api: kube::Api<k8s_openapi::api::apps::v1::Deployment> =
        kube::Api::namespaced(k8s.clone(), &state.namespace);

    let now = chrono::Utc::now().to_rfc3339();
    let patch = json!({
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "kubectl.kubernetes.io/restartedAt": now
                    }
                }
            }
        }
    });

    api.patch(
        &name,
        &kube::api::PatchParams::apply("docgrok-router"),
        &kube::api::Patch::Merge(&patch),
    )
    .await
    .map_err(|e| AppError(StatusCode::INTERNAL_SERVER_ERROR, format!("Restart failed: {e}")))?;

    info!("Restarted {name}");
    Ok(Json(json!({"name": name, "restarted": true})))
}

async fn get_logs(
    State(state): State<AppState>,
    Path(name): Path<String>,
) -> Result<impl IntoResponse, AppError> {
    let k8s = state
        .k8s
        .as_ref()
        .ok_or_else(|| AppError(StatusCode::SERVICE_UNAVAILABLE, "K8s not available".into()))?;

    let pods: kube::Api<k8s_openapi::api::core::v1::Pod> =
        kube::Api::namespaced(k8s.clone(), &state.namespace);

    let list = pods
        .list(&kube::api::ListParams::default().labels(&format!("app={name}")))
        .await
        .map_err(|e| AppError(StatusCode::INTERNAL_SERVER_ERROR, format!("List pods: {e}")))?;

    let mut logs = Vec::new();
    for pod in list.items.iter().take(1) {
        let pod_name = pod.metadata.name.as_deref().unwrap_or("");
        match pods
            .logs(
                pod_name,
                &kube::api::LogParams {
                    tail_lines: Some(100),
                    ..Default::default()
                },
            )
            .await
        {
            Ok(log) => logs.push(json!({"pod": pod_name, "logs": log})),
            Err(e) => logs.push(json!({"pod": pod_name, "error": e.to_string()})),
        }
    }

    Ok(Json(json!({"name": name, "logs": logs})))
}

// ============================================================================
// Admin — System
// ============================================================================

async fn system_info(State(state): State<AppState>) -> impl IntoResponse {
    let mut gpu_info = Vec::new();
    if let Some(k8s) = &state.k8s {
        let nodes: kube::Api<k8s_openapi::api::core::v1::Node> = kube::Api::all(k8s.clone());
        if let Ok(list) = nodes.list(&Default::default()).await {
            for node in list.items {
                let name = node.metadata.name.clone().unwrap_or_default();
                let allocatable = node
                    .status
                    .as_ref()
                    .and_then(|s| s.allocatable.as_ref());
                let gpus = allocatable
                    .and_then(|a| a.get("nvidia.com/gpu"))
                    .map(|q| q.0.clone())
                    .unwrap_or_else(|| "0".into());
                gpu_info.push(json!({"node": name, "gpus": gpus}));
            }
        }
    }

    Json(json!({
        "runtime": "rust",
        "version": "7.0.0-rust",
        "nodes": gpu_info,
    }))
}

// ============================================================================
// Admin — Pipelines
// ============================================================================

async fn list_pipelines(State(state): State<AppState>) -> Result<impl IntoResponse, AppError> {
    load_pipelines_from_cosmos(&state).await.map_err(registry_unavailable)?;
    let pipelines: Vec<Value> = state.pipelines.iter().map(|e| e.value().clone()).collect();
    Ok(Json(json!({"pipelines": pipelines})))
}

async fn get_pipeline(
    State(state): State<AppState>,
    Path(name): Path<String>,
) -> Result<impl IntoResponse, AppError> {
    if !state.pipelines.contains_key(&name) {
        load_pipelines_from_cosmos(&state).await.map_err(registry_unavailable)?;
    }
    state
        .pipelines
        .get(&name)
        .map(|v| Json(v.value().clone()))
        .ok_or_else(|| AppError(StatusCode::NOT_FOUND, format!("Pipeline '{name}' not found")))
}

async fn create_pipeline(
    State(state): State<AppState>,
    Json(req): Json<PipelineRequest>,
) -> Result<impl IntoResponse, AppError> {
    let name = req.config["name"]
        .as_str()
        .ok_or_else(|| AppError(StatusCode::BAD_REQUEST, "Pipeline 'name' is required".into()))?
        .to_string();

    // ID is auto-generated as trp-<hash> unless the caller supplies one.
    // Existing IDs (e.g. when reloading from Cosmos) are preserved.
    let pipeline_id = req
        .config
        .get("id")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string())
        .unwrap_or_else(|| {
            let hash = format!("{:x}", md5_hash(&name));
            format!("trp-{}", &hash[..8])
        });

    let mut doc = req.config.clone();
    doc["id"] = json!(pipeline_id);
    doc["name"] = json!(name);
    doc["doc_type"] = json!("docgrok_pipeline");

    save_registry_document(&state, &state.pipelines, &doc).await?;

    Ok((StatusCode::CREATED, Json(json!({"id": pipeline_id, "name": name}))))
}

async fn update_pipeline(
    State(state): State<AppState>,
    Path(id): Path<String>,
    Json(req): Json<PipelineRequest>,
) -> Result<impl IntoResponse, AppError> {
    if !state.pipelines.contains_key(&id) {
        load_pipelines_from_cosmos(&state).await.map_err(registry_unavailable)?;
    }
    if !state.pipelines.contains_key(&id) {
        return Err(AppError(
            StatusCode::NOT_FOUND,
            format!("Pipeline '{id}' not found"),
        ));
    }

    let mut doc = req.config.clone();
    doc["id"] = json!(id);
    doc["doc_type"] = json!("docgrok_pipeline");

    save_registry_document(&state, &state.pipelines, &doc).await?;

    Ok(Json(json!({"id": id})))
}

async fn delete_pipeline(
    State(state): State<AppState>,
    Path(name): Path<String>,
) -> Result<impl IntoResponse, AppError> {
    load_pipelines_from_cosmos(&state).await.map_err(registry_unavailable)?;
    remove_registry_document(&state, &state.pipelines, &name, "docgrok_pipeline").await?;

    Ok(Json(json!({"deleted": name})))
}

async fn reset_pipeline(
    State(state): State<AppState>,
    Path(name): Path<String>,
) -> Result<impl IntoResponse, AppError> {
    if !state.pipelines.contains_key(&name) {
        load_pipelines_from_cosmos(&state).await.map_err(registry_unavailable)?;
    }
    let mut doc = state.pipelines.get(&name).map(|entry| entry.value().clone())
        .ok_or_else(|| AppError(StatusCode::NOT_FOUND, format!("Pipeline '{name}' not found")))?;
    doc["reset_at"] = json!(chrono::Utc::now().to_rfc3339());
    save_registry_document(&state, &state.pipelines, &doc).await?;
    Ok(Json(json!({"name": name, "reset": true})))
}

async fn pipeline_options() -> impl IntoResponse {
    Json(json!({
        "step_types": ["local", "api", "external"],
        "model_types": ["azure-openai", "openai", "native"],
        "local_functions": [
            {"name": "pymupdf", "description": "Convert PDF pages to images using PyMuPDF"},
            {"name": "paddleocr", "description": "Extract text from images using PaddleOCR"},
            {"name": "chunk_text", "description": "Split text into chunks at paragraph/sentence boundaries"},
        ],
    }))
}

// ============================================================================
// Controller — Background Health Checks
// ============================================================================

async fn run_model_health_checks(state: &AppState) {
    let now = chrono::Utc::now().to_rfc3339();
    let mut checked = 0u32;

    // Check native models via K8s
    if let Some(k8s) = &state.k8s {
        let api: kube::Api<k8s_openapi::api::apps::v1::Deployment> =
            kube::Api::namespaced(k8s.clone(), &state.namespace);
        let lp = kube::api::ListParams::default().labels("omnivec/role=model");
        if let Ok(list) = api.list(&lp).await {
            for dep in list.items {
                let name = dep.metadata.name.clone().unwrap_or_default();
                let model_id = format!("mdl-native-{name}");
                let spec = dep.spec.as_ref();
                let replicas = spec.map(|s| s.replicas.unwrap_or(0)).unwrap_or(0);
                let ready = dep.status.as_ref().and_then(|s| s.ready_replicas).unwrap_or(0);

                let (status, detail) = if replicas == 0 {
                    ("stopped", format!("Scaled to 0 replicas"))
                } else if ready >= replicas {
                    ("healthy", format!("{ready}/{replicas} replicas ready"))
                } else {
                    ("unhealthy", format!("{ready}/{replicas} replicas ready"))
                };

                // Check HTTP health if running
                let mut endpoint_check = json!(null);
                if ready > 0 {
                    if let Some(url) = state.native_urls.get(&name) {
                        match state.http.get(format!("{url}/health"))
                            .timeout(Duration::from_secs(10))
                            .send().await
                        {
                            Ok(r) if r.status().is_success() => {
                                endpoint_check = json!({"status": "pass", "detail": "Health endpoint OK"});
                            }
                            Ok(r) => {
                                endpoint_check = json!({"status": "fail", "detail": format!("HTTP {}", r.status())});
                            }
                            Err(e) => {
                                endpoint_check = json!({"status": "fail", "detail": format!("{e}")});
                            }
                        }
                    }
                }

                state.model_health.insert(model_id.clone(), json!({
                    "id": model_id,
                    "name": name,
                    "kind": "native",
                    "status": status,
                    "detail": detail,
                    "replicas": replicas,
                    "ready_replicas": ready,
                    "endpoint_check": endpoint_check,
                    "checked_at": now,
                }));
                checked += 1;
            }
        }
    }

    // Never retain DashMap shard guards across remote health-probe awaits.
    let external_models: Vec<(String, Value)> = state.registry.iter()
        .map(|item| (item.key().clone(), item.value().clone())).collect();
    for (model_id, cfg) in external_models {
        let name = cfg["name"].as_str().unwrap_or("").to_string();
        let model_type = cfg["type"].as_str().unwrap_or("").to_string();
        let endpoint = cfg["endpoint"].as_str().unwrap_or("").to_string();
        let deployment = cfg["deployment"].as_str().or_else(|| cfg["name"].as_str()).unwrap_or("").to_string();
        let api_version = cfg["api_version"].as_str().unwrap_or("2024-06-01").to_string();
        let api_key = cfg["api_key"].as_str().unwrap_or("").to_string();

        let mut status = "healthy";
        let mut detail = format!("Registered ({model_type})");
        let mut endpoint_check = json!(null);

        if !endpoint.is_empty() {
            let test_url = match model_type.as_str() {
                "azure-openai" => format!("{endpoint}/openai/deployments/{deployment}/embeddings?api-version={api_version}"),
                "openai" => format!("{endpoint}/embeddings"),
                _ => endpoint.clone(),
            };

            let mut req = state.http.post(&test_url)
                .timeout(Duration::from_secs(10))
                .json(&json!({"input": "health", "model": &deployment}));

            if model_type == "azure-openai" {
                if !api_key.is_empty() {
                    req = req.header("api-key", &api_key);
                } else if let Ok(token) = get_aoai_token(&state.http).await {
                    req = req.header("Authorization", format!("Bearer {token}"));
                }
            } else if !api_key.is_empty() {
                req = req.header("Authorization", format!("Bearer {api_key}"));
            }

            match req.send().await {
                Ok(r) if r.status().is_success() || r.status().as_u16() == 429 => {
                    let s = r.status();
                    endpoint_check = json!({"status": "pass", "detail": format!("HTTP {s}")});
                }
                Ok(r) => {
                    let s = r.status();
                    let body = r.text().await.unwrap_or_default();
                    status = "unhealthy";
                    detail = format!("Endpoint error: HTTP {s}");
                    endpoint_check = json!({"status": "fail", "detail": format!("HTTP {s}: {}", &body[..body.len().min(150)])});
                }
                Err(e) => {
                    status = "unhealthy";
                    detail = format!("Cannot reach endpoint");
                    endpoint_check = json!({"status": "fail", "detail": format!("{e}")});
                }
            }
        }

        state.model_health.insert(model_id.clone(), json!({
            "id": model_id,
            "name": name,
            "kind": "external",
            "type": model_type,
            "status": status,
            "detail": detail,
            "endpoint": endpoint,
            "endpoint_check": endpoint_check,
            "checked_at": now,
        }));
        checked += 1;
    }

    // Update last check timestamp
    let mut last = state.last_health_check.write().await;
    *last = Some(std::time::Instant::now());

    info!("Health check complete: {checked} models checked");
}

async fn controller_health(State(state): State<AppState>) -> impl IntoResponse {
    let last = state.last_health_check.read().await;
    let age_secs = last.map(|t| t.elapsed().as_secs()).unwrap_or(0);
    Json(json!({
        "status": "healthy",
        "service": "DocGrok Controller",
        "mode": "controller",
        "models_tracked": state.model_health.len(),
        "last_check_age_secs": age_secs,
    }))
}

async fn get_model_health(State(state): State<AppState>) -> impl IntoResponse {
    let results: Vec<Value> = state.model_health.iter().map(|e| e.value().clone()).collect();
    let last = state.last_health_check.read().await;
    let age_secs = last.map(|t| t.elapsed().as_secs());
    let healthy = results.iter().filter(|r| r["status"] == "healthy").count();
    let unhealthy = results.iter().filter(|r| r["status"] == "unhealthy").count();

    Json(json!({
        "models": results,
        "summary": {
            "total": results.len(),
            "healthy": healthy,
            "unhealthy": unhealthy,
        },
        "last_check_age_secs": age_secs,
    }))
}

// ============================================================================
// Router — Deployments & Model Health Proxy
// ============================================================================

async fn list_deployments(State(state): State<AppState>) -> impl IntoResponse {
    let mut deployments = Vec::new();

    if let Some(k8s) = &state.k8s {
        let api: kube::Api<k8s_openapi::api::apps::v1::Deployment> =
            kube::Api::namespaced(k8s.clone(), &state.namespace);
        let pods_api: kube::Api<k8s_openapi::api::core::v1::Pod> =
            kube::Api::namespaced(k8s.clone(), &state.namespace);

        // List all deployments in the namespace, filter for docgrok-related ones
        let lp = kube::api::ListParams::default();
        if let Ok(list) = api.list(&lp).await {
            for dep in list.items {
                let name = dep.metadata.name.clone().unwrap_or_default();
                let labels = dep.metadata.labels.clone().unwrap_or_default();
                let app_label = labels.get("app").cloned().unwrap_or_default();
                let component = labels.get("component").cloned().unwrap_or_default();
                let role = labels.get("omnivec/role").cloned().unwrap_or_default();

                // Include DocGrok router, controller, pipeline-worker, and model deployments
                let is_docgrok = app_label == "docgrok" || app_label == "docgrok-controller"
                    || app_label == "docgrok-pipeline-worker"
                    || component == "orchestrator" || component == "controller"
                    || component == "pipeline-worker"
                    || role == "model";
                if !is_docgrok {
                    continue;
                }

                let spec = dep.spec.as_ref();
                let replicas = spec.map(|s| s.replicas.unwrap_or(0)).unwrap_or(0);
                let ready = dep.status.as_ref().and_then(|s| s.ready_replicas).unwrap_or(0);
                let available = dep.status.as_ref().and_then(|s| s.available_replicas).unwrap_or(0);

                let container = spec
                    .and_then(|s| s.template.spec.as_ref())
                    .and_then(|ps| ps.containers.first());
                let image = container.map(|c| c.image.clone().unwrap_or_default()).unwrap_or_default();

                let status = if replicas == 0 {
                    "stopped"
                } else if ready >= replicas {
                    "running"
                } else {
                    "starting"
                };

                let kind = if role == "model" { "model" }
                    else if component == "controller" || app_label == "docgrok-controller" { "controller" }
                    else if component == "pipeline-worker" || app_label == "docgrok-pipeline-worker" { "pipeline-worker" }
                    else { "router" };

                // Query pods for this deployment
                let mut pods = Vec::new();
                let pod_selector = format!("app={}", app_label);
                if let Ok(pod_list) = pods_api.list(&kube::api::ListParams::default().labels(&pod_selector)).await {
                    for pod in pod_list.items {
                        let pod_name = pod.metadata.name.clone().unwrap_or_default();
                        let pod_status = pod.status.as_ref()
                            .and_then(|s| s.phase.clone())
                            .unwrap_or_else(|| "Unknown".to_string());
                        let restarts: i32 = pod.status.as_ref()
                            .and_then(|s| s.container_statuses.as_ref())
                            .map(|cs| cs.iter().map(|c| c.restart_count).sum())
                            .unwrap_or(0);
                        let age = pod.metadata.creation_timestamp.as_ref()
                            .map(|ts| {
                                let elapsed = chrono::Utc::now() - ts.0;
                                if elapsed.num_days() > 0 { format!("{}d", elapsed.num_days()) }
                                else if elapsed.num_hours() > 0 { format!("{}h", elapsed.num_hours()) }
                                else { format!("{}m", elapsed.num_minutes()) }
                            })
                            .unwrap_or_else(|| "-".to_string());
                        pods.push(json!({
                            "name": pod_name,
                            "status": pod_status,
                            "restarts": restarts,
                            "age": age,
                        }));
                    }
                }

                deployments.push(json!({
                    "name": name,
                    "kind": kind,
                    "component": component,
                    "status": status,
                    "replicas": replicas,
                    "ready_replicas": ready,
                    "available_replicas": available,
                    "image": image,
                    "labels": labels,
                    "pods": pods,
                }));
            }
        }
    }

    Json(json!({"deployments": deployments}))
}

async fn proxy_model_health(State(state): State<AppState>) -> impl IntoResponse {
    let url = format!("{}/admin/health/models", state.controller_url);
    match state.http.get(&url).timeout(Duration::from_secs(5)).send().await {
        Ok(resp) if resp.status().is_success() => {
            let body: Value = resp.json().await.unwrap_or(json!({"error": "parse error"}));
            (StatusCode::OK, Json(body)).into_response()
        }
        Ok(resp) => {
            let status = resp.status();
            (StatusCode::BAD_GATEWAY, Json(json!({"error": format!("Controller returned HTTP {status}")}))).into_response()
        }
        Err(e) => {
            (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"error": format!("Controller unreachable: {e}")}))).into_response()
        }
    }
}

// ============================================================================
// Helpers
// ============================================================================

fn md5_hash(input: &str) -> u64 {
    // Simple hash for model ID generation (not cryptographic)
    let mut h: u64 = 0xcbf29ce484222325;
    for byte in input.bytes() {
        h ^= byte as u64;
        h = h.wrapping_mul(0x100000001b3);
    }
    h
}


// ============================================================================
// Pipeline-worker transparent proxy
// ============================================================================
// Forwards the request (path + query + body + content-type) to the configured
// pipeline-worker. Required so that all transform/process traffic flows
// through the docgrok router rather than reaching the pipeline-worker directly.
async fn proxy_pipeline_worker(
    State(state): State<AppState>,
    method: Method,
    uri: Uri,
    headers: axum::http::HeaderMap,
    body: Bytes,
) -> Result<Response, AppError> {
    let path_and_query = uri
        .path_and_query()
        .map(|pq| pq.as_str())
        .unwrap_or(uri.path());
    let target = format!(
        "{}{}",
        state.pipeline_worker_url.trim_end_matches('/'),
        path_and_query
    );
    let mut req_builder = state.http.request(method.clone(), &target).body(body);
    if let Some(ct) = headers.get(header::CONTENT_TYPE) {
        if let Ok(val) = ct.to_str() {
            req_builder = req_builder.header(header::CONTENT_TYPE, val);
        }
    }
    let resp = req_builder.send().await.map_err(|e| {
        AppError(
            StatusCode::BAD_GATEWAY,
            format!("Pipeline worker error: {e}"),
        )
    })?;
    let status = resp.status();
    let resp_ct = resp
        .headers()
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("application/json")
        .to_string();
    let body_bytes = resp.bytes().await.map_err(|e| {
        AppError(
            StatusCode::BAD_GATEWAY,
            format!("Pipeline worker body error: {e}"),
        )
    })?;
    let mut response = Response::new(Body::from(body_bytes));
    *response.status_mut() = StatusCode::from_u16(status.as_u16()).unwrap_or(StatusCode::OK);
    if let Ok(ct_val) = resp_ct.parse() {
        response.headers_mut().insert(header::CONTENT_TYPE, ct_val);
    }
    Ok(response)
}

#[cfg(test)]
mod registry_tests {
    use super::*;
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

    fn registration_request(key: Option<&str>) -> RegisterModelRequest {
        let mut request = json!({
            "id": "mdl-ext-existing", "name": "edited name", "type": "azure-openai",
            "endpoint": "https://edited.invalid", "embedding_dim": 1536
        });
        if let Some(key) = key {
            request["api_key"] = json!(key);
        }
        serde_json::from_value(request).unwrap()
    }

    fn registration_store(
        stored: Arc<Mutex<Value>>, fail_read: Arc<AtomicBool>,
        fail_write: Arc<AtomicBool>, writes: Arc<AtomicUsize>,
    ) -> Router {
        let handler = move |headers: axum::http::HeaderMap, Json(body): Json<Value>| {
                let (stored, fail_read, fail_write, writes) =
                    (stored.clone(), fail_read.clone(), fail_write.clone(), writes.clone());
                async move {
                    if headers.contains_key("x-ms-documentdb-isquery") {
                        if fail_read.load(Ordering::SeqCst) {
                            return StatusCode::SERVICE_UNAVAILABLE.into_response();
                        }
                        assert_eq!(body["parameters"], json!([
                            {"name":"@type", "value":"docgrok_model"},
                            {"name":"@id", "value":"mdl-ext-existing"}
                        ]));
                        Json(json!({"Documents":[stored.lock().await.clone()]})).into_response()
                    } else {
                        writes.fetch_add(1, Ordering::SeqCst);
                        assert!(!headers.contains_key("x-ms-documentdb-is-upsert"));
                        assert_eq!(headers.get("if-match").unwrap(), "version-1");
                        if fail_write.load(Ordering::SeqCst) {
                            return StatusCode::SERVICE_UNAVAILABLE.into_response();
                        }
                        *stored.lock().await = body;
                        StatusCode::CREATED.into_response()
                    }
                }
            };
        Router::new()
            .route("/dbs/test/colls/metadata/docs", post(handler.clone()))
            .route("/dbs/test/colls/metadata/docs/mdl-ext-existing", put(handler))
    }

    #[tokio::test]
    async fn model_edits_merge_authoritative_credentials_and_metadata_and_allow_rotation() {
        let original = json!({
            "id":"mdl-ext-existing", "doc_type":"docgrok_model", "name":"old", "_etag":"version-1",
            "api_key":"persisted-test-key", "model_category":"embedding",
            "auth_type":"api_key", "client_id":"existing-client",
            "api_key_envelope":{"version":1, "ciphertext":"test-envelope"}, "custom":{"keep":true}
        });
        let stored = Arc::new(Mutex::new(original.clone()));
        let writes = Arc::new(AtomicUsize::new(0));
        let app = registration_store(stored.clone(), Arc::new(AtomicBool::new(false)),
            Arc::new(AtomicBool::new(false)), writes.clone());
        let (state, server) = test_state(app).await;
        for key in [None, Some(""), Some("rotated-test-key")] {
            // Even a populated but stale cache must not be used as the merge base.
            state.registry.insert("mdl-ext-existing".into(), json!({
                "id":"mdl-ext-existing", "api_key":"stale-test-key", "model_category":"stale"
            }));
            let response = register_model(State(state.clone()), Json(registration_request(key))).await
                .unwrap_or_else(|_| panic!("Model edit failed")).into_response();
            assert_eq!(response.status(), StatusCode::CREATED);
            let doc = stored.lock().await.clone();
            assert_eq!(doc["id"], "mdl-ext-existing");
            assert_eq!(doc["name"], "edited name");
            assert_eq!(doc["endpoint"], "https://edited.invalid");
            assert_eq!(doc["api_key"], key.filter(|key| !key.is_empty()).unwrap_or("persisted-test-key"));
            for field in ["model_category", "api_key_envelope", "custom", "auth_type", "client_id"] {
                assert_eq!(doc[field], original[field]);
            }
            assert_eq!(state.registry.get("mdl-ext-existing").unwrap().value(), &doc);
        }
        assert_eq!(writes.load(Ordering::SeqCst), 3);
        server.abort();
    }

    #[tokio::test]
    async fn model_registration_honors_explicit_auth_metadata_and_preserves_omitted_fields() {
        let stored = Arc::new(Mutex::new(json!({
            "id":"mdl-ext-existing", "doc_type":"docgrok_model", "_etag":"version-1",
            "api_key":"persisted-test-key", "auth_type":"api_key", "client_id":"old-client"
        })));
        let app = registration_store(stored.clone(), Arc::new(AtomicBool::new(false)),
            Arc::new(AtomicBool::new(false)), Arc::new(AtomicUsize::new(0)));
        let (state, server) = test_state(app).await;
        for update in [
            json!({"auth_type":"managed_identity", "client_id":"new-client"}),
            json!({}),
            json!({"auth_type":"api_key", "client_id":""}),
        ] {
            let mut request = json!({
                "id":"mdl-ext-existing", "name":"edited", "type":"azure-openai",
                "endpoint":"https://edited.invalid", "api_key":""
            });
            request.as_object_mut().unwrap().extend(update.as_object().unwrap().clone());
            register_model(State(state.clone()), Json(serde_json::from_value(request).unwrap())).await
                .unwrap_or_else(|_| panic!("Auth metadata edit failed"));
            let doc = stored.lock().await.clone();
            assert_eq!(doc["auth_type"], update.get("auth_type").cloned().unwrap_or(json!("managed_identity")));
            assert_eq!(doc["client_id"], update.get("client_id").cloned().unwrap_or(json!("new-client")));
            assert_eq!(doc["api_key"], "persisted-test-key");
            assert_eq!(state.registry.get("mdl-ext-existing").unwrap().value(), &doc);
        }
        server.abort();
    }

    #[tokio::test]
    async fn model_registration_read_or_persist_failure_preserves_last_good_state() {
        let original = json!({
            "id":"mdl-ext-existing", "doc_type":"docgrok_model", "api_key":"persisted-test-key", "_etag":"version-1",
            "model_category":"embedding", "api_key_envelope":{"ciphertext":"test-envelope"}
        });
        let stored = Arc::new(Mutex::new(original.clone()));
        let fail_read = Arc::new(AtomicBool::new(true));
        let fail_write = Arc::new(AtomicBool::new(false));
        let writes = Arc::new(AtomicUsize::new(0));
        let app = registration_store(stored.clone(), fail_read.clone(), fail_write.clone(), writes.clone());
        let (state, server) = test_state(app).await;
        let cached = json!({"id":"mdl-ext-existing", "api_key":"last-good-cache-key", "name":"cached"});
        state.registry.insert("mdl-ext-existing".into(), cached.clone());
        for lookup_failure in [true, false] {
            fail_read.store(lookup_failure, Ordering::SeqCst);
            fail_write.store(!lookup_failure, Ordering::SeqCst);
            let failure = register_model(State(state.clone()), Json(registration_request(Some("replacement"))))
                .await.err().unwrap();
            assert_eq!(failure.0, StatusCode::SERVICE_UNAVAILABLE);
            assert_eq!(writes.load(Ordering::SeqCst), if lookup_failure { 0 } else { 1 });
            assert_eq!(*stored.lock().await, original);
            assert_eq!(state.registry.get("mdl-ext-existing").unwrap().value(), &cached);
        }
        server.abort();
    }

    #[tokio::test]
    async fn concurrent_model_edit_cannot_overwrite_a_newer_credential() {
        let app = Router::new()
            .route("/dbs/test/colls/metadata/docs", post(|| async {
                Json(json!({"Documents":[{
                    "id":"mdl-ext-existing", "doc_type":"docgrok_model",
                    "_etag":"old-version", "api_key":"old-test-key"
                }]}))
            }))
            .route("/dbs/test/colls/metadata/docs/mdl-ext-existing", put(
                |headers: axum::http::HeaderMap| async move {
                    assert_eq!(headers.get("if-match").unwrap(), "old-version");
                    // Another replica rotated the credential after the lookup.
                    StatusCode::PRECONDITION_FAILED
                },
            ));
        let (state, server) = test_state(app).await;
        let cached = json!({"id":"mdl-ext-existing", "api_key":"last-good-test-key"});
        state.registry.insert("mdl-ext-existing".into(), cached.clone());
        let error = register_model(State(state.clone()), Json(registration_request(None)))
            .await.err().unwrap();
        assert_eq!(error.0, StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(state.registry.get("mdl-ext-existing").unwrap().value(), &cached);
        server.abort();
    }

    #[tokio::test]
    async fn new_model_registration_does_not_upsert_over_a_concurrent_creator() {
        let app = Router::new().route("/dbs/test/colls/metadata/docs", post(
            |headers: axum::http::HeaderMap| async move {
                if headers.contains_key("x-ms-documentdb-isquery") {
                    Json(json!({"Documents":[]})).into_response()
                } else {
                    assert!(!headers.contains_key("x-ms-documentdb-is-upsert"));
                    assert!(!headers.contains_key("if-match"));
                    StatusCode::CONFLICT.into_response()
                }
            },
        ));
        let (state, server) = test_state(app).await;
        let error = register_model(State(state.clone()), Json(registration_request(Some("test-key"))))
            .await.err().unwrap();
        assert_eq!(error.0, StatusCode::SERVICE_UNAVAILABLE);
        assert!(state.registry.is_empty());
        server.abort();
    }

    #[tokio::test]
    async fn standalone_model_registration_keeps_stable_ids_and_preserves_credentials() {
        let (mut state, server) = test_state(Router::new()).await;
        state.cosmos = None;
        for key in [Some("initial-test-key"), None, Some(""), Some("rotated-test-key")] {
            let mut request = registration_request(key);
            request.id = None;
            register_model(State(state.clone()), Json(request)).await
                .unwrap_or_else(|_| panic!("Standalone registration failed"));
            assert_eq!(state.registry.len(), 1);
            let mut entry = state.registry.iter_mut().next().unwrap();
            assert_eq!(entry["api_key"], if key == Some("rotated-test-key") {
                "rotated-test-key"
            } else {
                "initial-test-key"
            });
            if key != Some("initial-test-key") {
                assert_eq!(entry["custom"], "retained");
            }
            entry["custom"] = json!("retained");
        }
        server.abort();
    }

    async fn test_state(app: Router) -> (AppState, tokio::task::JoinHandle<()>) {
        let cache = TOKEN_CACHE.get_or_init(|| RwLock::new((String::new(), std::time::Instant::now())));
        *cache.write().await = ("local-test-token".into(), std::time::Instant::now());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let endpoint = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        (AppState {
            registry: Arc::new(DashMap::new()),
            pipelines: Arc::new(DashMap::new()),
            native_urls: Arc::new(HashMap::new()),
            http: Client::builder().timeout(Duration::from_secs(5)).build().unwrap(),
            k8s: None,
            namespace: "test".into(),
            cosmos: Some(CosmosConfig {
                endpoint: endpoint.clone(), database: "test".into(), container: "metadata".into(), api_key: String::new(),
            }),
            mock_1536_single: Arc::new(String::new()),
            mock_128_single: Arc::new(String::new()),
            controller_url: endpoint.clone(),
            pipeline_worker_url: endpoint,
            model_health: Arc::new(DashMap::new()),
            last_health_check: Arc::new(RwLock::new(None)),
            metadata_lock: Arc::new(Mutex::new(())),
        }, server)
    }

    #[tokio::test]
    async fn failed_registry_writes_and_deletes_do_not_change_memory_or_report_success() {
        let app = Router::new()
            .route("/dbs/test/colls/metadata/docs", post(|| async { StatusCode::SERVICE_UNAVAILABLE }))
            .route("/dbs/test/colls/metadata/docs/{id}", delete(|| async { StatusCode::SERVICE_UNAVAILABLE }));
        let (state, server) = test_state(app).await;
        let original = json!({"id":"trp-existing", "doc_type":"docgrok_pipeline", "model_id":"mdl-ext-old"});
        state.pipelines.insert("trp-existing".into(), original.clone());
        let changed = json!({"id":"trp-existing", "doc_type":"docgrok_pipeline", "model_id":"mdl-ext-new"});
        assert_eq!(save_registry_document(&state, &state.pipelines, &changed).await.err().unwrap().0, StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(state.pipelines.get("trp-existing").unwrap().value(), &original);
        assert_eq!(remove_registry_document(&state, &state.pipelines, "trp-existing", "docgrok_pipeline").await.err().unwrap().0, StatusCode::SERVICE_UNAVAILABLE);
        assert!(state.pipelines.contains_key("trp-existing"));
        let request: PipelineRequest = serde_json::from_value(json!({"name":"cannot-persist", "model_id":"mdl-ext-new"})).unwrap();
        assert_eq!(create_pipeline(State(state.clone()), Json(request)).await.err().unwrap().0, StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(state.pipelines.len(), 1);
        assert_eq!(list_registry_models(State(state.clone())).await.err().unwrap().0, StatusCode::SERVICE_UNAVAILABLE);
        server.abort();
    }

    #[tokio::test]
    async fn registry_load_reads_every_page_and_retains_last_good_snapshot_on_failure() {
        let fail_second = Arc::new(AtomicBool::new(false));
        let failure = fail_second.clone();
        let app = Router::new().route("/dbs/test/colls/metadata/docs", post(move |headers: axum::http::HeaderMap| {
            let failure = failure.clone();
            async move {
                if headers.contains_key("x-ms-continuation") {
                    if failure.load(Ordering::SeqCst) {
                        return StatusCode::SERVICE_UNAVAILABLE.into_response();
                    }
                    Json(json!({"Documents":[{"id":"mdl-second","doc_type":"docgrok_model"}]})).into_response()
                } else {
                    ([("x-ms-continuation", "second")], Json(json!({
                        "Documents":[{"id":"mdl-first","doc_type":"docgrok_model"}]
                    }))).into_response()
                }
            }
        }));
        let (state, server) = test_state(app).await;
        load_models_from_cosmos(&state).await.unwrap();
        assert!(state.registry.contains_key("mdl-first") && state.registry.contains_key("mdl-second"));
        fail_second.store(true, Ordering::SeqCst);
        state.registry.insert("mdl-retained".into(), json!({"id":"mdl-retained"}));
        assert!(load_models_from_cosmos(&state).await.is_err());
        assert_eq!(state.registry.len(), 3);
        server.abort();
    }

    #[tokio::test]
    async fn malformed_registry_response_cannot_clear_a_working_cache() {
        let app = Router::new().route("/dbs/test/colls/metadata/docs", post(|| async {
            Json(json!({"unexpected":"response"}))
        }));
        let (state, server) = test_state(app).await;
        state.registry.insert("mdl-existing".into(), json!({"id":"mdl-existing"}));
        assert!(load_models_from_cosmos(&state).await.is_err());
        assert!(state.registry.contains_key("mdl-existing"));
        server.abort();
    }

    #[tokio::test]
    async fn repeated_continuation_fails_without_losing_cached_models() {
        let app = Router::new().route("/dbs/test/colls/metadata/docs", post(|| async {
            ([("x-ms-continuation", "repeated")], Json(json!({"Documents":[]})))
        }));
        let (state, server) = test_state(app).await;
        state.registry.insert("mdl-existing".into(), json!({"id":"mdl-existing"}));
        assert!(load_models_from_cosmos(&state).await.is_err());
        assert!(state.registry.contains_key("mdl-existing"));
        server.abort();
    }

    #[tokio::test]
    async fn durable_models_and_pipelines_reload_and_reach_the_file_worker_on_a_fresh_replica() {
        let stored = Arc::new(Mutex::new(HashMap::<String, Value>::new()));
        let posted = stored.clone();
        let deleted = stored.clone();
        let app = Router::new()
            .route("/dbs/test/colls/metadata/docs", post(move |headers: axum::http::HeaderMap, Json(body): Json<Value>| {
                let stored = posted.clone();
                async move {
                    let mut docs = stored.lock().await;
                    if headers.contains_key("x-ms-documentdb-isquery") {
                        let kind = body["parameters"][0]["value"].as_str().unwrap();
                        Json(json!({"Documents":docs.values().filter(|doc| doc["doc_type"] == kind).cloned().collect::<Vec<_>>()})).into_response()
                    } else {
                        docs.insert(body["id"].as_str().unwrap().to_owned(), body);
                        StatusCode::CREATED.into_response()
                    }
                }
            }))
            .route("/dbs/test/colls/metadata/docs/{id}", delete(move |Path(id): Path<String>| {
                let stored = deleted.clone();
                async move {
                    stored.lock().await.remove(&id);
                    StatusCode::NO_CONTENT
                }
            }))
            .route("/process", post(|Json(body): Json<Value>| async move {
                assert_eq!(body["model_id"], "mdl-ext-durable");
                Json(json!({"model_id":body["model_id"], "chunks":[]}))
            }));
        let (state, server) = test_state(app).await;
        let model = json!({"id":"mdl-ext-durable","doc_type":"docgrok_model","name":"durable"});
        let pipeline = json!({"id":"trp-durable","doc_type":"docgrok_pipeline","name":"files","model_id":"mdl-ext-durable"});
        save_registry_document(&state, &state.registry, &model).await.unwrap_or_else(|_| panic!("Model persistence failed"));
        save_registry_document(&state, &state.pipelines, &pipeline).await.unwrap_or_else(|_| panic!("Pipeline persistence failed"));

        let mut replica = state.clone();
        replica.registry = Arc::new(DashMap::new());
        replica.pipelines = Arc::new(DashMap::new());
        replica.metadata_lock = Arc::new(Mutex::new(()));
        let request = serde_json::from_value(json!({"pipeline":"trp-durable","blob_name":"demo.txt"})).unwrap();
        let response = handle_embed(State(replica.clone()), Json(request)).await
            .unwrap_or_else(|_| panic!("File request did not recover persisted model routing")).into_response();
        assert_eq!(response.status(), StatusCode::OK);
        initialize_registry(&replica).await.unwrap();
        assert_eq!(replica.registry.get("mdl-ext-durable").unwrap().value(), &model);
        assert_eq!(replica.pipelines.get("trp-durable").unwrap().value(), &pipeline);

        remove_registry_document(&state, &state.registry, "mdl-ext-durable", "docgrok_model").await
            .unwrap_or_else(|_| panic!("Model deletion failed"));
        load_models_from_cosmos(&replica).await.unwrap();
        assert!(!replica.registry.contains_key("mdl-ext-durable"));
        assert!(stored.lock().await.contains_key("trp-durable"));
        server.abort();
    }
}

#[cfg(test)]
mod request_body_tests {
    use super::*;

    #[test]
    fn file_and_transform_requests_resolve_the_pipeline_embedding_model() {
        let pipelines = DashMap::new();
        pipelines.insert("trp-demo".to_owned(), json!({"model_id": "mdl-ext-demo"}));
        for field in ["blob_name", "blobUrl", "data", "transform_name"] {
            let mut input = json!({"pipeline": "trp-demo"});
            input[field] = json!("demo.txt");
            let req: EmbedRequest = serde_json::from_value(input).unwrap();
            assert_eq!(request_model_id(&req, &pipelines).as_deref(), Some("mdl-ext-demo"));
        }
    }

    #[test]
    fn explicit_model_overrides_pipeline_and_model_free_transforms_remain_valid() {
        let pipelines = DashMap::new();
        pipelines.insert("trp-demo".to_owned(), json!({"model_id": "mdl-ext-default"}));
        let req: EmbedRequest = serde_json::from_value(json!({
            "pipeline": "trp-demo", "model_id": "mdl-ext-override", "blob_name": "demo.txt"
        })).unwrap();
        assert_eq!(request_model_id(&req, &pipelines).as_deref(), Some("mdl-ext-override"));
        let req: EmbedRequest = serde_json::from_value(json!({
            "blob_name": "image.png", "transform_name": "image"
        })).unwrap();
        assert!(request_model_id(&req, &pipelines).is_none());
    }

    #[test]
    fn pipeline_model_aliases_and_model_steps_resolve_consistently() {
        for config in [
            json!({"model_id": "mdl-ext-demo"}),
            json!({"model": "mdl-ext-demo"}),
            json!({"steps": [{"type": "extract"}, {"type": "model", "model_id": "mdl-ext-demo"}]}),
            json!({"steps": [{"type": "model", "model": "mdl-ext-demo"}]}),
            json!({"steps": [{"model": "mdl-ext-demo"}]}),
        ] {
            assert_eq!(pipeline_model_id(&config), Some("mdl-ext-demo"));
        }
        assert!(pipeline_model_id(&json!({"steps": [{"type": "image_embed"}]})).is_none());
    }

    #[tokio::test]
    async fn request_body_limits_allow_base64_maximum_and_reject_overflow() {
        let app = with_request_body_limits(
            Router::new().route("/process", post(|body: Bytes| async move {
                Json(json!({ "bytes": body.len() }))
            })),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}/process", listener.local_addr().unwrap());
        let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let client = Client::new();
        // Also exceeds Axum's default 2 MiB extractor cap.
        let base64_length = 4 * ((50 * 1024 * 1024 + 2) / 3);
        let payload = json!({ "data": "A".repeat(base64_length), "source_name": "file.txt" }).to_string();
        assert!(payload.len() < MAX_REQUEST_BODY_BYTES);
        let response = client.post(&url).body(payload).send().await.unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let response = client.post(&url).body(vec![b'A'; MAX_REQUEST_BODY_BYTES]).send().await.unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        // Send only headers for an oversized request. Sending the entire body
        // can race the early 413 with a connection reset on Windows.
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let address = url.strip_prefix("http://").unwrap().strip_suffix("/process").unwrap();
        let mut connection = tokio::net::TcpStream::connect(address).await.unwrap();
        let headers = format!(
            "POST /process HTTP/1.1\r\nHost: localhost\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
            MAX_REQUEST_BODY_BYTES + 1
        );
        connection.write_all(headers.as_bytes()).await.unwrap();
        let mut response = [0u8; 4096];
        let length = tokio::time::timeout(Duration::from_secs(5), connection.read(&mut response))
            .await.unwrap().unwrap();
        assert!(String::from_utf8_lossy(&response[..length]).starts_with("HTTP/1.1 413"));
        server.abort();
    }
}
