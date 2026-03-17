# Architecture Code Review - Reliability Issues

## Critical Issues (Must Fix)

### 1. Memory Explosion - Job Refs Cache
**File:** `blob_backfill_worker.py:178-179, 283-298`
**Problem:** Loads ALL existing job refs into memory before enumeration
**Impact:** For 100M documents, this will OOM the worker
**Fix:**
```python
# Instead of loading all refs, check existence per-blob
async def _job_exists(self, blob_ref: str) -> bool:
    job_id = _job_id(self.source_id, blob_ref)
    try:
        self.store.get(job_id, partition_key="job")
        return True
    except:
        return False
```
Or use bloom filter for probabilistic check.

---

### 2. Job ID Collision Across Pipelines
**File:** `blob_backfill_worker.py:222`
**Problem:** Job ID = `hash(source_id + blob_ref)` - doesn't include pipeline_id
**Impact:** If source has multiple pipelines, jobs overwrite each other
**Fix:**
```python
def _job_id(source_id: str, blob_path: str, pipeline_id: str) -> str:
    return f"job-{hashlib.md5(f'{source_id}:{pipeline_id}:{blob_path}'.encode()).hexdigest()[:16]}"
```

---

### 3. Race Condition on First Checkpoint Save
**File:** `checkpoint_manager.py:107-113`
**Problem:** First save uses `upsert()` without etag check
**Impact:** Two workers starting simultaneously will overwrite each other's checkpoint
**Fix:**
```python
# Always try create first, fall back to update
try:
    store.create(checkpoint)  # Will fail if exists
except AlreadyExists:
    self.load()  # Get current etag
    store.replace_with_etag(checkpoint, self._current_etag)
```

---

### 4. Lease Race Condition (Leader Election)
**File:** `leader_election.py:177-182, 235-240`
**Problem:** Read-modify-write without resourceVersion
**Impact:** Two pods could both become leader simultaneously
**Fix:**
```python
# Use resourceVersion for optimistic concurrency
lease.metadata.resource_version = existing_lease.metadata.resource_version
await self._coord_api.replace_namespaced_lease(...)
```

---

### 5. Standalone Mode When K8S Unavailable
**File:** `leader_election.py:93-102`
**Problem:** If K8S API temporarily fails, pod runs as standalone leader
**Impact:** Multiple pods could all think they're leader
**Fix:**
```python
# Don't assume leadership, exit or wait
if not K8S_AVAILABLE:
    logger.error("K8S required for leader election - exiting")
    sys.exit(1)
```

---

## High Priority Issues

### 6. Checkpoint Saved with Old Continuation Token
**File:** `blob_backfill_worker.py:240-246`
**Problem:** Checkpoint saves `continuation_token` before updating to `next_token` (line 260)
**Impact:** If crash after checkpoint but before page processing, will re-fetch same page
**Fix:** Save checkpoint AFTER moving to next_token or don't save mid-page.

---

### 7. Stuck Jobs Stay in PROCESSING for 5 Minutes
**File:** `blob_backfill_worker.py:347-351`
**Problem:** If batch fails, jobs remain PROCESSING until stuck job recovery (60s check, 300s timeout)
**Impact:** Up to 5 minutes delay before retry
**Fix:**
```python
# On batch failure, immediately reset failed jobs
except Exception as e:
    for job in jobs:
        self._reset_job_to_pending(job)
```

---

### 8. Stuck Job Recovery Race Condition
**File:** `blob_backfill_worker.py:397-401`
**Problem:** Uses `upsert()` without etag - could reset a job that just completed
**Impact:** Job marked failed after successful completion
**Fix:**
```python
# Use etag to ensure job is still in PROCESSING state
etag = doc.get("_etag")
doc["status"] = "pending"
try:
    self.store.replace_with_etag(doc, etag)
except ConflictError:
    pass  # Job was updated, skip
```

---

### 9. No Graceful Shutdown
**File:** `blob_backfill_worker.py:122-125`
**Problem:** Setting `_running = False` doesn't interrupt asyncio.gather
**Impact:** Worker won't stop until current long operation completes
**Fix:**
```python
async def stop(self):
    self._running = False
    # Cancel all running tasks
    for task in self._tasks:
        task.cancel()
```

---

### 10. Exception Handling Too Broad
**File:** `checkpoint_manager.py:60-64`
**Problem:** Checks for "NotFound" in exception string
**Impact:** Could miss actual errors or match false positives
**Fix:**
```python
from azure.cosmos.exceptions import CosmosResourceNotFoundError
try:
    ...
except CosmosResourceNotFoundError:
    return None
```

---

## Medium Priority Issues

### 11. Pipeline Refresh During Long Enumeration
**File:** `blob_backfill_worker.py:92-95`
**Problem:** Pipelines loaded once at startup
**Impact:** New pipelines added during enumeration won't be processed
**Fix:** Periodically reload pipelines (every 5 minutes)

---

### 12. Progress Tracker Not Atomic
**File:** `progress_tracker.py:55-130`
**Problem:** Read-modify-write without etag
**Impact:** Concurrent updates could lose data
**Fix:** Use etag-based updates like CheckpointManager

---

### 13. Leader Election Gives Up Too Easily
**File:** `leader_election.py:128-133`
**Problem:** Any exception causes leadership loss
**Impact:** Transient network error causes unnecessary failover
**Fix:**
```python
# Only give up after multiple consecutive failures
self._consecutive_failures += 1
if self._consecutive_failures > 3:
    self._is_leader = False
```

---

### 14. No Jitter in Retry Intervals
**File:** `leader_election.py:136`
**Problem:** All replicas retry at exactly same interval
**Impact:** Thundering herd on lease contention
**Fix:**
```python
import random
jitter = random.uniform(0, self.retry_period * 0.2)
await asyncio.sleep(self.retry_period + jitter)
```

---

### 15. HTTP Client Not Closed on Error
**File:** `blob_backfill_worker.py:113-120`
**Problem:** If exception in gather, finally block runs but tasks may not be cancelled
**Impact:** Resource leak
**Fix:** Use context manager or explicit cleanup

---

## Low Priority Issues

### 16. Extra Read After Checkpoint Save
**File:** `checkpoint_manager.py:115-117`
**Problem:** Reads back document just to get etag
**Impact:** Extra latency and RU cost
**Fix:** CosmosDB returns etag in response headers - capture it

---

### 17. Unused renew_deadline_seconds
**File:** `leader_election.py:40`
**Problem:** Parameter defined but never used
**Impact:** Misleading API
**Fix:** Implement or remove

---

### 18. MD5 for Job ID
**File:** `blob_backfill_worker.py:63`
**Problem:** MD5 is cryptographically broken
**Impact:** Theoretical collision risk (very low in practice)
**Fix:** Use SHA256 and truncate

---

## Recommended Fixes Priority

1. **CRITICAL (Before Production):**
   - #1 Memory explosion
   - #2 Job ID collision
   - #3 Checkpoint race condition
   - #4 Leader election race
   - #5 Standalone mode

2. **HIGH (Within 1 Week):**
   - #6 Checkpoint ordering
   - #7 Stuck job delay
   - #8 Stuck job race
   - #9 Graceful shutdown
   - #10 Exception handling

3. **MEDIUM (Within 2 Weeks):**
   - #11 Pipeline refresh
   - #12 Progress atomic
   - #13 Leader retry
   - #14 Jitter

4. **LOW (Nice to Have):**
   - #15-18
