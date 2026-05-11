use clap::Parser;
use reqwest::Client;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Instant;
use tokio::sync::Semaphore;

#[derive(Parser)]
#[command(name = "docgrok-bench")]
struct Args {
    /// DocGrok router URL
    #[arg(long, default_value = "http://docgrok-router:80")]
    url: String,

    /// Pipeline name
    #[arg(long, default_value = "mock-1536")]
    pipeline: String,

    /// Texts per batch
    #[arg(long, default_value_t = 500)]
    batch: usize,

    /// Max concurrent requests
    #[arg(long, default_value_t = 500)]
    concurrency: usize,

    /// Total documents to process
    #[arg(long, default_value_t = 2_000_000)]
    total: usize,
}

#[tokio::main]
async fn main() {
    let args = Args::parse();
    let total_batches = args.total / args.batch;

    // Build payload once — use model_id for "mdl-*", pipeline for everything else
    let texts: Vec<String> = (0..args.batch).map(|i| format!("t{i}")).collect();
    let payload = if args.pipeline.starts_with("mdl-") {
        serde_json::json!({
            "model_id": args.pipeline,
            "texts": texts,
        })
    } else {
        serde_json::json!({
            "pipeline": args.pipeline,
            "texts": texts,
        })
    };
    let payload_bytes = serde_json::to_vec(&payload).unwrap();
    let payload_arc = Arc::new(payload_bytes);

    let client = Client::builder()
        .gzip(true)
        .pool_max_idle_per_host(args.concurrency)
        .pool_idle_timeout(std::time::Duration::from_secs(30))
        .timeout(std::time::Duration::from_secs(120))
        .build()
        .unwrap();

    let completed = Arc::new(AtomicU64::new(0));
    let errors = Arc::new(AtomicU64::new(0));
    let bytes_recv = Arc::new(AtomicU64::new(0));
    let sem = Arc::new(Semaphore::new(args.concurrency));

    println!("=== DocGrok Bench (Rust) ===");
    println!(
        "URL:         {}\nPipeline:    {}\nBatch:       {}\nConcurrency: {}\nTotal:       {} ({} batches)\n",
        args.url, args.pipeline, args.batch, args.concurrency, args.total, total_batches
    );

    let start = Instant::now();
    let url = format!("{}/embed/batch", args.url);

    let mut handles = Vec::with_capacity(total_batches);
    for _ in 0..total_batches {
        let permit = sem.clone().acquire_owned().await.unwrap();
        let client = client.clone();
        let url = url.clone();
        let payload = payload_arc.clone();
        let completed = completed.clone();
        let errors = errors.clone();
        let bytes_recv = bytes_recv.clone();

        handles.push(tokio::spawn(async move {
            let resp = client
                .post(&url)
                .header("Content-Type", "application/json")
                .body(payload.as_ref().clone())
                .send()
                .await;

            match resp {
                Ok(r) if r.status().is_success() => {
                    let body = r.bytes().await.unwrap_or_default();
                    bytes_recv.fetch_add(body.len() as u64, Ordering::Relaxed);
                    completed.fetch_add(1, Ordering::Relaxed);
                }
                Ok(r) => {
                    let _ = r.bytes().await;
                    errors.fetch_add(1, Ordering::Relaxed);
                }
                Err(_) => {
                    errors.fetch_add(1, Ordering::Relaxed);
                }
            }
            drop(permit);
        }));
    }

    // Progress reporter
    let comp_prog = completed.clone();
    let err_prog = errors.clone();
    let batch_size = args.batch;
    let reporter = tokio::spawn(async move {
        let mut last = 0u64;
        loop {
            tokio::time::sleep(std::time::Duration::from_secs(2)).await;
            let c = comp_prog.load(Ordering::Relaxed);
            let e = err_prog.load(Ordering::Relaxed);
            if c + e >= total_batches as u64 {
                break;
            }
            if c != last {
                let elapsed = start.elapsed().as_secs_f64();
                let rate = (c as f64 * batch_size as f64) / elapsed;
                println!(
                    "  {}/{} batches — {:.0} docs/sec (err={})",
                    c, total_batches, rate, e
                );
                last = c;
            }
        }
    });

    for h in handles {
        let _ = h.await;
    }
    reporter.abort();

    let elapsed = start.elapsed().as_secs_f64();
    let c = completed.load(Ordering::Relaxed);
    let e = errors.load(Ordering::Relaxed);
    let docs = c * args.batch as u64;
    let rate = docs as f64 / elapsed;
    let bytes = bytes_recv.load(Ordering::Relaxed);

    println!("\n=== Results ===");
    println!("Completed: {}/{} batches ({} docs)", c, total_batches, docs);
    println!("Errors:    {}", e);
    println!("Elapsed:   {:.2}s", elapsed);
    println!("Throughput: {:.0} docs/sec", rate);
    println!("Data recv: {:.1} MB ({:.1} MB/s)", bytes as f64 / 1e6, bytes as f64 / 1e6 / elapsed);
}
