"""Test Azure AI Foundry text-embedding-3-small at 7M tokens/min sustained throughput."""
import os
import sys
import time
import concurrent.futures
import requests

# Configuration
ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
API_KEY = os.environ.get("AZURE_OPENAI_KEY", "")
DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "text-embedding-3-small")
API_VERSION = "2024-02-01"

# 7M tokens/min = ~116,667 tokens/sec
TARGET_TPM = 7_000_000
TARGET_TPS = TARGET_TPM / 60  # ~116,667 tokens/sec

# Each ~20-word sentence ≈ 25 tokens. 100 texts per batch = ~2,500 tokens/batch.
# To hit 116K tokens/sec we need ~47 batches/sec of 100 texts.
# Use concurrent requests to sustain this.
BATCH_SIZE = 100
TEXT_TEMPLATE = "Document {i}: Lorem ipsum dolor sit amet, consectetur adipiscing elit. Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam."
# ~30 tokens per text × 100 = ~3,000 tokens per batch
TOKENS_PER_TEXT = 30  # approximate
TOKENS_PER_BATCH = BATCH_SIZE * TOKENS_PER_TEXT

# Test duration
TEST_DURATION_SEC = 60
CONCURRENT_REQUESTS = 50  # parallel requests to saturate the endpoint

if not ENDPOINT or not API_KEY:
    ENDPOINT = input("Enter Azure OpenAI endpoint (e.g. https://xxx.openai.azure.com/): ").strip()
    API_KEY = input("Enter API key: ").strip()
    dep = input(f"Enter deployment name [{DEPLOYMENT}]: ").strip()
    if dep:
        DEPLOYMENT = dep

url = f"{ENDPOINT.rstrip('/')}/openai/deployments/{DEPLOYMENT}/embeddings?api-version={API_VERSION}"
headers = {"Content-Type": "application/json", "api-key": API_KEY}

# --- Warmup: verify endpoint works ---
print("=== Warmup ===")
payload = {"input": "test"}
resp = requests.post(url, json=payload, headers=headers)
if resp.status_code != 200:
    print(f"ERROR {resp.status_code}: {resp.text}")
    sys.exit(1)
dims = len(resp.json()["data"][0]["embedding"])
print(f"  Endpoint OK, model dimensions: {dims}")

# --- Build batch payload ---
texts = [TEXT_TEMPLATE.format(i=i) for i in range(BATCH_SIZE)]
batch_payload = {"input": texts}

# --- Single batch timing ---
print("\n=== Single batch baseline ===")
start = time.time()
resp = requests.post(url, json=batch_payload, headers=headers)
baseline_ms = (time.time() - start) * 1000
data = resp.json()
actual_tokens = data.get("usage", {}).get("total_tokens", TOKENS_PER_BATCH)
actual_tpb = actual_tokens  # tokens per batch (actual)
print(f"  {BATCH_SIZE} texts, {actual_tokens} tokens, {baseline_ms:.0f}ms")
print(f"  Single-request rate: {actual_tokens / (baseline_ms / 1000):.0f} tokens/sec")

# Recalculate with actual token count
TOKENS_PER_BATCH = actual_tokens
batches_needed_per_sec = TARGET_TPS / TOKENS_PER_BATCH
print(f"\n  Target: {TARGET_TPM:,} tokens/min = {TARGET_TPS:,.0f} tokens/sec")
print(f"  Tokens/batch: {TOKENS_PER_BATCH}")
print(f"  Batches needed/sec: {batches_needed_per_sec:.1f}")
print(f"  Concurrent requests: {CONCURRENT_REQUESTS}")

# --- Sustained throughput test ---
print(f"\n=== Sustained throughput test ({TEST_DURATION_SEC}s) ===")

session = requests.Session()
session.headers.update(headers)

total_tokens = 0
total_batches = 0
total_errors = 0
error_codes = {}
lock = __import__('threading').Lock()


def send_batch():
    """Send one batch and return (tokens, error_code_or_none)."""
    try:
        resp = session.post(url, json=batch_payload, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("usage", {}).get("total_tokens", TOKENS_PER_BATCH), None
        else:
            return 0, resp.status_code
    except Exception as e:
        return 0, str(e)[:50]


start = time.time()
last_report = start

with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENT_REQUESTS) as executor:
    # Keep submitting batches for TEST_DURATION_SEC
    futures = set()

    while True:
        elapsed = time.time() - start
        if elapsed >= TEST_DURATION_SEC and len(futures) == 0:
            break

        # Submit new batches if under duration
        while len(futures) < CONCURRENT_REQUESTS and (time.time() - start) < TEST_DURATION_SEC:
            futures.add(executor.submit(send_batch))

        # Collect completed futures
        done, futures = concurrent.futures.wait(futures, timeout=0.1, return_when=concurrent.futures.FIRST_COMPLETED)
        for f in done:
            tokens, err = f.result()
            total_tokens += tokens
            total_batches += 1
            if err:
                total_errors += 1
                error_codes[err] = error_codes.get(err, 0) + 1

        # Report every 10 seconds
        now = time.time()
        if now - last_report >= 10:
            elapsed = now - start
            tpm = (total_tokens / elapsed) * 60
            print(f"  [{elapsed:5.0f}s] {total_batches:,} batches, {total_tokens:,} tokens, "
                  f"{tpm:,.0f} TPM ({tpm/1_000_000:.1f}M), errors={total_errors}")
            last_report = now

# Wait for remaining futures
if futures:
    done, _ = concurrent.futures.wait(futures, timeout=30)
    for f in done:
        tokens, err = f.result()
        total_tokens += tokens
        total_batches += 1
        if err:
            total_errors += 1
            error_codes[err] = error_codes.get(err, 0) + 1

elapsed = time.time() - start
tpm = (total_tokens / elapsed) * 60
tps = total_tokens / elapsed

print(f"\n=== Results ===")
print(f"  Duration:    {elapsed:.1f}s")
print(f"  Batches:     {total_batches:,} ({total_batches / elapsed:.1f}/sec)")
print(f"  Tokens:      {total_tokens:,}")
print(f"  TPM:         {tpm:,.0f} ({tpm/1_000_000:.1f}M tokens/min)")
print(f"  TPS:         {tps:,.0f} tokens/sec")
print(f"  Errors:      {total_errors} ({100*total_errors/max(total_batches,1):.1f}%)")
if error_codes:
    print(f"  Error breakdown: {error_codes}")
print(f"  Target:      {TARGET_TPM:,} TPM ({TARGET_TPM/1_000_000:.0f}M)")
print(f"  Achieved:    {tpm/TARGET_TPM*100:.1f}% of target")
