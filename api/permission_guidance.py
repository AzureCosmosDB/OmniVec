"""Read-only access checks and deterministic, customer-executed permission plans."""
import asyncio
from datetime import datetime, timezone
import logging
import os
import re
import shlex
from urllib.parse import urlparse
from uuid import UUID, uuid5, NAMESPACE_URL


logger = logging.getLogger(__name__)
SUPPORTED = {("source", "azure-blob"), ("source", "cosmosdb"), ("destination", "cosmosdb-vector")}
RESOURCE_ID = re.compile(
    r"^/subscriptions/([0-9a-f-]{36})/resourceGroups/([A-Za-z0-9_.()-]+)/providers/"
    r"(Microsoft\.DocumentDB/databaseAccounts|Microsoft\.Storage/storageAccounts)/([a-z0-9-]+)$",
    re.IGNORECASE,
)


def guid(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        return None


def classify_error(error):
    """Never return raw exception text: SDK messages can contain credentials."""
    text = str(error).lower()
    status = getattr(error, "status_code", None)
    if "5300" in text and ("cannot be authorized" in text or "data plane" in text):
        return "provisioning_required", "This operation requires Azure resource provisioning, not another data-access role."
    if isinstance(error, (TimeoutError, ConnectionError)) or any(
        word in text for word in ("timed out", "timeout", "name resolution", "connection refused", "failed to resolve")
    ):
        return "network_unreachable", "The service could not be reached. Check DNS, private endpoints and outbound connectivity; do not grant more permissions."
    if status == 403 and any(word in text for word in ("firewall", "virtual network", "ip address", "public network")):
        return "network_restricted", "The service rejected the network path. Ask the network owner to review the private endpoint or allowed network; granting data roles will not fix this restriction."
    if any(word in text for word in ("aadsts", "credentialunavailable", "authenticationfailed")) or status == 401:
        return "authentication_failed", "The workload identity could not authenticate. Check its tenant and workload identity federation before granting data access."
    if status == 403 or any(word in text for word in ("forbidden", "authorizationpermissionmismatch", "accessdenied")):
        return "access_denied", "The service denied this operation. Review the identity and required access below; a denial can also be caused by service network restrictions."
    if status == 404:
        return "resource_missing", "The requested resource was not found. Check the account, database and container; granting a role does not create them."
    return "unknown", "The access check failed without enough evidence to identify a permission problem."


def account_from_config(connector, config):
    endpoint = config.get("account_url" if connector == "azure-blob" else "endpoint", "")
    if not isinstance(endpoint, str):
        raise ValueError("Enter an HTTPS account endpoint.")
    try:
        parsed = urlparse(endpoint)
        port = parsed.port
    except ValueError:
        raise ValueError("Enter a valid HTTPS account endpoint.") from None
    suffix = ".blob.core.windows.net" if connector == "azure-blob" else ".documents.azure.com"
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ("", "/") or port not in (None, 443)
            or not host.endswith(suffix)):
        raise ValueError("Use an HTTPS Azure public-cloud account endpoint without credentials, paths or query parameters.")
    account = host[:-len(suffix)]
    if not re.fullmatch(r"[a-z0-9-]+", account):
        raise ValueError("Invalid account name in endpoint.")
    return account


def validate_resource_id(resource_id, connector, account):
    match = RESOURCE_ID.fullmatch(resource_id)
    provider = "Microsoft.Storage/storageAccounts" if connector == "azure-blob" else "Microsoft.DocumentDB/databaseAccounts"
    if not match or not guid(match[1]) or match[3].lower() != provider.lower() or match[4].lower() != account:
        raise ValueError("Account resource ID must match the selected endpoint and include its actual subscription and resource group.")
    return match


async def resolve_resource_id(connector, account):
    """Discovery is advisory; lack of ARM read access never means missing data access."""
    import httpx
    from azure.core.exceptions import AzureError
    from azure.identity.aio import DefaultAzureCredential

    subscription = guid(os.getenv("AZURE_SUBSCRIPTION_ID"))
    if not subscription:
        return "", "Paste the full account resource ID from Azure Portal > resource > JSON View."
    provider = "Microsoft.Storage/storageAccounts" if connector == "azure-blob" else "Microsoft.DocumentDB/databaseAccounts"
    try:
        async with DefaultAzureCredential() as credential:
            token = await credential.get_token("https://management.azure.com/.default")
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(
                    f"https://management.azure.com/subscriptions/{subscription}/resources",
                    params={"api-version": "2021-04-01", "$filter": f"resourceType eq '{provider}' and name eq '{account}'"},
                    headers={"Authorization": "Bearer " + token.token},
                )
                response.raise_for_status()
                matches = response.json().get("value", [])
                if len(matches) == 1 and not response.json().get("nextLink"):
                    resource_id = matches[0]["id"]
                    validate_resource_id(resource_id, connector, account)
                    return resource_id, ""
    except (httpx.HTTPError, AzureError, ValueError, KeyError) as exc:
        logger.info("Permission resource discovery unavailable: %s", type(exc).__name__)
    return "", "Resource discovery is unavailable or outside the deployment subscription. Paste the full account resource ID; no extra discovery permissions are required."


def render_command(args, shell):
    if shell == "bash":
        return " ".join(shlex.quote(arg) for arg in args)
    return args[0] + " " + " ".join("'" + arg.replace("'", "''") + "'" for arg in args[1:])


def grant_commands(kind, connector, config, resource_id, principal_id):
    account = account_from_config(connector, config)
    match = validate_resource_id(resource_id, connector, account)
    principal = guid(principal_id)
    if not principal:
        raise ValueError("An actual workload identity principal/object ID is required, not its client/application ID.")
    container = config.get("container", "")
    database = config.get("database", "")
    for value in [container] + ([] if connector == "azure-blob" else [database]):
        if not isinstance(value, str) or not value or re.search(r"[/\\?#\x00-\x1f\x7f]", value):
            raise ValueError("Enter a valid database/container name without path separators or control characters.")
    if connector == "azure-blob":
        scope = resource_id + "/blobServices/default/containers/" + container
        role = "Storage Blob Data Reader"
        role_id = "2a2b9908-6ea1-4ae2-8e65-a410df84e7d1"
        assignment = str(uuid5(NAMESPACE_URL, (scope + principal + role_id).lower()))
        args = ["az", "role", "assignment", "create", "--subscription", match[1], "--name", assignment,
                "--assignee-object-id", principal, "--assignee-principal-type", "ServicePrincipal",
                "--role", role_id, "--scope", scope]
        administrator = "An Azure administrator allowed to create role assignments at this container scope."
    else:
        scope = f"/dbs/{database}/colls/{container}"
        role = "Cosmos DB Built-in Data " + ("Reader" if kind == "source" else "Contributor")
        role_id = "00000000-0000-0000-0000-00000000000" + ("1" if kind == "source" else "2")
        assignment = str(uuid5(NAMESPACE_URL, (resource_id + scope + principal + role_id).lower()))
        args = ["az", "cosmosdb", "sql", "role", "assignment", "create", "--subscription", match[1],
                "--resource-group", match[2], "--account-name", account, "--role-assignment-id", assignment,
                "--role-definition-id", resource_id + "/sqlRoleDefinitions/" + role_id,
                "--principal-id", principal, "--scope", scope]
        administrator = "An Azure administrator allowed to write Cosmos SQL role assignments on this account."
    return {
        "role": role, "scope": scope, "resource_id": resource_id, "principal_id": principal,
        "administrator": administrator,
        "reason": "Read source data without modifying it." if kind == "source" else
                  "Read, create, update and delete vector documents as required for synchronization.",
        "commands": {shell: render_command(args, shell) for shell in ("powershell", "bash")},
    }


def probe(connector, config):
    from azure.identity import DefaultAzureCredential

    with DefaultAzureCredential(managed_identity_client_id=os.getenv("AZURE_CLIENT_ID"),
                                connection_timeout=3, read_timeout=5, retry_total=0) as credential:
        if connector == "azure-blob":
            from azure.storage.blob import BlobServiceClient
            with BlobServiceClient(config["account_url"], credential=credential, connection_timeout=3,
                                   read_timeout=5, retry_total=0) as client:
                next(iter(client.get_container_client(config["container"]).list_blobs(results_per_page=1)), None)
        else:
            from azure.cosmos import CosmosClient
            with CosmosClient(config["endpoint"], credential=credential, connection_timeout=3,
                              timeout=5, retry_total=0) as client:
                container = client.get_database_client(config["database"]).get_container_client(config["container"])
                container.read()
                if connector == "cosmosdb":
                    next(iter(container.query_items("SELECT TOP 1 c.id FROM c", enable_cross_partition_query=True)), None)


async def check_access(kind, connector, config, resource_id="", principal_id=""):
    from azure.core.exceptions import AzureError

    result = {
        "status": "unknown", "summary": "", "checked_at": datetime.now(timezone.utc).isoformat(),
        "checks": [], "missing": [], "commands": {}, "resource_id": resource_id,
        "verification": "Run Check again after the administrator executes the command. Role propagation can take several minutes. No permission changes are performed by OmniVec.",
        "limitations": ["Checked from the API workload. Worker-specific network policies and identities may differ.",
                        "Destination writes, deletes, changefeed and internal checkpoint access are not verified by this read-only check."],
    }
    if (kind, connector) not in SUPPORTED:
        result.update(status="unsupported", summary="Guided command generation is not available for this connector.")
        result["missing"] = [
            "For SharePoint, ask the tenant administrator for Sites.Selected application access and a read grant on the selected site."
            if connector == "sharepoint" else
            "Ask the service administrator to verify the configured identity and resource-scoped read/write permissions. Use the connector connection test."
        ]
        return result
    try:
        account = account_from_config(connector, config)
        if config.get("auth_type", "managed-identity") != "managed-identity":
            raise ValueError("This check supports managed identity only. Use the connection test for credential-based access.")
        if config.get("client_id") and config["client_id"] != os.getenv("AZURE_CLIENT_ID"):
            raise ValueError("The configured identity differs from the API workload identity. Verify access from that workload; no grant command can be safely inferred.")
        # Validate all scope components even when ARM discovery is unavailable.
        for name in (["container"] if connector == "azure-blob" else ["database", "container"]):
            value = config.get(name)
            if not isinstance(value, str) or not value or re.search(r"[/\\?#\x00-\x1f\x7f]", value):
                raise ValueError(f"Enter a valid {name} without path separators or control characters.")
        if resource_id:
            validate_resource_id(resource_id, connector, account)
        configured_principal = guid(os.getenv("AZURE_PRINCIPAL_ID"))
        if principal_id and not guid(principal_id):
            raise ValueError("Enter a valid principal/object ID.")
        if principal_id and configured_principal and guid(principal_id) != configured_principal:
            raise ValueError("Principal ID does not match the deployment workload identity.")
        principal_id = configured_principal or guid(principal_id)
    except ValueError as exc:
        result.update(status="needs_input", summary=str(exc), resource_id="")
        return result
    try:
        await asyncio.wait_for(asyncio.to_thread(probe, connector, config), timeout=15)
        result.update(status="read_verified", summary="Source read access verified." if kind == "source" else
                      "Destination metadata is readable. Write and delete permissions are not yet verified.")
        result["checks"].append({"name": "Source read" if kind == "source" else "Destination metadata read", "status": "passed"})
    except (AzureError, OSError, TimeoutError) as exc:
        code, summary = classify_error(exc)
        logger.info("Permission check failed for %s: %s (%s)", connector, code, type(exc).__name__)
        result.update(status=code, summary=summary)
        result["checks"].append({"name": "Service access", "status": "failed", "code": code})
    result["principal_id"] = principal_id
    result["identity_source"] = "deployment" if configured_principal else "customer-supplied; confirm this is the workload principal/object ID"
    if result["status"] != "access_denied":
        return result
    if not resource_id:
        try:
            resource_id, message = await asyncio.wait_for(resolve_resource_id(connector, account), timeout=8)
        except TimeoutError:
            logger.info("Permission resource discovery timed out")
            resource_id, message = "", "Resource discovery timed out. Paste the full account resource ID from Azure Portal."
        if message:
            result["missing"].append(message)
    if not principal_id:
        result["missing"].append("Enter the workload managed identity Object (principal) ID from Azure Portal > Managed Identities > Overview. Do not use the client ID.")
    result["resource_id"] = resource_id
    if resource_id and principal_id:
        result.update(grant_commands(kind, connector, config, resource_id, principal_id))
        result["verification"] += " Commands use a deterministic assignment ID for safe retries; existing broader grants are not removed."
    return result
