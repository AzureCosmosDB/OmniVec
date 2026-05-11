use axum::{
    body::{Body, Bytes},
    extract::{Json, Path, Query, Request, State},
    http::{header, Method, StatusCode, Uri},
    response::{IntoResponse, Response},
    routing::{delete, get, post, put},
    Router,
};
use dashmap::DashMap;
use reqwest::Client;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{collections::HashMap, env, sync::Arc, time::Duration};
use tokio::sync::RwLock;
use tower_http::{cors::CorsLayer, compression::CompressionLayer, limit::RequestBodyLimitLayer};
use tracing::{error, info, warn};

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
    };

    // Load models from CosmosDB on startup
    if let Err(e) = load_models_from_cosmos(&state).await {
        warn!("Failed to load models from CosmosDB: {e}");
    }

    // Load pipelines from CosmosDB
    if let Err(e) = load_pipelines_from_cosmos(&state).await {
        warn!("Failed to load pipelines from CosmosDB: {e}");
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
            .layer(RequestBodyLimitLayer::new(50 * 1024 * 1024)) // 50 MB limit for large PDFs
            .layer(CompressionLayer::new().gzip(true))
            .layer(CorsLayer::permissive())
            .with_state(state);

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

    let resp = state
        .http
        .post(&url)
        .header("Authorization", format!("type=aad&ver=1.0&sig={token}"))
        .header("Content-Type", "application/query+json")
        .header("x-ms-version", "2020-07-15")
        .header("x-ms-documentdb-isquery", "true")
        .header("x-ms-documentdb-query-enablecrosspartition", "true")
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("CosmosDB request failed: {e}"))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let text = resp.text().await.unwrap_or_default();
        return Err(format!("CosmosDB query failed ({status}): {text}"));
    }

    let data: Value = resp
        .json()
        .await
        .map_err(|e| format!("Failed to parse CosmosDB response: {e}"))?;
    Ok(data["Documents"].as_array().cloned().unwrap_or_default())
}

async fn cosmos_upsert(state: &AppState, doc: &Value) -> Result<(), String> {
    let cosmos = state.cosmos.as_ref().ok_or("No CosmosDB config")?;
    let url = format!(
        "{}/dbs/{}/colls/{}/docs",
        cosmos.endpoint, cosmos.database, cosmos.container
    );

    let token = get_cosmos_token(state).await?;
    let doc_type = doc["doc_type"].as_str().unwrap_or("unknown");

    let resp = state
        .http
        .post(&url)
        .header("Authorization", format!("type=aad&ver=1.0&sig={token}"))
        .header("Content-Type", "application/json")
        .header("x-ms-version", "2020-07-15")
        .header("x-ms-documentdb-is-upsert", "true")
        .header("x-ms-documentdb-partitionkey", format!("[\"{doc_type}\"]"))
        .json(doc)
        .send()
        .await
        .map_err(|e| format!("CosmosDB upsert failed: {e}"))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let text = resp.text().await.unwrap_or_default();
        return Err(format!("CosmosDB upsert failed ({status}): {text}"));
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
    if state.cosmos.is_none() {
        return Ok(());
    }
    let docs =
        cosmos_query(state, "SELECT * FROM c WHERE c.doc_type = 'docgrok_model'", &[]).await?;
    let count = docs.len();
    for doc in docs {
        if let Some(id) = doc["id"].as_str() {
            state.registry.insert(id.to_string(), doc);
        }
    }
    info!("Loaded {count} external models from CosmosDB");
    Ok(())
}

async fn load_pipelines_from_cosmos(state: &AppState) -> Result<(), String> {
    if state.cosmos.is_none() {
        return Ok(());
    }
    let docs = cosmos_query(
        state,
        "SELECT * FROM c WHERE c.doc_type = 'docgrok_pipeline'",
        &[],
    )
    .await?;
    let count = docs.len();
    for doc in docs {
        if let Some(id) = doc["id"].as_str() {
            state.pipelines.insert(id.to_string(), doc);
        }
    }
    info!("Loaded {count} pipelines from CosmosDB");
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

async fn handle_embed(
    State(state): State<AppState>,
    Json(req): Json<EmbedRequest>,
) -> Result<impl IntoResponse, AppError> {
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
            "model_id": req.model_id,
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
            "model_id": req.model_id,
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
            let pipeline_model_id = cfg["model_id"].as_str()
                .or_else(|| cfg["model"].as_str())
                .or_else(|| {
                    cfg["steps"].as_array().and_then(|steps| {
                        steps.iter().find_map(|s| {
                            if s["type"].as_str() == Some("model") {
                                s["model_id"].as_str().or_else(|| s["model"].as_str())
                            } else {
                                None
                            }
                        })
                    })
                });

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
        let pipeline_cfg = state.pipelines.get(pipeline.as_str()).map(|v| v.value().clone());
        if let Some(cfg) = pipeline_cfg {
            let resolved_model = cfg["model_id"].as_str()
                .or_else(|| cfg["model"].as_str())
                .or_else(|| cfg["steps"][0]["model"].as_str());
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

async fn list_registry_models(State(state): State<AppState>) -> impl IntoResponse {
    let mut models = Vec::new();

    // Native models from K8s — only deployments with omnivec/role=model label
    if let Some(k8s) = &state.k8s {
        let api: kube::Api<k8s_openapi::api::apps::v1::Deployment> =
            kube::Api::namespaced(k8s.clone(), &state.namespace);
        let lp = kube::api::ListParams::default().labels("omnivec/role=model");
        if let Ok(list) = api.list(&lp).await {
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

    Json(json!({"models": models}))
}

async fn register_model(
    State(state): State<AppState>,
    Json(req): Json<RegisterModelRequest>,
) -> Result<impl IntoResponse, AppError> {
    let model_id = req.id.unwrap_or_else(|| {
        let hash = format!("{:x}", md5_hash(&req.name));
        format!("mdl-ext-{}", &hash[..8])
    });

    let cfg = json!({
        "id": model_id,
        "doc_type": "docgrok_model",
        "name": req.name,
        "type": req.model_type,
        "endpoint": req.endpoint,
        "deployment": if req.deployment.is_empty() { req.name.clone() } else { req.deployment },
        "api_key": req.api_key,
        "api_version": if req.api_version.is_empty() { "2024-06-01".to_string() } else { req.api_version },
        "embedding_dim": req.embedding_dim,
    });

    state.registry.insert(model_id.clone(), cfg.clone());

    // Persist to CosmosDB
    if let Err(e) = cosmos_upsert(&state, &cfg).await {
        warn!("Failed to persist model to CosmosDB: {e}");
    }

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
    if state.registry.remove(&model_id).is_none() {
        return Err(AppError(
            StatusCode::NOT_FOUND,
            format!("Model '{model_id}' not found"),
        ));
    }

    if let Err(e) = cosmos_delete(&state, &model_id, "docgrok_model").await {
        warn!("Failed to delete model from CosmosDB: {e}");
    }

    info!("Deleted model: {model_id}");
    Ok(Json(json!({"deleted": model_id})))
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

async fn list_pipelines(State(state): State<AppState>) -> impl IntoResponse {
    let pipelines: Vec<Value> = state.pipelines.iter().map(|e| e.value().clone()).collect();
    Json(json!({"pipelines": pipelines}))
}

async fn get_pipeline(
    State(state): State<AppState>,
    Path(name): Path<String>,
) -> Result<impl IntoResponse, AppError> {
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

    state.pipelines.insert(pipeline_id.clone(), doc.clone());

    if let Err(e) = cosmos_upsert(&state, &doc).await {
        warn!("Failed to persist pipeline: {e}");
    }

    Ok((StatusCode::CREATED, Json(json!({"id": pipeline_id, "name": name}))))
}

async fn update_pipeline(
    State(state): State<AppState>,
    Path(id): Path<String>,
    Json(req): Json<PipelineRequest>,
) -> Result<impl IntoResponse, AppError> {
    if !state.pipelines.contains_key(&id) {
        return Err(AppError(
            StatusCode::NOT_FOUND,
            format!("Pipeline '{id}' not found"),
        ));
    }

    let mut doc = req.config.clone();
    doc["id"] = json!(id);
    doc["doc_type"] = json!("docgrok_pipeline");

    state.pipelines.insert(id.clone(), doc.clone());

    if let Err(e) = cosmos_upsert(&state, &doc).await {
        warn!("Failed to persist pipeline: {e}");
    }

    Ok(Json(json!({"id": id})))
}

async fn delete_pipeline(
    State(state): State<AppState>,
    Path(name): Path<String>,
) -> Result<impl IntoResponse, AppError> {
    if state.pipelines.remove(&name).is_none() {
        return Err(AppError(
            StatusCode::NOT_FOUND,
            format!("Pipeline '{name}' not found"),
        ));
    }

    if let Err(e) = cosmos_delete(&state, &name, "docgrok_pipeline").await {
        warn!("Failed to delete pipeline from CosmosDB: {e}");
    }

    Ok(Json(json!({"deleted": name})))
}

async fn reset_pipeline(
    State(state): State<AppState>,
    Path(name): Path<String>,
) -> Result<impl IntoResponse, AppError> {
    if let Some(mut entry) = state.pipelines.get_mut(&name) {
        entry.value_mut()["reset_at"] = json!(chrono::Utc::now().to_rfc3339());
        let doc = entry.value().clone();
        drop(entry);
        if let Err(e) = cosmos_upsert(&state, &doc).await {
            warn!("Failed to persist pipeline reset: {e}");
        }
        Ok(Json(json!({"name": name, "reset": true})))
    } else {
        Err(AppError(
            StatusCode::NOT_FOUND,
            format!("Pipeline '{name}' not found"),
        ))
    }
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

    // Check external models from registry
    for item in state.registry.iter() {
        let (model_id, cfg) = item.pair();
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
