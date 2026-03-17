use reqwest::Client;
use serde::{Deserialize, Serialize};
use std::env;
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::time;

#[derive(Serialize)]
struct EmbeddingRequest {
    input: Vec<String>,
}

// Only deserialize usage — skip the huge embedding vectors to save memory
#[derive(Deserialize)]
struct UsageOnly {
    usage: Usage,
}

#[derive(Deserialize)]
struct Usage {
    total_tokens: i64,
}

// Full response only for warmup (to get dims)
#[derive(Deserialize)]
struct WarmupResponse {
    data: Vec<WarmupData>,
    usage: Usage,
}

#[derive(Deserialize)]
struct WarmupData {
    embedding: Vec<f32>,
}

struct Stats {
    total_tokens: AtomicI64,
    total_batches: AtomicU64,
    total_errors: AtomicU64,
    total_429s: AtomicU64,
    total_retries: AtomicU64,
    total_latency_ms: AtomicU64,
    min_latency_ms: AtomicU64,
    max_latency_ms: AtomicU64,
}

impl Stats {
    fn new() -> Self {
        Self {
            total_tokens: AtomicI64::new(0),
            total_batches: AtomicU64::new(0),
            total_errors: AtomicU64::new(0),
            total_429s: AtomicU64::new(0),
            total_retries: AtomicU64::new(0),
            total_latency_ms: AtomicU64::new(0),
            min_latency_ms: AtomicU64::new(u64::MAX),
            max_latency_ms: AtomicU64::new(0),
        }
    }

    fn update_latency(&self, ms: u64) {
        self.total_latency_ms.fetch_add(ms, Ordering::Relaxed);
        let mut current = self.min_latency_ms.load(Ordering::Relaxed);
        while ms < current {
            match self.min_latency_ms.compare_exchange_weak(current, ms, Ordering::Relaxed, Ordering::Relaxed) {
                Ok(_) => break,
                Err(c) => current = c,
            }
        }
        current = self.max_latency_ms.load(Ordering::Relaxed);
        while ms > current {
            match self.max_latency_ms.compare_exchange_weak(current, ms, Ordering::Relaxed, Ordering::Relaxed) {
                Ok(_) => break,
                Err(c) => current = c,
            }
        }
    }
}

/// Fixed worker: loops sending batches until stop signal
async fn worker(
    client: Client,
    url: String,
    api_key: String,
    body: Arc<serde_json::Value>,
    stats: Arc<Stats>,
    stop: Arc<AtomicBool>,
) {
    while !stop.load(Ordering::Relaxed) {
        let max_retries = 5u32;
        let mut succeeded = false;
        for attempt in 0..=max_retries {
            if stop.load(Ordering::Relaxed) {
                return;
            }

            let start = Instant::now();
            let result = client
                .post(&url)
                .header("api-key", &api_key)
                .json(body.as_ref())
                .send()
                .await;

            match result {
                Ok(resp) => {
                    let status = resp.status().as_u16();
                    let latency_ms = start.elapsed().as_millis() as u64;

                    if status == 200 {
                        // Read body as bytes, parse only usage field
                        match resp.bytes().await {
                            Ok(bytes) => {
                                if let Ok(data) = serde_json::from_slice::<UsageOnly>(&bytes) {
                                    stats.total_tokens.fetch_add(data.usage.total_tokens, Ordering::Relaxed);
                                    stats.total_batches.fetch_add(1, Ordering::Relaxed);
                                    stats.update_latency(latency_ms);
                                    succeeded = true;
                                } else {
                                    stats.total_errors.fetch_add(1, Ordering::Relaxed);
                                    stats.total_batches.fetch_add(1, Ordering::Relaxed);
                                    succeeded = true;
                                }
                            }
                            Err(_) => {
                                stats.total_errors.fetch_add(1, Ordering::Relaxed);
                                stats.total_batches.fetch_add(1, Ordering::Relaxed);
                                succeeded = true;
                            }
                        }
                        break;
                    } else if status == 429 {
                        stats.total_429s.fetch_add(1, Ordering::Relaxed);
                        stats.total_retries.fetch_add(1, Ordering::Relaxed);
                        // Consume body to release connection
                        let _ = resp.bytes().await;

                        let retry_after = 1u64 << attempt.min(4);
                        if attempt < max_retries {
                            time::sleep(Duration::from_secs(retry_after)).await;
                            continue;
                        }
                        stats.total_errors.fetch_add(1, Ordering::Relaxed);
                        stats.total_batches.fetch_add(1, Ordering::Relaxed);
                        succeeded = true;
                        break;
                    } else {
                        let _ = resp.bytes().await;
                        stats.total_errors.fetch_add(1, Ordering::Relaxed);
                        stats.total_batches.fetch_add(1, Ordering::Relaxed);
                        succeeded = true;
                        break;
                    }
                }
                Err(_) => {
                    stats.total_errors.fetch_add(1, Ordering::Relaxed);
                    stats.total_batches.fetch_add(1, Ordering::Relaxed);
                    succeeded = true;
                    break;
                }
            }
        }
        if !succeeded {
            // All retries exhausted without recording
            stats.total_errors.fetch_add(1, Ordering::Relaxed);
            stats.total_batches.fetch_add(1, Ordering::Relaxed);
        }
    }
}

#[tokio::main]
async fn main() {
    let endpoint = env::var("AZURE_OPENAI_ENDPOINT").unwrap_or_default();
    let api_key = env::var("AZURE_OPENAI_KEY").unwrap_or_default();
    let deployment = env::var("AZURE_OPENAI_DEPLOYMENT")
        .unwrap_or_else(|_| "text-embedding-3-small".to_string());

    if endpoint.is_empty() || api_key.is_empty() {
        eprintln!("Usage: AZURE_OPENAI_ENDPOINT=... AZURE_OPENAI_KEY=... bench-embedding-rs");
        eprintln!("Optional: AZURE_OPENAI_DEPLOYMENT (default: text-embedding-3-small)");
        eprintln!("Optional: CONCURRENCY (default: 50)");
        eprintln!("Optional: BATCH_SIZE (default: 100)");
        eprintln!("Optional: DURATION_SEC (default: 60)");
        std::process::exit(1);
    }

    let concurrency: usize = env::var("CONCURRENCY")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(50);
    let batch_size: usize = env::var("BATCH_SIZE")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(100);
    let duration_sec: u64 = env::var("DURATION_SEC")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(60);
    let target_tpm: i64 = 7_000_000;

    let url = format!(
        "{}/openai/deployments/{}/embeddings?api-version=2024-02-01",
        endpoint.trim_end_matches('/'),
        deployment
    );

    // Build batch payload as pre-serialized JSON value
    let texts: Vec<String> = (0..batch_size)
        .map(|i| format!(
            "Document {}: Lorem ipsum dolor sit amet, consectetur adipiscing elit. \
             Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. \
             Ut enim ad minim veniam.", i
        ))
        .collect();
    let body_value = serde_json::json!({"input": texts});

    // HTTP client
    let client = Client::builder()
        .timeout(Duration::from_secs(30))
        .pool_max_idle_per_host(concurrency)
        .pool_idle_timeout(Duration::from_secs(90))
        .build()
        .expect("Failed to build HTTP client");

    // Warmup — full deserialization to get dims
    println!("=== Warmup ===");
    let start = Instant::now();
    let resp = client
        .post(&url)
        .header("api-key", &api_key)
        .json(&body_value)
        .send()
        .await
        .expect("Warmup request failed");

    if resp.status() != 200 {
        let status = resp.status();
        let text = resp.text().await.unwrap_or_default();
        eprintln!("Warmup failed: HTTP {}: {:.500}", status, text);
        std::process::exit(1);
    }

    let warmup_ms = start.elapsed().as_millis();
    let warmup_data: WarmupResponse = resp.json().await.expect("Failed to parse warmup response");
    let tokens_per_batch = warmup_data.usage.total_tokens;
    let dims = warmup_data.data.first().map(|d| d.embedding.len()).unwrap_or(0);
    println!("  OK, tokens/batch: {}, dims: {}, latency: {}ms", tokens_per_batch, dims, warmup_ms);
    // Drop warmup data immediately
    drop(warmup_data);

    let batches_per_sec = target_tpm as f64 / 60.0 / tokens_per_batch as f64;

    println!("\n=== Config ===");
    println!("  Concurrency:       {}", concurrency);
    println!("  Batch size:        {} texts", batch_size);
    println!("  Duration:          {}s", duration_sec);
    println!("  Tokens/batch:      {}", tokens_per_batch);
    println!("  Target:            {}M TPM", target_tpm / 1_000_000);
    println!("  Batches needed/s:  {:.1}", batches_per_sec);

    println!("\n=== Sustained throughput test ({}s) ===", duration_sec);

    let stats = Arc::new(Stats::new());
    let stop = Arc::new(AtomicBool::new(false));
    let body = Arc::new(body_value);
    let test_start = Instant::now();

    // Spawn fixed worker pool — exactly `concurrency` workers, no unbounded allocation
    let mut handles = Vec::with_capacity(concurrency);
    for _ in 0..concurrency {
        let c = client.clone();
        let u = url.clone();
        let k = api_key.clone();
        let b = Arc::clone(&body);
        let s = Arc::clone(&stats);
        let st = Arc::clone(&stop);
        handles.push(tokio::spawn(worker(c, u, k, b, s, st)));
    }

    // Reporter
    let reporter_stats = Arc::clone(&stats);
    let reporter_stop = Arc::clone(&stop);
    let reporter = tokio::spawn(async move {
        let mut interval = time::interval(Duration::from_secs(10));
        interval.tick().await;
        loop {
            interval.tick().await;
            if reporter_stop.load(Ordering::Relaxed) {
                return;
            }
            let elapsed = test_start.elapsed().as_secs_f64();
            let tokens = reporter_stats.total_tokens.load(Ordering::Relaxed);
            let batches = reporter_stats.total_batches.load(Ordering::Relaxed);
            let errors = reporter_stats.total_errors.load(Ordering::Relaxed);
            let retries = reporter_stats.total_retries.load(Ordering::Relaxed);
            let tpm = tokens as f64 / elapsed * 60.0;
            println!(
                "  [{:5.0}s] {} batches, {} tokens, {:.0} TPM ({:.1}M), errors={}, 429s={}",
                elapsed, batches, tokens, tpm, tpm / 1_000_000.0, errors, retries
            );
        }
    });

    // Wait for duration then stop
    time::sleep(Duration::from_secs(duration_sec)).await;
    stop.store(true, Ordering::Relaxed);

    // Wait for workers to finish current request
    for h in handles {
        let _ = h.await;
    }
    reporter.abort();

    let elapsed = test_start.elapsed().as_secs_f64();
    let tokens = stats.total_tokens.load(Ordering::Relaxed);
    let batches = stats.total_batches.load(Ordering::Relaxed);
    let errors = stats.total_errors.load(Ordering::Relaxed);
    let retries = stats.total_retries.load(Ordering::Relaxed);
    let total_429s = stats.total_429s.load(Ordering::Relaxed);
    let tpm = tokens as f64 / elapsed * 60.0;
    let tps = tokens as f64 / elapsed;
    let successful = batches - errors;
    let avg_latency = if successful > 0 {
        stats.total_latency_ms.load(Ordering::Relaxed) as f64 / successful as f64
    } else {
        0.0
    };
    let min_lat = stats.min_latency_ms.load(Ordering::Relaxed);
    let max_lat = stats.max_latency_ms.load(Ordering::Relaxed);

    println!("\n=== Results ===");
    println!("  Duration:      {:.1}s", elapsed);
    println!("  Batches:       {} ({:.1}/sec)", batches, batches as f64 / elapsed);
    println!("  Tokens:        {}", tokens);
    println!("  TPM:           {:.0} ({:.1}M tokens/min)", tpm, tpm / 1_000_000.0);
    println!("  TPS:           {:.0} tokens/sec", tps);
    println!(
        "  Latency:       avg={:.0}ms, min={}ms, max={}ms",
        avg_latency,
        if min_lat == u64::MAX { 0 } else { min_lat },
        max_lat
    );
    println!(
        "  Errors:        {} ({:.1}%)",
        errors,
        if batches > 0 { errors as f64 / batches as f64 * 100.0 } else { 0.0 }
    );
    println!("  429 retries:   {}", retries);
    println!("  Total 429s:    {}", total_429s);
    println!("  Target:        {}M TPM", target_tpm / 1_000_000);
    println!("  Achieved:      {:.1}% of target", tpm / target_tpm as f64 * 100.0);
}
