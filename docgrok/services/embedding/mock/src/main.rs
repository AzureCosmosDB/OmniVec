use axum::{
    extract::{DefaultBodyLimit, Json},
    http::{header, HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Router,
};
use serde::Deserialize;
use serde_json::json;
use std::time::Duration;

const MEDIA: &str = "application/vnd.omnivec.embeddings.f32";
const MAX_VALUES: usize = 1_048_576;

#[derive(Deserialize)]
#[serde(untagged)]
enum Input {
    Single(String),
    Batch(Vec<String>),
}

#[derive(Deserialize)]
struct EmbeddingRequest {
    input: Input,
    dimensions: u32,
    #[serde(default)]
    latency_ms: f64,
}

async fn embeddings(headers: HeaderMap, Json(request): Json<EmbeddingRequest>) -> Response {
    let count = match &request.input {
        Input::Single(text) => {
            let _ = text.len();
            1
        }
        Input::Batch(texts) => texts.len(),
    };
    if count == 0 {
        return (StatusCode::BAD_REQUEST, Json(json!({"detail": "Input must be non-empty"}))).into_response();
    }
    if !(1..=65536).contains(&request.dimensions)
        || !request.latency_ms.is_finite()
        || !(0.0..=60000.0).contains(&request.latency_ms)
    {
        return (StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({"detail": "Dimensions must be 1..65536; latency_ms must be 0..60000"}))).into_response();
    }
    let dimensions = request.dimensions as usize;
    if count > 2048 || count * dimensions > MAX_VALUES {
        return (StatusCode::PAYLOAD_TOO_LARGE,
            Json(json!({"detail": "Batch exceeds count or 4 MiB vector limit"}))).into_response();
    }
    if request.latency_ms > 0.0 {
        tokio::time::sleep(Duration::from_secs_f64(request.latency_ms / 1000.0)).await;
    }
    let value = (1.0 / (dimensions as f64).sqrt()) as f32;
    let binary = headers.get(header::ACCEPT).and_then(|v| v.to_str().ok())
        .is_some_and(|accept| accept.split(',').any(|part| part.split(';').next()
            .unwrap_or("").trim().eq_ignore_ascii_case(MEDIA)));
    if binary {
        let mut body = Vec::with_capacity(16 + count * dimensions * 4);
        body.extend_from_slice(b"OVEC");
        body.extend_from_slice(&1u16.to_le_bytes());
        body.extend_from_slice(&0u16.to_le_bytes());
        body.extend_from_slice(&(count as u32).to_le_bytes());
        body.extend_from_slice(&request.dimensions.to_le_bytes());
        for _ in 0..count * dimensions {
            body.extend_from_slice(&value.to_le_bytes());
        }
        return ([
            (header::CONTENT_TYPE, MEDIA),
            (header::CONTENT_ENCODING, "identity"),
        ], body).into_response();
    }
    let vector = vec![value; dimensions];
    let data: Vec<_> = (0..count).map(|index| json!({
        "object": "embedding", "index": index, "embedding": vector,
    })).collect();
    Json(json!({"object": "list", "data": data, "model": "mock-embedding",
        "usage": {"synthetic": true}, "real_embeddings": false})).into_response()
}

fn app() -> Router {
    Router::new()
        .route("/health", get(|| async { Json(json!({
            "status": "healthy", "model": "mock-embedding", "synthetic": true,
            "implementation": "rust", "gpu_inference": false,
        })) }))
        .route("/embeddings", post(embeddings))
        .layer(DefaultBodyLimit::max(70 * 1024 * 1024))
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .init();
    let listener = tokio::net::TcpListener::bind("0.0.0.0:8000").await
        .expect("Cannot bind mock embedding service");
    tracing::info!("Rust synthetic embedding model listening on port 8000");
    axum::serve(listener, app()).await.expect("Mock embedding server failed");
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request(count: usize, dimensions: u32, latency_ms: f64) -> EmbeddingRequest {
        EmbeddingRequest { input: Input::Batch(vec!["test".into(); count]), dimensions, latency_ms }
    }

    #[tokio::test]
    async fn full_binary_payload_has_exact_shape_and_finite_nonzero_values() {
        let mut headers = HeaderMap::new();
        headers.insert(header::ACCEPT, MEDIA.parse().unwrap());
        let response = embeddings(headers, Json(request(1024, 1024, 0.0))).await;
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(response.headers()[header::CONTENT_TYPE], MEDIA);
        let body = axum::body::to_bytes(response.into_body(), MAX_VALUES * 4 + 16).await.unwrap();
        assert_eq!(body.len(), 4_194_320);
        assert_eq!(&body[..4], b"OVEC");
        assert_eq!(&body[4..8], &[1, 0, 0, 0]);
        assert_eq!(u32::from_le_bytes(body[8..12].try_into().unwrap()), 1024);
        assert_eq!(u32::from_le_bytes(body[12..16].try_into().unwrap()), 1024);
        for bytes in body[16..].chunks_exact(4) {
            assert_eq!(f32::from_le_bytes(bytes.try_into().unwrap()), 0.03125);
        }
    }

    #[tokio::test]
    async fn json_and_binary_return_the_same_vectors() {
        let response = embeddings(HeaderMap::new(), Json(request(2, 4, 0.0))).await;
        let body = axum::body::to_bytes(response.into_body(), 4096).await.unwrap();
        let result: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(result["data"][0]["index"], 0);
        assert_eq!(result["data"][1]["index"], 1);
        for entry in result["data"].as_array().unwrap() {
            assert_eq!(entry["embedding"], json!([0.5, 0.5, 0.5, 0.5]));
        }
    }

    #[tokio::test]
    async fn invalid_configuration_and_oversized_batches_fail_explicitly() {
        for req in [request(1, 0, 0.0), request(1, 65537, 0.0),
            request(1, 4, -1.0), request(1, 4, 60001.0), request(1, 4, f64::NAN)] {
            assert_eq!(embeddings(HeaderMap::new(), Json(req)).await.status(),
                StatusCode::UNPROCESSABLE_ENTITY);
        }
        for req in [request(2049, 1, 0.0), request(1025, 1024, 0.0)] {
            assert_eq!(embeddings(HeaderMap::new(), Json(req)).await.status(), StatusCode::PAYLOAD_TOO_LARGE);
        }
        assert_eq!(embeddings(HeaderMap::new(), Json(request(0, 4, 0.0))).await.status(), StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn latency_is_awaited_before_the_response() {
        let started = std::time::Instant::now();
        assert_eq!(embeddings(HeaderMap::new(), Json(request(1, 4, 25.0))).await.status(), StatusCode::OK);
        assert!(started.elapsed() >= Duration::from_millis(25));
    }

    #[test]
    fn malformed_inputs_cannot_deserialize() {
        for value in [json!({"input": [1], "dimensions": 4}),
            json!({"input": "t", "dimensions": true}),
            json!({"input": "t", "dimensions": -1}),
            json!({"input": "t", "dimensions": 1.5})] {
            assert!(serde_json::from_value::<EmbeddingRequest>(value).is_err());
        }
    }
}
