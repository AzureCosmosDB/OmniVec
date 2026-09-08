"""Opt-in live Cosmos chunk probe: execute via kubectl exec -i API -- python3 - < script.

Arguments: --endpoint https://TEST.documents.azure.com:443/ --model mdl-existing
--prefix unique-synthetic-name [--database testdb]. Never targets metadata storage.
Creates and retains synthetic containers/registrations; pauses its pipelines on exit.
Admin authentication is read only inside the pod and sent only to loopback.
"""
import argparse
import hashlib
import json
import math
import os
import time

import requests
from azure.cosmos import CosmosClient, PartitionKey
from azure.identity import DefaultAzureCredential
from chunker import chunk_text, make_chunk_doc_id


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--database", default="testdb")
    parser.add_argument("--chunk-size", type=int, default=600)
    parser.add_argument("--chunk-overlap", type=int, default=80)
    parser.add_argument("--chunk-unit", choices=["chars", "tokens"], default="chars")
    parser.add_argument("--doc-id-pattern", default="{source}-chunk-{chunk}")
    args = parser.parse_args()
    assert args.prefix.startswith("pr183-chunks-"), "Use an isolated synthetic pr183-chunks-* prefix"
    assert args.database == "testdb", "This probe is limited to the synthetic test database"
    assert args.endpoint.rstrip("/") == "https://omnivec-test-v3fapuy5j6s7c.documents.azure.com:443", "PR183 test account only"
    api = requests.Session()
    api.headers["Authorization"] = "Bearer " + os.environ["OMNIVEC_ADMIN_TOKEN"]
    api.headers["Content-Type"] = "application/json"

    def call(method, path, body=None, expected=200):
        response = api.request(method, "http://127.0.0.1:8080/api/" + path, json=body, timeout=180)
        if response.status_code != expected:
            # Responses may contain connection configuration; print status/path only.
            raise AssertionError(f"{method} {path}: HTTP {response.status_code}, expected {expected}")
        return response.json()

    credential = DefaultAzureCredential()
    client = CosmosClient(args.endpoint, credential=credential)
    db = client.get_database_client(args.database)
    source_name, dest_name = args.prefix + "-source", args.prefix + "-vectors"
    db.create_container_if_not_exists(id=source_name, partition_key=PartitionKey(path="/id"))
    db.create_container_if_not_exists(id=dest_name, partition_key=PartitionKey(path="/id"),
        vector_embedding_policy={"vectorEmbeddings": [
            {"path": "/embedding", "dataType": "float32", "dimensions": 1536, "distanceFunction": "cosine"}]},
        indexing_policy={"indexingMode": "consistent", "automatic": True,
            "includedPaths": [{"path": "/*"}], "excludedPaths": [{"path": '/"embedding"/*'}],
            "vectorIndexes": [{"path": "/embedding", "type": "quantizedFlat"}]})
    source = db.get_container_client(source_name)
    vectors = db.get_container_client(dest_name)
    connection = {"endpoint": args.endpoint, "database": args.database, "auth_type": "managed-identity",
                  "client_id": os.environ.get("AZURE_CLIENT_ID", "")}

    def register(kind, name, body):
        existing = [x for x in call("GET", kind)[kind] if x["name"] == name]
        if existing:
            return existing[0]["id"]
        return call("POST", kind, {"name": name, **body})[kind[:-1]]["id"]

    sid = register("sources", source_name, {"type": "cosmosdb", "config": {**connection, "container": source_name}})
    did = register("destinations", dest_name, {"type": "cosmosdb-vector",
        "config": {**connection, "container": dest_name, "vector_dimensions": 1536}})
    chunk = {"chunk_size": args.chunk_size, "chunk_overlap": args.chunk_overlap,
             "chunk_unit": args.chunk_unit, "store_text": True,
             "text_field": "text", "doc_id_pattern": args.doc_id_pattern}
    body = {"sources": [{"source_id": sid, "content_fields": ["content"], "content_mode": "field"}],
            "destination_id": did, "docgrok_pipeline": args.model, "vector_index_path": "embedding",
            "processing_mode": "queue", "process_existing": True, "content_strategy": "chunk",
            "chunk_config": chunk, "metadata_fields": []}
    call("POST", "pipelines", {"name": args.prefix + "-reject-inline", **body, "processing_mode": "inline"}, expected=400)
    for invalid_pattern in ("same-name-for-every-chunk", "{source}-{pipeline}", "{unknown}-{chunk}"):
        rejected = call("POST", "pipelines", {"name": args.prefix + "-reject-pattern", **body,
            "chunk_config": {**chunk, "doc_id_pattern": invalid_pattern}}, expected=400)
        assert "doc_id_pattern" in rejected.get("detail", ""), "Template rejection must explain the invalid setting"
    pids = [register("pipelines", args.prefix + suffix, body) for suffix in ("-primary", "-isolation")]
    print(json.dumps({"fixtures": {"source": sid, "destination": did, "pipelines": pids,
                                  "source_container": source_name, "vector_container": dest_name}}), flush=True)

    def rows(pid):
        return list(vectors.query_items(
            "SELECT * FROM c WHERE c.pipeline_id=@p AND c.source_id=@s AND c.source_ref='synthetic-long'",
            parameters=[{"name": "@p", "value": pid}, {"name": "@s", "value": sid}],
            enable_cross_partition_query=True))

    def wait_rows(pid, predicate, label):
        deadline = time.monotonic() + 420
        while time.monotonic() < deadline:
            found = rows(pid)
            if predicate(found):
                return found
            time.sleep(5)
        raise AssertionError(f"Timed out: {label}; observed {len(found)} chunks")

    def validate(found):
        assert found and len({x["id"] for x in found}) == len(found)
        assert sorted(x["chunk_index"] for x in found) == list(range(len(found)))
        assert all(x["chunk_count"] == len(found) and x.get("text") for x in found)
        assert all(len(x["embedding"]) == 1536 and all(math.isfinite(v) for v in x["embedding"])
                   and any(x["embedding"]) for x in found)
        assert all("content" not in x and x["chunk_source_partition"] == "synthetic-long" for x in found)
        assert all(x["source_ref"] == "synthetic-long" and x["id"] != x["source_ref"] for x in found)
        for document in found:
            rendered = make_chunk_doc_id(document["pipeline_id"], document["source_ref"],
                document["chunk_index"], args.doc_id_pattern)
            assert document["id"].endswith("-" + rendered), "Configured chunk-name template was not honored"

    topics = [
        "The cobalt otter observatory measures the aurora above polar ice. Its unique instrument is a violet spectrometer.",
        "Azure Kubernetes Service orchestrates container scheduling and rolling application deployments using node pools.",
        "Coral reef restoration uses marine nurseries and temperature monitoring to protect ocean biodiversity.",
        "A sourdough bakery ferments rye flour overnight and controls hydration to bake a crisp fragrant loaf.",
        "A lunar rover samples basalt and maps craters using solar powered navigation and geological sensors.",
        "Classical orchestras rehearse symphonies with violin, clarinet, cello and precise musical dynamics.",
    ]
    text = "\n\n".join(" ".join(f"{topic} Observation {i}." for i in range(8)) for topic in topics)
    expected_chunks = chunk_text(text, args.chunk_size, args.chunk_overlap, args.chunk_unit)
    original = {"id": "synthetic-long", "content": text, "marker": args.prefix}
    try:
        for pid in pids:
            call("POST", f"pipelines/{pid}/pause")
        source.upsert_item(original)
        before = source.read_item("synthetic-long", partition_key="synthetic-long")
        for pid in pids:
            call("POST", f"pipelines/{pid}/run")
        initial = []
        for pid in pids:
            found = wait_rows(pid, lambda r: len(r) > 2 and all(x.get("chunk_count") == len(r) for x in r), "long document")
            validate(found)
            assert [x["text"] for x in sorted(found, key=lambda x: x["chunk_index"])] == [
                part for part, _ in expected_chunks], "Worker differs from existing Python chunk strategy"
            assert len({tuple(x["embedding"]) for x in found}) == len(found), "Expected distinct chunk vectors"
            initial.append(found)
        assert set(x["id"] for x in initial[0]).isdisjoint(x["id"] for x in initial[1])
        after = source.read_item("synthetic-long", partition_key="synthetic-long")
        assert after == before, "Chunking mutated the source"
        search = call("POST", "playground/search",
            {"query": "cobalt otter observatory violet spectrometer polar aurora", "destination_ids": [did], "top_k": 3})
        results = search["results"]
        by_id = {x["id"]: x for group in initial for x in group}
        assert results and results[0]["id"] in by_id
        assert "observatory" in by_id[results[0]["id"]]["text"], "Search did not retrieve the expected chunk"
        print(json.dumps({"long_verified": True, "source_chars": len(text),
            "chunks_per_pipeline": [len(r) for r in initial], "dimensions": 1536,
            "distinct_finite_vectors": True, "source_unchanged": True,
            "chunk_config": chunk, "python_chunk_strategy_matches": True,
            "configured_template_verified": True, "invalid_templates_rejected": True,
            "search_top_id": results[0]["id"]}), flush=True)

        # Pause the independent pipeline: primary cleanup must leave its vectors unchanged.
        call("POST", f"pipelines/{pids[1]}/pause")
        time.sleep(45)
        isolation_before = rows(pids[1])
        short = "The cobalt otter observatory now uses one violet spectrometer. This is the shorter revised source."
        original["content"] = short
        source.upsert_item(original)
        updated_source = source.read_item("synthetic-long", partition_key="synthetic-long")
        shrunk = wait_rows(pids[0], lambda r: len(r) == 1 and r[0].get("text") == short, "shrink cleanup")
        validate(shrunk)
        assert shrunk[0]["id"] == min(initial[0], key=lambda x: x["chunk_index"])["id"]
        assert shrunk[0]["embedding"] != initial[0][0]["embedding"]
        assert rows(pids[1]) == isolation_before, "Cleanup touched the other pipeline"
        assert source.read_item("synthetic-long", partition_key="synthetic-long") == updated_source
        print(json.dumps({"shrink_verified": True, "remaining_chunks": 1,
            "obsolete_removed": len(initial[0]) - 1, "deterministic_id": shrunk[0]["id"],
            "other_pipeline_untouched": True}), flush=True)

        original["content"] = ""
        source.upsert_item(original)
        wait_rows(pids[0], lambda r: not r, "empty text cleanup")
        assert rows(pids[1]) == isolation_before
        original["content"] = short
        source.upsert_item(original)
        restored = wait_rows(pids[0], lambda r: len(r) == 1 and r[0].get("text") == short, "restore")
        validate(restored)
        print(json.dumps({"empty_cleanup_verified": True, "restored_chunks": 1,
            "content_sha256": hashlib.sha256(short.encode()).hexdigest(), "PASS": True}), flush=True)
    finally:
        for pid in pids:
            call("POST", f"pipelines/{pid}/pause")
        client.close()
        credential.close()


if __name__ == "__main__":
    main()
