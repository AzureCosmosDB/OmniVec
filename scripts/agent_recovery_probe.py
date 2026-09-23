"""Pod-local, opt-in real-agent recovery of one NEW synthetic Cosmos pipeline."""

import argparse
from datetime import datetime, timezone
import json
import math
import os
import re
import signal
import time


ENDPOINT = "https://omnivec-test-v3fapuy5j6s7c.documents.azure.com:443/"
DATABASE = "testdb"
EMBEDDING_MODEL = "mdl-ext-52423101"
CHAT_MODEL = "mdl-ext-075dd3b9"
CONFIRMATION = "ISOLATED-PR183-AGENT-RESUME-AUTHORIZED"
CHUNK = {"chunk_size": 600, "chunk_overlap": 80, "chunk_unit": "chars",
         "store_text": True, "text_field": "text", "doc_id_pattern": "{source}-chunk-{chunk}"}


class Blocked(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise Blocked(code)


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", value),
            "invalid_identifier")
    return value


def prefix_valid(prefix):
    require(bool(re.fullmatch(r"pr183-agent-recovery-[0-9a-f]{32}", prefix)), "invalid_owned_prefix")


def parse_sse(lines, *, clock=time.monotonic, deadline=None):
    """Consume the actual proxy's data: JSON SSE contract, with hard input caps."""
    events, data, size = [], [], 0
    for line in lines:
        require(deadline is None or clock() < deadline, "sse_deadline")
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        size += len(line)
        require(size <= 2_000_000, "sse_size_limit")
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif not line and data:
            event = json.loads("\n".join(data))
            require(isinstance(event, dict) and isinstance(event.get("type"), str), "invalid_sse_event")
            events.append(event)
            data = []
            require(len(events) <= 512, "sse_event_limit")
            require(event["type"] != "error", "agent_or_proxy_error")
            if event["type"] == "done":
                return events
    raise Blocked("sse_missing_done")


def select_proposal(events, pipeline_id):
    sessions = [e for e in events if e["type"] == "session"]
    proposals = [e for e in events if e["type"] == "approval_required"]
    require(len(sessions) == 1 and len(proposals) == 1, "one_session_and_proposal_required")
    session_id = identifier(sessions[0].get("session_id"))
    proposal = proposals[0]
    require(proposal.get("tool") == "resume_pipeline", "non_allowlisted_action")
    require(proposal.get("args") == {"pipeline_id": pipeline_id}, "non_allowlisted_action_scope")
    call_id = identifier(proposal.get("call_id"))
    # Prose is not evidence: require a scoped successful diagnostic tool result.
    proposal_index = events.index(proposal)
    calls = {e.get("id"): e for e in events[:proposal_index]
             if e["type"] == "tool_call" and e.get("name") == "diagnose_pipeline"
             and e.get("args") == {"pipeline_id": pipeline_id}}
    diagnoses = [e.get("result") for e in events[:proposal_index]
                 if e["type"] == "tool_result" and e.get("name") == "diagnose_pipeline"
                 and e.get("id") in calls]
    require(bool(diagnoses), "scoped_diagnosis_missing")
    diagnosis = diagnoses[-1]
    require(isinstance(diagnosis, dict) and not diagnosis.get("error"), "diagnosis_failed")
    require(diagnosis.get("status") == "BLOCKED" and not diagnosis.get("unknown"),
            "diagnosis_unknown_or_unready_dependencies")
    findings = diagnosis.get("findings", [])
    require(bool(findings) and all(f.get("code") == "paused" for f in findings),
            "paused_must_be_only_diagnosed_fault")
    entries = diagnosis.get("pipelines", [])
    require(len(entries) == 1 and entries[0].get("pipeline_id") == pipeline_id,
            "diagnosis_pipeline_mismatch")
    require(not entries[0].get("unknown"), "pipeline_diagnosis_unknown")
    return session_id, call_id


def validate_pending(pending, session_id, call_id, pipeline_id):
    require(isinstance(pending, list) and len(pending) == 1, "one_server_pending_approval_required")
    item = pending[0]
    require(item.get("session_id") == session_id and item.get("call_id") == call_id,
            "pending_identity_mismatch")
    require(item.get("tool_name") == "resume_pipeline"
            and item.get("args") == {"pipeline_id": pipeline_id}, "pending_action_mismatch")


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def arm_deadline(seconds):
    if hasattr(signal, "SIGALRM"):
        def expired(_signum, _frame):
            raise TimeoutError("bounded_scenario_deadline")
        signal.signal(signal.SIGALRM, expired)
        signal.alarm(seconds)


def recovery_proof(events, call_id, pipeline_id):
    """Accept runtime action evidence, never free-text chat claims or idle snapshots."""
    decisions = [e for e in events if e["type"] == "approval_decision"]
    require(len(decisions) == 1 and decisions[0].get("call_id") == call_id
            and decisions[0].get("decision") == "approve"
            and decisions[0].get("tool") == "resume_pipeline", "approval_execution_not_acknowledged")
    require(not any(e["type"] == "approval_required" for e in events), "additional_action_proposed_not_approved")
    actions = [e for e in events if e["type"] == "tool_result"
               and e.get("id") == call_id and e.get("name") == "resume_pipeline"]
    require(len(actions) == 1, "approved_action_result_missing")
    result = actions[0].get("result", {})
    require(not result.get("error") and not result.get("denied") and result.get("success") is True,
            "approved_action_failed")
    record = result.get("recovery", {})
    require(record.get("tool") == "resume_pipeline" and record.get("scope") == pipeline_id
            and type(record.get("attempt")) is int and record["attempt"] == 1, "runtime_action_scope_mismatch")
    require(record.get("outcome") == "VERIFIED_PROCESSING", "runtime_processing_not_verified")
    verification = record.get("verification", {})
    require(verification.get("status") == "HEALTHY" and verification.get("processing_verified") is True
            and not verification.get("unknown"), "runtime_verification_not_healthy")
    before_at, after_at = verification.get("baseline_at"), verification.get("observed_at")
    require(isinstance(before_at, str) and isinstance(after_at, str), "two_observation_timestamps_missing")
    before = datetime.fromisoformat(before_at.replace("Z", "+00:00"))
    after = datetime.fromisoformat(after_at.replace("Z", "+00:00"))
    require(before.tzinfo is not None and after.tzinfo is not None and before < after,
            "invalid_observation_order")
    require(0 <= (datetime.now(timezone.utc) - after).total_seconds() <= 600, "stale_runtime_verification")
    rows = verification.get("pipelines", [])
    require(len(rows) == 1 and rows[0].get("pipeline_id") == pipeline_id
            and rows[0].get("status") == "HEALTHY" and not rows[0].get("unknown")
            and rows[0].get("processing_verified") is True, "runtime_pipeline_progress_missing")
    progress = rows[0].get("progress", {})
    old, new = progress.get("embedded_before"), progress.get("embedded_after")
    require(numeric(old) and numeric(new) and old == 0 and new > old, "runtime_progress_did_not_increase")
    finals = [e for e in events if e["type"] == "final"]
    require(len(finals) == 1 and finals[0].get("recovery") == record, "authoritative_final_record_missing")
    require(any(e["type"] == "verification" and e.get("recovery") == record for e in events),
            "runtime_verification_event_missing")
    return {"outcome": "VERIFIED_PROCESSING", "pipeline_id": pipeline_id,
            "tool": "resume_pipeline", "attempt": 1, "embedded_before": old, "embedded_after": new,
            "baseline_at": before_at, "observed_at": after_at}


def validate_chunks(rows, pipeline_id, source_id, expected):
    require(len(rows) == len(expected) and len(rows) > 1, "actual_chunk_count_mismatch")
    require(len({r.get("id") for r in rows}) == len(rows), "duplicate_chunk_ids")
    require(all(r.get("pipeline_id") == pipeline_id and r.get("source_id") == source_id
                and r.get("source_ref") == "owned-pending-document" for r in rows), "foreign_chunk_identity")
    ordered = sorted(rows, key=lambda r: r.get("chunk_index", -1))
    require([r.get("chunk_index") for r in ordered] == list(range(len(rows)))
            and all(r.get("chunk_count") == len(rows) for r in rows), "invalid_chunk_indices")
    require([r.get("text") for r in ordered] == expected, "chunk_content_mismatch")
    for row in rows:
        vector = row.get("embedding")
        require(isinstance(vector, list) and len(vector) == 1536
                and all(numeric(v) for v in vector) and any(vector), "invalid_actual_embedding")
    require(len({tuple(r["embedding"]) for r in rows}) == len(rows), "identical_chunk_embeddings")
    return {"chunks": len(rows), "ids": sorted(r["id"] for r in rows), "dimensions": 1536,
            "duplicates": 0, "content_matches": True}


class Scenario:
    def __init__(self, prefix):
        prefix_valid(prefix)
        import requests
        from azure.cosmos import CosmosClient
        from azure.identity import DefaultAzureCredential

        self.prefix = prefix
        self.source_id = self.destination_id = self.pipeline_id = None
        self.approval_at = None
        self.action_observed = False
        self.deadline = time.monotonic() + 1200
        self.http = requests.Session()
        self.http.headers.update({
            "Authorization": "Bearer " + os.environ["OMNIVEC_ADMIN_TOKEN"],
            "Content-Type": "application/json",
        })
        self.credential = DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID"))
        self.cosmos = CosmosClient(ENDPOINT, credential=self.credential,
                                  connection_timeout=10, read_timeout=20, retry_total=0)
        self.db = self.cosmos.get_database_client(DATABASE)
        self.report = {
            "result": "blocked", "prefix": prefix, "fault_injected": [],
            "observed_failure": "not_checked", "repair_actor": "none",
            "agent_repair": "not_verified", "processing_recovery": "not_checked",
            "cleanup": "not_needed", "approval_attempts": 0, "fixture_creation_attempted": False,
        }

    def emit(self):
        # Never print free-form model text, raw events, HTTP bodies or exceptions.
        print("AGENT_RECOVERY_REPORT=" + json.dumps(self.report), flush=True)

    def remaining(self):
        require(time.monotonic() < self.deadline, "scenario_deadline")
        return self.deadline - time.monotonic()

    def call(self, method, path, body=None):
        self.report["last_operation"] = method + " " + path
        allowed_posts = {"sources", "destinations", "pipelines"}
        if self.pipeline_id:
            allowed_posts.add("pipelines/" + self.pipeline_id + "/pause")
        require(method == "GET" or method == "POST" and path in allowed_posts, "manual_action_not_allowlisted")
        response = self.http.request(
            method, "http://127.0.0.1:8080/api/" + path, json=body, timeout=(10, min(45, self.remaining())),
        )
        require(response.status_code == 200, "api_http_" + str(response.status_code))
        return response.json()

    def stream(self, path, body):
        require(path in ("agent/chat", "agent/chat/approve"), "invalid_agent_proxy_path")
        self.report["last_operation"] = "POST " + path
        deadline = min(self.deadline, time.monotonic() + 240)
        with self.http.post(
            "http://127.0.0.1:8080/api/" + path, json=body, stream=True,
            timeout=(10, min(180, self.remaining())),
        ) as response:
            require(response.status_code == 200, "agent_proxy_http_" + str(response.status_code))
            require("text/event-stream" in response.headers.get("Content-Type", ""), "agent_proxy_not_sse")
            return parse_sse(response.iter_lines(), deadline=deadline)

    def rows(self):
        self.remaining()
        return list(self.vectors.query_items("SELECT * FROM c", enable_cross_partition_query=True))

    def wait(self, check, seconds, code):
        end = min(self.deadline, time.monotonic() + seconds)
        while time.monotonic() < end:
            value = check()
            if value:
                return value
            time.sleep(5)
        raise Blocked(code)

    def owned_pipeline(self):
        pipe = self.call("GET", "pipelines/" + self.pipeline_id)
        require(pipe.get("id") == self.pipeline_id and pipe.get("name") == self.prefix + "-pipeline",
                "pipeline_ownership_lost")
        require(pipe.get("destination_id") == self.destination_id
                and [s.get("source_id") for s in pipe.get("sources", [])] == [self.source_id]
                and pipe.get("docgrok_pipeline") == EMBEDDING_MODEL, "pipeline_binding_changed")
        return pipe

    def preflight(self):
        models = self.call("GET", "models")["models"]
        chat = [m for m in models if m.get("id") == CHAT_MODEL]
        require(len(chat) == 1 and chat[0].get("model_category") == "chat", "approved_chat_model_not_available")
        require(sum(m.get("id") == EMBEDDING_MODEL for m in models) == 1, "embedding_model_missing")
        catalog = self.call("GET", "agent/tools")
        require(catalog.get("role") == "admin", "legitimate_authenticated_admin_required")
        tools = {t["name"]: t for t in catalog.get("tools", [])}
        require(tools.get("resume_pipeline", {}).get("readonly") is False
                and tools.get("diagnose_pipeline", {}).get("readonly") is True, "agent_tool_contract_missing")
        # New-only ownership: never adopt existing containers or registrations.
        existing = {item["id"] for item in self.db.list_containers()}
        require(self.prefix + "-source" not in existing and self.prefix + "-vectors" not in existing,
                "synthetic_containers_already_exist")
        for kind in ("sources", "destinations", "pipelines"):
            entries = self.call("GET", kind)[kind]
            require(not any(e.get("name", "").startswith(self.prefix) for e in entries), "fixture_prefix_already_registered")

    def create_fixture(self):
        from azure.cosmos import PartitionKey
        from chunker import chunk_text

        self.report["fixture_creation_attempted"] = True
        self.source = self.db.create_container(id=self.prefix + "-source", partition_key=PartitionKey(path="/id"))
        self.vectors = self.db.create_container(
            id=self.prefix + "-vectors", partition_key=PartitionKey(path="/id"),
            vector_embedding_policy={"vectorEmbeddings": [
                {"path": "/embedding", "dataType": "float32", "dimensions": 1536, "distanceFunction": "cosine"}]},
            indexing_policy={"indexingMode": "consistent", "automatic": True,
                             "includedPaths": [{"path": "/*"}], "excludedPaths": [{"path": '/"embedding"/*'}],
                             "vectorIndexes": [{"path": "/embedding", "type": "quantizedFlat"}]},
        )
        connection = {"endpoint": ENDPOINT, "database": DATABASE, "auth_type": "managed-identity",
                      "client_id": os.environ.get("AZURE_CLIENT_ID", "")}
        source = self.call("POST", "sources", {
            "name": self.prefix + "-source", "type": "cosmosdb",
            "config": {**connection, "container": self.prefix + "-source"},
        })["source"]
        self.source_id = identifier(source["id"])
        dest = self.call("POST", "destinations", {
            "name": self.prefix + "-vectors", "type": "cosmosdb-vector",
            "config": {**connection, "container": self.prefix + "-vectors", "vector_dimensions": 1536},
        })["destination"]
        self.destination_id = identifier(dest["id"])
        # Create against EMPTY source, pause, then introduce the pending document.
        pipeline = self.call("POST", "pipelines", {
            "name": self.prefix + "-pipeline",
            "sources": [{"source_id": self.source_id, "content_fields": ["content"], "content_mode": "field"}],
            "destination_id": self.destination_id, "docgrok_pipeline": EMBEDDING_MODEL,
            "vector_index_path": "embedding", "processing_mode": "queue", "process_existing": True,
            "content_strategy": "chunk", "chunk_config": CHUNK, "metadata_fields": [],
        })["pipeline"]
        self.pipeline_id = identifier(pipeline["id"])
        self.report["fixture"] = {"source_id": self.source_id, "destination_id": self.destination_id,
                                  "pipeline_id": self.pipeline_id, "database": DATABASE,
                                  "source_container": self.prefix + "-source",
                                  "vector_container": self.prefix + "-vectors"}
        self.emit()
        self.owned_pipeline()
        self.call("POST", "pipelines/" + self.pipeline_id + "/pause")
        require(self.owned_pipeline().get("status") == "paused", "fixture_pause_failed")
        self.report["fault_injected"] = ["paused_new_synthetic_pipeline_with_pending_document"]
        time.sleep(45)
        text = "\n\n".join(
            f"Section {i}. The cobalt otter observatory uses a violet spectrometer to measure polar aurora. "
            f"This is isolated agent recovery evidence {self.prefix}. "
            "The agent must resume only this pipeline; actual distinct persisted chunks demonstrate processing."
            for i in range(10)
        )
        self.document = {"id": "owned-pending-document", "content": text, "owner": self.prefix}
        self.expected = [part for part, _ in chunk_text(text, 600, 80, "chars")]
        require(1 < len(self.expected) <= 20, "fixture_chunk_bound")
        self.source.create_item(self.document)
        time.sleep(15)
        require(self.owned_pipeline().get("status") == "paused" and not self.rows(), "pending_fault_not_observed")
        self.report["observed_failure"] = {"pipeline": "paused", "source_documents": 1, "destination_chunks": 0}

    def wait_for_cached_health(self):
        def check():
            health = self.call("GET", "health/checks")
            for kind, item_id in (("sources", self.source_id), ("destinations", self.destination_id),
                                  ("models", EMBEDDING_MODEL)):
                row = next((r for r in health.get(kind, []) if r.get("id") == item_id), {})
                if row.get("status") != "healthy":
                    return False
                stamp = row.get("checked_at") or health.get("checked_at")
                if not stamp:
                    return False
                observed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if observed.tzinfo is None:
                    observed = observed.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - observed).total_seconds()
                if not 0 <= age < 600:
                    return False
            return True
        # Never trigger a broad health-check run across originals.
        self.wait(check, 360, "fresh_fixture_dependency_health_unavailable")

    def request_and_approve(self):
        prompt = (
            f"Diagnose ONLY synthetic pipeline {self.pipeline_id} using diagnose_pipeline. "
            "This is an authorized isolated recovery test: its intentional pause represents the injected fault "
            "and a new source document awaits processing. If pause is the only fault and dependencies are known "
            "healthy, propose exactly one resume_pipeline action with only "
            f'{{"pipeline_id":"{self.pipeline_id}"}}. Do not act on any other pipeline/resource. '
            "No other mutation is authorized. Do not propose retries, scaling, restarts, offset changes, "
            "queue operations or model changes. Wait for the existing approval flow. "
            "After approval use runtime recovery evidence; idle, unknown or unchanged counts are not repair."
        )
        proposed = self.stream("agent/chat", {"model_id": CHAT_MODEL,
                                            "messages": [{"role": "user", "content": prompt}]})
        session_id, call_id = select_proposal(proposed, self.pipeline_id)
        pending = self.call("GET", "agent/sessions/" + session_id + "/approvals")
        validate_pending(pending, session_id, call_id, self.pipeline_id)
        require(self.owned_pipeline().get("status") == "paused" and not self.rows(), "fault_changed_before_approval")
        self.report["approval"] = {"session_id": session_id, "call_id": call_id,
                                   "tool": "resume_pipeline", "args": {"pipeline_id": self.pipeline_id}}
        self.report["approval_attempts"] = 1
        self.approval_at = time.monotonic()
        self.emit()
        # Exactly one legitimate authenticated approval. Never retry an ambiguous POST.
        approved = self.stream("agent/chat/approve", {
            "session_id": session_id, "call_id": call_id, "decision": "approve",
            "comment": "Parent-authorized isolated test: resume only the exact new synthetic pipeline.",
        })
        self.action_observed = any(e["type"] == "tool_result" and e.get("id") == call_id
                                   and e.get("name") == "resume_pipeline" for e in approved)
        self.report["approved_action_result_observed"] = self.action_observed
        outcomes = [e.get("result", {}).get("recovery", {}).get("outcome") for e in approved
                    if e["type"] == "tool_result" and e.get("id") == call_id]
        allowed_outcomes = {"VERIFIED_PROCESSING", "READY_IDLE", "NOT_VERIFIED", "ACTION_FAILED", "ACTION_NOT_EXECUTED"}
        self.report["runtime_outcome"] = outcomes[0] if outcomes and outcomes[0] in allowed_outcomes else "UNKNOWN"
        self.report["runtime_proof"] = recovery_proof(approved, call_id, self.pipeline_id)

    def verify_data(self):
        def check():
            rows = self.rows()
            if len(rows) < len(self.expected):
                return None
            return validate_chunks(rows, self.pipeline_id, self.source_id, self.expected)
        self.wait(check, 180, "actual_processing_deadline")
        time.sleep(5)
        chunks = check()
        require(chunks is not None, "actual_processing_regressed")
        stored = self.source.read_item("owned-pending-document", partition_key="owned-pending-document")
        require(all(stored.get(k) == v for k, v in self.document.items()), "source_document_changed")
        pipe = self.owned_pipeline()
        require(pipe.get("status") == "active" and pipe.get("stats", {}).get("embedded_count", 0) > 0,
                "actual_pipeline_progress_missing")
        self.report["processing_recovery"] = chunks
        self.report["repair_actor"] = "approved_real_agent_resume_pipeline"
        self.report["agent_repair"] = "VERIFIED_PROCESSING_AND_ACTUAL_CHUNKS"

    def cleanup(self):
        self.deadline = time.monotonic() + 180
        if not self.pipeline_id and self.source_id and self.destination_id:
            # Recover a lost create response by exact unique name/binding, never adopt unrelated fixtures.
            candidates = [p for p in self.call("GET", "pipelines")["pipelines"]
                          if p.get("name") == self.prefix + "-pipeline"]
            require(len(candidates) <= 1, "ambiguous_pipeline_create")
            if candidates:
                self.pipeline_id = identifier(candidates[0]["id"])
        if not self.pipeline_id:
            return
        if self.approval_at is not None and not self.action_observed:
            # Runtime has a 35s pre-action observation + 30s HTTP action budget.
            # Leave margin before final pause if the approval response was lost.
            time.sleep(max(0, self.approval_at + 90 - time.monotonic()))
            self.report["approval_transport"] = "ambiguous_no_retry_operator_must_reconcile"
        self.owned_pipeline()
        self.call("POST", "pipelines/" + self.pipeline_id + "/pause")
        require(self.owned_pipeline().get("status") == "paused", "final_owned_pause_failed")
        time.sleep(5)
        require(self.owned_pipeline().get("status") == "paused", "final_owned_pause_regressed")
        self.report["cleanup"] = {"pipeline_id": self.pipeline_id, "status": "paused",
                                  "resources_retained": True, "manual_resume_executed": False}

    def run(self):
        arm_deadline(1200)
        try:
            self.preflight()
            self.create_fixture()
            self.wait_for_cached_health()
            self.request_and_approve()
            self.verify_data()
            self.report["result"] = "passed"
        except Exception as error:
            self.report["result"] = "failed" if self.approval_at is not None else "blocked"
            self.report["error_code"] = str(error) if isinstance(error, Blocked) else type(error).__name__
        finally:
            arm_deadline(180)
            try:
                self.cleanup()
            except Exception as error:
                self.report["result"] = "failed"
                self.report["cleanup"] = "unverified_owned_fixture_retained"
                self.report["cleanup_error"] = str(error) if isinstance(error, Blocked) else type(error).__name__
            self.emit()
            arm_deadline(0)
        return self.report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    prefix_valid(args.prefix)
    require(args.confirm == CONFIRMATION, "explicit_authorization_required")
    scenario = Scenario(args.prefix)
    report = scenario.run()
    return 0 if report["result"] == "passed" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print("AGENT_RECOVERY_REPORT=" + json.dumps({
            "result": "blocked", "error_code": type(error).__name__, "cleanup": "inspect_owned_prefix",
        }), flush=True)
        raise SystemExit(1)
