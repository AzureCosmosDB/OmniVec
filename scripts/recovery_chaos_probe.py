"""PR183 pod-local probes. Credentials never leave the API pod or enter output."""

import argparse
import base64
import collections
import hashlib
import json
import math
import os
import time
import urllib.request


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def valid_vector(vector):
    return (
        isinstance(vector, list)
        and len(vector) == 1536
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in vector)
        and any(vector)
    )


def snapshot(rows):
    return sorted(
        [
            row["id"],
            row["source_ref"],
            row.get("content_hash"),
            hashlib.sha256(json.dumps(row["embedding"]).encode()).hexdigest(),
        ]
        for row in rows
    )


def validate_rows(rows, config, synthetic=None):
    expected = set(config["expected_refs"])
    if synthetic:
        expected.add(synthetic)
    refs = collections.Counter(row.get("source_ref") for row in rows)
    require(set(refs) == expected, "Unexpected document set")
    require(all(count == 1 for count in refs.values()), "Duplicate small-document chunks")
    require(len({row["id"] for row in rows}) == len(rows), "Duplicate vector identities")
    require(all(row.get("source_id") == config["source_id"] for row in rows), "Source identity changed")
    require(all(valid_vector(row.get("embedding")) for row in rows), "Invalid real model embeddings")


def wait_until(check, timeout, sleep=time.sleep, clock=time.monotonic):
    deadline = clock() + timeout
    while True:
        result = check()
        if result is not None:
            return result
        require(clock() < deadline, "Probe deadline exceeded")
        sleep(min(3, max(0, deadline - clock())))


class Probe:
    def __init__(self, payload):
        from azure.cosmos import CosmosClient
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient

        self.payload = payload
        self.c = payload["config"]
        self.run_id = payload["run_id"]
        require(len(self.run_id) == 32 and all(c in "0123456789abcdef" for c in self.run_id), "Invalid ownership")
        self.name = "pr183-chaos-" + self.run_id + ".txt"
        self.credential = DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID"))
        self.storage = BlobServiceClient(
            "https://" + self.c["blob_account"] + ".blob.core.windows.net",
            credential=self.credential, connection_timeout=10, read_timeout=15, retry_total=0,
        )
        client = CosmosClient(
            "https://" + self.c["cosmos_account"] + ".documents.azure.com:443/",
            credential=self.credential, connection_timeout=10, read_timeout=15, retry_total=0,
        )
        self.container = client.get_database_client(self.c["cosmos_database"]).get_container_client(self.c["cosmos_container"])

    def request(self, path, body=None, router=False):
        base = "http://docgrok" if router else "http://127.0.0.1:8080"
        headers = {"Content-Type": "application/json"}
        if not router:
            headers["Authorization"] = "Bearer " + os.environ["OMNIVEC_ADMIN_TOKEN"]
        request = urllib.request.Request(
            base + path, headers=headers,
            data=None if body is None else json.dumps(body).encode(),
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)

    def rows(self):
        # Include the whole pipeline so an accidental source change cannot hide extra rows.
        return list(self.container.query_items(
            "SELECT * FROM c WHERE c.pipeline_id = @pipeline",
            parameters=[{"name": "@pipeline", "value": self.c["pipeline_id"]}],
            enable_cross_partition_query=True,
        ))

    def registry(self):
        c = self.c
        pipe = self.request("/api/pipelines/" + c["pipeline_id"])
        require(pipe["destination_id"] == c["destination_id"], "Destination identity changed")
        require([s["source_id"] for s in pipe["sources"]] == [c["source_id"]], "Pipeline source changed")
        require(pipe["docgrok_pipeline"] == c["route_ids"][0] and pipe["status"] == "active", "Pipeline unavailable")
        source = self.request("/api/sources/" + c["source_id"])
        dest = self.request("/api/destinations/" + c["destination_id"])
        require(source["type"] == "azure-blob" and source.get("enabled", True), "Wrong source type")
        require(source["config"]["account_url"].rstrip("/") ==
                "https://" + c["blob_account"] + ".blob.core.windows.net", "Wrong Blob account")
        require(source["config"]["container"] == c["blob_container"], "Wrong Blob container")
        require(dest["type"] == "cosmosdb-vector" and dest.get("enabled", True), "Wrong destination type")
        require(dest["config"]["endpoint"].rstrip("/") ==
                "https://" + c["cosmos_account"] + ".documents.azure.com:443", "Wrong Cosmos account")
        require(dest["config"]["database"] == c["cosmos_database"] and
                dest["config"]["container"] == c["cosmos_container"], "Wrong Cosmos scope")
        api_models = self.request("/api/models")["models"]
        router_models = self.request("/admin/models/registry", router=True)["models"]
        for models in (api_models, router_models):
            require(sum(m["id"] == c["model_id"] for m in models) == 1, "Model missing or duplicated")
        routes = self.request("/admin/pipelines", router=True)["pipelines"]
        for route_id in c["route_ids"]:
            found = [r for r in routes if r["id"] == route_id]
            require(len(found) == 1 and found[0]["model_id"] == c["model_id"], "Route identity changed")
        vector = self.request("/embed/batch", {
            "model_id": c["model_id"], "texts": ["Synthetic bounded chaos real embedding probe."],
        }, router=True)["outputs"][0][0]
        require(valid_vector(vector), "Registered model embedding failed")
        return {"model": c["model_id"], "routes": c["route_ids"], "dimensions": 1536}

    def baseline(self):
        identity = self.registry()
        rows = self.rows()
        validate_rows(rows, self.c)
        require(not self.storage.get_blob_client(self.c["blob_container"], self.name).exists(), "Fixture already exists")
        self.search()
        return {"snapshot": snapshot(rows), "identity": identity, "documents": len(rows)}

    def upload(self):
        blob = self.storage.get_blob_client(self.c["blob_container"], self.name)
        blob.upload_blob(
            ("Synthetic isolated PR183 outage fixture " + self.run_id +
             ". Queued indexing must recover with a finite real embedding.").encode(),
            overwrite=False, metadata={"pr183_chaos_run": self.run_id},
        )
        return {"owned_blob": self.name}

    def queued(self):
        from azure.servicebus import ServiceBusClient

        with ServiceBusClient(
            self.c["servicebus_namespace"] + ".servicebus.windows.net", self.credential,
            retry_total=0,
        ) as client:
            with client.get_subscription_receiver(
                topic_name=self.c["topic"], subscription_name=self.c["subscription_name"],
                socket_timeout=10,
            ) as receiver:
                def check():
                    # Peek only: never receive, settle, replay, purge or inspect a DLQ.
                    start = None
                    for _ in range(10):
                        messages = receiver.peek_messages(max_message_count=100, sequence_number=start, timeout=10)
                        for message in messages:
                            try:
                                body = json.loads(str(message))
                            except (ValueError, TypeError):
                                continue
                            if not isinstance(body, dict):
                                continue
                            if (body.get("source_ref") == self.name and
                                    body.get("pipeline_id") == self.c["pipeline_id"] and
                                    body.get("source_id") == self.c["source_id"]):
                                require(not any(row["source_ref"] == self.name for row in self.rows()),
                                        "Fixture processed before outage observation")
                                return {"owned_message_peeked": True, "processing_absent_during_outage": True}
                        if len(messages) < 100:
                            break
                        start = messages[-1].sequence_number + 1
                    return None
                return wait_until(check, 60)

    def verify(self, deleted=False):
        def check():
            rows = self.rows()
            expected = set(self.c["expected_refs"]) | (set() if deleted else {self.name})
            if {row.get("source_ref") for row in rows} != expected:
                return None
            validate_rows(rows, self.c, None if deleted else self.name)
            original = [row for row in rows if row["source_ref"] != self.name]
            require(snapshot(original) == self.payload["baseline"]["snapshot"], "Original vector identity or content changed")
            return {"documents": len(rows), "originals_unchanged": True, "duplicates": 0,
                    "owned_fixture_deleted": deleted}
        # Two observations reduce false success from eventually arriving work.
        wait_until(check, 90)
        time.sleep(5)
        return wait_until(check, 30)

    def cleanup(self):
        from azure.core import MatchConditions

        blob = self.storage.get_blob_client(self.c["blob_container"], self.name)
        if blob.exists():
            properties = blob.get_blob_properties()
            require(properties.metadata.get("pr183_chaos_run") == self.run_id, "Fixture ownership lost")
            blob.delete_blob(etag=properties.etag, match_condition=MatchConditions.IfNotModified)
        return self.verify(deleted=True)

    def search(self):
        for query, expected in (
            ("globally distributed database", "azure-cosmos-db"),
            ("managed Kubernetes microservices", "azure-kubernetes-service"),
            ("unstructured object storage documents", "azure-blob-storage"),
        ):
            result = self.request("/api/playground/search", {
                "query": query, "destination_ids": [self.c["destination_id"]], "top_k": 3,
            })["results"]
            require(bool(result), "Search returned no results")
            identity = json.dumps({key: result[0].get(key) for key in ("id", "source_ref", "metadata")}).lower()
            require(expected in identity, "Search top result changed")
        return {"search_cases": 3, "result": "passed"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["baseline", "registry", "upload", "queued", "verify", "cleanup", "search"])
    parser.add_argument("payload")
    args = parser.parse_args()
    try:
        probe = Probe(json.loads(base64.b64decode(args.payload)))
        result = getattr(probe, args.action)()
        print("CHAOS_PROBE=" + json.dumps(result), flush=True)
    except Exception as error:
        # Azure/HTTP exceptions can contain credentials or document content.
        print("CHAOS_ERROR=" + type(error).__name__, flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
