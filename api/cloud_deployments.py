"""Approval-gated, durable MCP and Foundry deployment jobs.

Only explicit admin requests queue work. A lost worker is never silently replayed:
its lease expires into interrupted state and an administrator must retry.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Literal
from uuid import UUID, uuid4

from azure.cosmos.exceptions import CosmosAccessConditionFailedError
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from permission_guidance import account_from_config, guid, render_command, validate_resource_id

logger = logging.getLogger(__name__)
DOC_TYPE = "cloud_deployment"
LEASE_SECONDS = 1800
MAX_ATTEMPTS = 3
SEGMENT = r"[A-Za-z0-9_-]+"
RG_PATTERN = r"/subscriptions/([0-9a-fA-F-]{36})/resourceGroups/([A-Za-z0-9_.()-]+)"
ACCOUNT_PATTERN = RG_PATTERN + r"/providers/Microsoft.CognitiveServices/accounts/(" + SEGMENT + ")"


class DeploymentPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["mcp", "foundry", "cosmos_container", "verification"]
    resource_group_id: str
    location: str = Field(pattern=r"^[a-z0-9]{2,40}$")
    destination_id: str = ""
    embedding_model_id: str = ""
    cosmos_account_id: str = ""
    embedding_account_id: str = ""
    vector_field: str = Field(default="embedding", pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
    fields: str = "id,title,text,source_ref"
    mcp_deployment_id: str = ""
    project_resource_id: str = ""
    chat_deployment: str = Field(default="", pattern=r"^[A-Za-z0-9_.-]*$", max_length=128)
    database: str = Field(default="", pattern=r"^[A-Za-z0-9_-]*$", max_length=128)
    container: str = Field(default="", pattern=r"^[A-Za-z0-9_-]*$", max_length=128)
    foundry_deployment_id: str = ""
    question: str = Field(default="", max_length=4000)


class DeploymentApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_hash: str
    approve_cost_and_permissions: Literal[True]


def capabilities():
    scopes = [s.strip().rstrip("/") for s in os.getenv("OMNIVEC_DEPLOYMENT_SCOPES", "").split(",") if s.strip()]
    valid = bool(scopes) and all(
        re.fullmatch(RG_PATTERN + r"(?:/providers/[A-Za-z0-9_.()-]+(?:/[A-Za-z0-9_.()-]+)+)?", scope, re.IGNORECASE)
        and not any(part in (".", "..") for part in scope.split("/"))
        and guid(scope.split("/")[2])
        for scope in scopes
    )
    return {
        "enabled": os.getenv("OMNIVEC_CLOUD_DEPLOYMENTS_ENABLED", "").lower() == "true" and valid,
        "allowed_scopes": scopes,
        "principal_id": os.getenv("AZURE_PRINCIPAL_ID", ""),
        "message": "An administrator must enable deployment and allow every target resource scope. Preview does not provision resources.",
    }


def in_scope(resource_id):
    value = resource_id.lower()
    return any(value == s.lower() or value.startswith(s.lower() + "/") for s in capabilities()["allowed_scopes"])


def require_scopes(plan):
    if not capabilities()["enabled"]:
        raise HTTPException(409, "Cloud deployment is disabled. Enable it and configure allowed resource scopes first.")
    if any(not in_scope(scope) for scope in plan["authorization_scopes"]):
        raise HTTPException(403, "A target resource is outside OMNIVEC_DEPLOYMENT_SCOPES. Ask the deployment administrator to review the plan.")


def public_job(doc):
    return {k: v for k, v in doc.items() if not k.startswith("_") and k != "doc_type"}


def _match(pattern, value, description):
    match = re.fullmatch(pattern, value, re.IGNORECASE)
    if not match:
        raise ValueError(f"Enter the full Azure resource ID for {description}.")
    UUID(match[1])
    return match


def _record(store, record_id, kind):
    doc = store.get(record_id, kind)
    if not doc:
        raise ValueError(f"The selected {kind} no longer exists.")
    return doc


def build_plan(body, store):
    _match(RG_PATTERN, body.resource_group_id, "the existing target resource group")
    job_id = "deploy-" + uuid4().hex
    suffix = job_id[-16:]
    plan = {
        "kind": body.kind, "resource_group_id": body.resource_group_id,
        "location": body.location, "authorization_scopes": [body.resource_group_id],
        "resources": [], "permissions": [], "references": [],
        "deployment_principal_id": capabilities()["principal_id"],
    }
    if body.kind == "cosmos_container":
        match = _match(RG_PATTERN + r"/providers/Microsoft.DocumentDB/databaseAccounts/(" + SEGMENT + ")", body.cosmos_account_id, "the existing Cosmos account")
        if not body.database or not body.container:
            raise ValueError("Enter the existing database and a new, isolated container name.")
        model = _record(store, body.embedding_model_id, "docgrok_model")
        dimensions = int(model.get("embedding_dim", 0))
        if model.get("model_category", "embedding") != "embedding" or model.get("enabled") is False or not 1 <= dimensions <= 4096:
            raise ValueError("Select an enabled embedding model with 1-4096 dimensions.")
        if body.resource_group_id.lower() != body.cosmos_account_id.split("/providers/")[0].lower():
            raise ValueError("The selected resource group must contain the Cosmos account.")
        rid = body.cosmos_account_id + f"/sqlDatabases/{body.database}/containers/{body.container}"
        plan.update(
            location="inherited from existing Cosmos account",
            cosmos_account_id=body.cosmos_account_id, database=body.database, container=body.container,
            container_id=rid, cosmos_endpoint=f"https://{match[3]}.documents.azure.com",
            embedding_dimensions=dimensions, vector_field=body.vector_field,
            resources=[rid], authorization_scopes=[body.cosmos_account_id],
            references=[{"id": model["id"], "type": "docgrok_model", "etag": model.get("_etag")}],
            notice="Creates one NEW vector container in an existing Cosmos database, partitioned by /id, with a cosine DiskANN index. Existing containers will not be adopted or changed. Uses the account/database throughput arrangement; normal Cosmos storage, RU and indexing charges apply. Register this container as a destination after provisioning succeeds.",
        )
    elif body.kind == "verification":
        parent = _record(store, body.foundry_deployment_id, DOC_TYPE)
        if parent["kind"] != "foundry" or parent["status"] != "succeeded":
            raise ValueError("Select a successfully created Foundry agent.")
        if not body.question.strip():
            raise ValueError("Enter a question for the agent.")
        previous = parent["plan"]
        plan.update(
            resource_group_id=previous["resource_group_id"], location=previous["location"],
            foundry_deployment_id=parent["id"], project_resource_id=previous["project_resource_id"],
            agent_name=parent["result"]["agent_name"], agent_version=parent["result"]["agent_version"],
            question=body.question.strip(), resources=[previous["project_resource_id"]],
            authorization_scopes=[previous["project_resource_id"]],
            notice="Runs ONE billable Foundry response against the saved agent version, including MCP retrieval and embedding calls. The question, answer and returned source references are saved in administrator-only job metadata. No SharePoint files are modified. A timed-out inference is not replayed automatically.",
        )
    elif body.kind == "mcp":
        destination = _record(store, body.destination_id, "destination")
        model = _record(store, body.embedding_model_id, "docgrok_model")
        if destination.get("type") != "cosmosdb-vector" or destination.get("enabled") is False:
            raise ValueError("Select an enabled Cosmos DB vector destination.")
        if (model.get("type") != "azure-openai" or model.get("model_category", "embedding") != "embedding"
                or model.get("enabled") is False):
            raise ValueError("Select an enabled Azure OpenAI embedding model.")
        config = destination["config"]
        account = account_from_config("cosmosdb-vector", config)
        validate_resource_id(body.cosmos_account_id, "cosmosdb-vector", account)
        embedding_account = _match(ACCOUNT_PATTERN, body.embedding_account_id, "the embedding account")[3]
        endpoint = f"https://{embedding_account.lower()}.openai.azure.com"
        if str(model.get("endpoint", "")).rstrip("/").lower() != endpoint:
            raise ValueError("Embedding account ID must match the registered Azure OpenAI endpoint.")
        for field in ("database", "container"):
            if not re.fullmatch(SEGMENT, str(config.get(field, ""))):
                raise ValueError("This deployment supports database/container names containing letters, digits, underscores and hyphens.")
        fields = [f.strip() for f in body.fields.split(",")]
        if not 1 <= len(fields) <= 20 or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", f) for f in fields):
            raise ValueError("Select 1-20 comma-separated top-level document fields.")
        deployment = model.get("deployment", "")
        if not re.fullmatch(SEGMENT, deployment):
            raise ValueError("The embedding model must have a valid Azure deployment name.")
        dimensions = int(model.get("embedding_dim", 0))
        if dimensions <= 0:
            raise ValueError("The registered embedding model must specify its dimensions.")
        app_name = "ovmcp-" + suffix
        storage_name = "ovmcp" + suffix
        root = body.resource_group_id + "/providers/"
        plan.update({
            "destination_id": body.destination_id, "embedding_model_id": body.embedding_model_id,
            "cosmos_account_id": body.cosmos_account_id, "embedding_account_id": body.embedding_account_id,
            "database": config["database"], "container": config["container"],
            "cosmos_endpoint": config["endpoint"].rstrip("/"), "embedding_endpoint": endpoint,
            "embedding_deployment": deployment, "embedding_dimensions": dimensions,
            "embedding_api_version": model.get("api_version", "2024-06-01"),
            "vector_field": body.vector_field, "fields": ",".join(fields),
            "app_name": app_name, "storage_name": storage_name,
            "function_id": root + "Microsoft.Web/sites/" + app_name,
            "storage_id": root + "Microsoft.Storage/storageAccounts/" + storage_name,
            "plan_id": root + "Microsoft.Web/serverfarms/" + app_name,
            "references": [{"id": d["id"], "type": d["doc_type"], "etag": d.get("_etag")} for d in (destination, model)],
            "package_sha256": package_hash(),
        })
        plan["resources"] = [plan["function_id"], plan["storage_id"], plan["plan_id"]]
        plan["authorization_scopes"] += [body.cosmos_account_id, body.embedding_account_id]
        plan["permissions"] = [
            {"role": "Cosmos DB Built-in Data Reader", "scope": body.cosmos_account_id + f"/dbs/{config['database']}/colls/{config['container']}"},
            {"role": "Cognitive Services OpenAI User", "scope": body.embedding_account_id},
            {"role": "Storage Blob Data Owner", "scope": plan["storage_id"]},
        ]
        plan["notice"] = (
            "Creates a Python 3.11 Flex Consumption Function, storage and read-only data grants to its new identity. "
            "Public HTTPS is protected by a Function key. Hosting, storage and subsequent embedding calls cost money. "
            "Confirm this embedding deployment is the one that produced the selected vectors; equal dimensions alone do not establish compatibility. "
            "Existing private-network-only data services require separate network setup. No existing data is changed."
        )
    else:
        mcp = _record(store, body.mcp_deployment_id, DOC_TYPE)
        if mcp["kind"] != "mcp" or mcp["status"] != "succeeded":
            raise ValueError("Select a successfully deployed MCP server.")
        match = _match(ACCOUNT_PATTERN + r"/projects/(" + SEGMENT + ")", body.project_resource_id, "the existing Foundry project")
        if body.project_resource_id.split("/providers/")[0].lower() != body.resource_group_id.lower():
            raise ValueError("The selected resource group must contain the Foundry project.")
        if not body.chat_deployment:
            raise ValueError("Enter the actual chat model deployment name in the Foundry account.")
        connection_id = body.project_resource_id + "/connections/ovmcp-" + suffix
        plan.update({
            "mcp_deployment_id": mcp["id"], "function_id": mcp["plan"]["function_id"],
            "server_url": mcp["result"]["server_url"],
            "project_resource_id": body.project_resource_id,
            "foundry_account_id": body.project_resource_id.rsplit("/projects/", 1)[0],
            "project_name": match[4], "chat_deployment": body.chat_deployment,
            "connection_id": connection_id, "agent_name": "omnivec-" + suffix,
            "container": mcp["plan"]["container"], "fields": mcp["plan"]["fields"],
            "vector_field": mcp["plan"]["vector_field"],
        })
        plan["resources"] = [connection_id, "Foundry agent: " + plan["agent_name"]]
        plan["authorization_scopes"] += [body.project_resource_id, plan["function_id"]]
        plan["notice"] = (
            "Creates a secure CustomKeys project connection and one prompt agent in an EXISTING Foundry project. "
            "The Function key is transferred server-to-server, never returned to the browser or saved in OmniVec metadata. "
            "No project, chat deployment, conversation or inference request is created. Agent inference and retrieval are not verified by creation."
        )
    plan["deployer_permissions"] = deployer_permissions(plan)
    return {
        "id": job_id, "doc_type": DOC_TYPE, "kind": body.kind, "status": "awaiting_approval",
        "created_at": time.time(), "updated_at": time.time(), "attempts": 0, "stage": "Review plan",
        "plan": plan, "plan_hash": hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest(),
        "history": [], "result": {},
    }


def package_path():
    return Path(os.getenv("OMNIVEC_MCP_PACKAGE", "/app/mcp-cosmos.zip"))


def deployer_permissions(plan):
    """Commands are for the administrator to review, never executed by the worker."""
    if plan["kind"] == "verification":
        roles = [(plan["project_resource_id"], "Azure AI User")]
    elif plan["kind"] == "cosmos_container":
        roles = [(plan["cosmos_account_id"], "DocumentDB Account Contributor")]
    else:
        roles = [(plan["resource_group_id"], "Contributor")]
    if plan["kind"] == "mcp":
        roles += [
            (plan["resource_group_id"], "Role Based Access Control Administrator"),
            (plan["cosmos_account_id"], "DocumentDB Account Contributor"),
            (plan["embedding_account_id"], "Reader"),
            (plan["embedding_account_id"], "Role Based Access Control Administrator"),
        ]
    elif plan["kind"] == "foundry":
        roles += [
            (plan["project_resource_id"], "Azure AI User"),
            (plan["foundry_account_id"], "Reader"),
            (plan["function_id"], "Website Contributor"),
        ]
    principal = guid(os.getenv("AZURE_PRINCIPAL_ID", ""))
    result = []
    for scope, role in roles:
        item = {"scope": scope, "role": role, "principal_id": principal or "", "commands": {}}
        if principal:
            args = ["az", "role", "assignment", "create", "--subscription", scope.split("/")[2],
                    "--assignee-object-id", principal, "--assignee-principal-type", "ServicePrincipal",
                    "--role", role, "--scope", scope]
            item["commands"] = {shell: render_command(args, shell) for shell in ("powershell", "bash")}
        result.append(item)
    return result


def package_hash():
    path = package_path()
    if not path.is_file():
        raise ValueError("The API image does not contain the built MCP package. Build the updated API image before preparing an MCP deployment.")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_references(doc, store):
    if doc["plan"]["deployment_principal_id"] != capabilities()["principal_id"]:
        raise ValueError("The deployment workload identity changed. Prepare a new plan.")
    for ref in doc["plan"].get("references", []):
        current = store.get(ref["id"], ref["type"])
        if not current or current.get("_etag") != ref["etag"]:
            raise ValueError("The destination or embedding model changed. Prepare a new plan.")
    if doc["kind"] == "mcp" and package_hash() != doc["plan"]["package_sha256"]:
        raise ValueError("The packaged server changed. Prepare a new plan.")


def install_routes(app, get_store):
    def admin(request):
        auth = getattr(request.state, "auth", None)
        if not auth or auth.get("role") != "admin":
            raise HTTPException(403, "Cloud deployment requires an authenticated OmniVec administrator.")
        return auth.get("id") or auth.get("name", "admin")

    @app.get("/api/cloud-deployments")
    async def list_cloud_deployments(request: Request):
        admin(request)
        def read():
            store = get_store()
            docs = store.query("SELECT TOP 100 * FROM c WHERE c.doc_type = @type ORDER BY c.created_at DESC",
                               [{"name": "@type", "value": DOC_TYPE}], partition_key=DOC_TYPE)
            return {"capabilities": capabilities(), "jobs": [public_job(d) for d in docs]}
        return await asyncio.to_thread(read)

    @app.post("/api/cloud-deployments/plan")
    async def plan_cloud_deployment(body: DeploymentPlanRequest, request: Request):
        actor = admin(request)
        def prepare():
            store = get_store()
            try:
                doc = build_plan(body, store)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            doc["created_by"] = actor
            return public_job(store.create(doc))
        return await asyncio.to_thread(prepare)

    @app.post("/api/cloud-deployments/{job_id}/approve")
    async def approve_cloud_deployment(job_id: str, body: DeploymentApproval, request: Request):
        actor = admin(request)
        def approve():
            store = get_store()
            doc = store.get(job_id, DOC_TYPE)
            if not doc:
                raise HTTPException(404, "Deployment not found.")
            require_scopes(doc["plan"])
            if body.plan_hash != doc["plan_hash"]:
                raise HTTPException(409, "The plan changed. Reload and review it.")
            if doc["status"] in ("queued", "running", "succeeded"):
                return public_job(doc)
            if doc["status"] not in ("awaiting_approval", "failed", "interrupted") or doc["attempts"] >= MAX_ATTEMPTS:
                raise HTTPException(409, "Retry limit reached. Inspect Azure resources before preparing another plan.")
            if doc["kind"] == "verification" and doc.get("inference_started"):
                raise HTTPException(409, "This inference may already have been billed. Inspect its result and prepare a new test plan to explicitly authorize another request.")
            if doc["status"] == "awaiting_approval" and time.time() - doc["created_at"] > 1800:
                raise HTTPException(409, "Plan expired. Prepare a new plan.")
            try:
                check_references(doc, store)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            doc.update(status="queued", approved_by=actor, approved_at=time.time(), updated_at=time.time(), error=None,
                       approval_history=doc.get("approval_history", []) + [{"actor": actor, "at": time.time(), "plan_hash": body.plan_hash}])
            try:
                return public_job(store.replace_with_etag(doc, doc["_etag"]))
            except CosmosAccessConditionFailedError:
                raise HTTPException(409, "Deployment changed concurrently. Refresh its status.") from None
        return await asyncio.to_thread(approve)


def _update(store, doc, **changes):
    updated = {**doc, **changes, "updated_at": time.time()}
    saved = store.replace_with_etag(updated, doc["_etag"])
    doc.clear()
    doc.update(saved)


def execute_job(store, doc):
    from cloud_deployment_azure import AzureDeployment, DeploymentFailure

    def progress(stage, **changes):
        if time.time() >= doc["lease_until"] - 120:
            raise DeploymentFailure("deadline_exceeded", "Deployment exceeded its 28-minute budget. Inspect the listed Azure resources before retrying.")
        require_scopes(doc["plan"])
        history = (doc["history"] + [{"stage": stage, "at": time.time()}])[-50:]
        _update(store, doc, stage=stage, history=history, **changes)

    try:
        require_scopes(doc["plan"])
        check_references(doc, store)
        with AzureDeployment(doc, progress) as deployment:
            result = deployment.run()
        _update(store, doc, status="succeeded", stage="Completed", result=result)
    except CosmosAccessConditionFailedError:
        logger.error("Deployment ownership lost: %s", doc["id"])
    except Exception as exc:
        # Persist a safe failure even for a worker bug, never a success-shaped fallback.
        if isinstance(exc, DeploymentFailure):
            error = {"code": exc.code, "message": exc.safe_message, "resource_id": exc.resource_id}
        elif isinstance(exc, HTTPException):
            error = {"code": "deployment_disabled", "message": exc.detail}
        elif isinstance(exc, ValueError):
            error = {"code": "plan_changed", "message": "The plan is no longer valid. Prepare a new plan."}
        else:
            error = {"code": "worker_error", "message": "The deployment worker failed. Inspect Azure activity logs for the listed resources before retrying."}
        logger.error("Cloud deployment %s failed at %s (%s)", doc["id"], doc["stage"], type(exc).__name__)
        _update(store, doc, status="failed", error=error)


async def deployment_worker(get_store):
    while True:
        try:
            if not capabilities()["enabled"]:
                await asyncio.sleep(10)
                continue
            def take():
                store = get_store()
                docs = store.query(
                    "SELECT TOP 20 * FROM c WHERE c.doc_type = @type AND c.status IN ('queued', 'running')",
                    [{"name": "@type", "value": DOC_TYPE}], partition_key=DOC_TYPE,
                )
                for doc in docs:
                    try:
                        if doc["status"] == "running":
                            if doc["lease_until"] < time.time():
                                _update(store, doc, status="interrupted", error={
                                    "code": "worker_interrupted",
                                    "message": "The worker stopped reporting. Inspect Azure resources, then explicitly approve a retry. No resources were deleted.",
                                })
                            continue
                        if not capabilities()["enabled"]:
                            continue
                        _update(store, doc, status="running", stage="Preflight",
                                lease_until=time.time() + LEASE_SECONDS, attempts=doc["attempts"] + 1)
                        return store, doc
                    except CosmosAccessConditionFailedError:
                        continue
                return None
            claimed = await asyncio.to_thread(take)
            if claimed:
                await asyncio.to_thread(execute_job, *claimed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Cloud deployment queue unavailable (%s)", type(exc).__name__)
        await asyncio.sleep(10)
