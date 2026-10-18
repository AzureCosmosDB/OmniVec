"""Private candidate retrieval followed by live delegated SharePoint authorization."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import quote, urlsplit

import requests
from azure.cosmos.exceptions import CosmosResourceNotFoundError

from cosmos_tools import normalize_filters, normalize_top_k, parse_fields, public_document
from mcp_auth import (
    AuthenticationRequired,
    AuthorizationUnavailable,
    Caller,
    canonical_guid,
    encode_claims,
)


PROVENANCE = "_omnivec_provenance"
SYNC = "_omnivec_sync"
MAX_CANDIDATES = 50
AUTHORIZATION_SECONDS = 20


def _string(value: object, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("Invalid SharePoint identifier")
    return value


@dataclass(frozen=True)
class Provenance:
    tenant_id: str
    site_id: str
    drive_id: str
    item_id: str
    etag: str

    @classmethod
    def parse(cls, value: object) -> Provenance:
        if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
                or value["schema_version"] != 1 or value.get("provider") != "sharepoint"):
            raise ValueError("Missing SharePoint provenance")
        return cls(
            canonical_guid(value.get("resource_tenant_id")),
            _string(value.get("site_id"), 512), _string(value.get("drive_id"), 256),
            _string(value.get("item_id"), 256), _string(value.get("source_etag"), 2048),
        )


class SharePointAuthorizer:
    def __init__(self, caller: Caller, token_provider: Callable[[], str]):
        self.caller = caller
        self._token_provider = token_provider
        self._token: str | None = None
        self._decisions: dict[Provenance, bool] = {}
        self._deadline = time.monotonic() + AUTHORIZATION_SECONDS
        self._session = requests.Session()

    def close(self) -> None:
        self._session.close()

    def remaining(self) -> float:
        seconds = self._deadline - time.monotonic()
        if seconds <= 0:
            raise AuthorizationUnavailable()
        return seconds

    def _get(self, url: str, *, metadata: bool) -> dict[str, Any] | bool:
        for attempt in range(3):
            self.remaining()
            if self._token is None:
                self._token = self._token_provider()
            try:
                response = self._session.get(
                    url, headers={"Authorization": f"Bearer {self._token}"},
                    allow_redirects=False, stream=True, timeout=min(5, self.remaining()),
                )
            except requests.RequestException:
                logging.warning("SharePoint authorization transport unavailable")
                raise AuthorizationUnavailable() from None
            try:
                status = response.status_code
                header = response.headers.get("WWW-Authenticate", "")
                scheme, _, challenge = header.partition(" ")
                parameters = requests.utils.parse_dict_header(challenge) if scheme.lower() == "bearer" else {}
                if status == 401 or (status == 403 and "claims" in parameters):
                    raise AuthenticationRequired(encode_claims(parameters.get("claims"), encoded=True))
                if status in {403, 404, 410}:
                    return False
                if status == 429 or status >= 500:
                    if attempt == 2:
                        logging.warning("SharePoint authorization retries exhausted")
                        raise AuthorizationUnavailable()
                    retry_after = response.headers.get("Retry-After")
                    delay = 0.25 * (2 ** attempt)
                    if retry_after:
                        try:
                            delay = max(0, float(retry_after))
                        except ValueError:
                            try:
                                delay = max(0, (
                                    parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)
                                ).total_seconds())
                            except (ValueError, TypeError, OverflowError):
                                raise AuthorizationUnavailable() from None
                    if delay >= self.remaining():
                        raise AuthorizationUnavailable()
                elif metadata and status == 200:
                    try:
                        value = response.json()
                    except ValueError:
                        raise AuthorizationUnavailable() from None
                    if not isinstance(value, dict):
                        raise AuthorizationUnavailable()
                    return value
                elif not metadata and status == 200:
                    return True
                elif not metadata and status == 302:
                    # A fresh delegated content request authorizes the download. Never follow,
                    # retain or log its preauthenticated Location, or read the file body.
                    location = urlsplit(response.headers.get("Location", ""))
                    if location.scheme != "https" or not location.hostname:
                        raise AuthorizationUnavailable()
                    return True
                else:
                    logging.warning("Unexpected SharePoint authorization response: %s", status)
                    raise AuthorizationUnavailable()
            finally:
                response.close()
            time.sleep(delay)
        raise AuthorizationUnavailable()

    def _current(self, url: str, provenance: Provenance) -> bool:
        item = self._get(url + "?$select=id,eTag,file,deleted,malware,sharepointIds", metadata=True)
        if item is False:
            return False
        if not isinstance(item, dict):
            raise AuthorizationUnavailable()
        ids = item.get("sharepointIds")
        return (
            item.get("id") == provenance.item_id and item.get("eTag") == provenance.etag
            and isinstance(item.get("file"), dict) and "deleted" not in item and "malware" not in item
            and isinstance(ids, dict) and isinstance(ids.get("tenantId"), str)
            and ids["tenantId"].lower() == provenance.tenant_id
        )

    def allowed(self, provenance: Provenance) -> bool:
        self.remaining()
        if provenance.tenant_id != self.caller.tenant_id:
            return False
        if provenance not in self._decisions:
            url = (
                f"https://graph.microsoft.com/v1.0/sites/{quote(provenance.site_id, safe='')}"
                f"/drives/{quote(provenance.drive_id, safe='')}/items/{quote(provenance.item_id, safe='')}"
            )
            self._decisions[provenance] = (
                self._current(url, provenance)
                and self._get(url + "/content", metadata=False) is True
                and self._current(url, provenance)
            )
        return self._decisions[provenance]


def candidate_query(partition_path: str, vector_field: str | None, filters: dict[str, str]) -> str:
    parts = partition_path.removeprefix("/").split("/")
    if not partition_path.startswith("/") or not all(parts):
        raise ValueError("SharePoint requires a dedicated document partition key")
    partition = "c" + "".join(f"[{json.dumps(part)}]" for part in parts)
    projection = f"c.id, c.source_id, c.pipeline_id, c.source_ref, c.{PROVENANCE}, c.{SYNC}, {partition} AS _partition_key"
    predicates = [
        f"c.{SYNC}.kind = 'chunk'", f"c.{PROVENANCE}.schema_version = 1",
        f"c.{PROVENANCE}.provider = 'sharepoint'", f"c.{PROVENANCE}.resource_tenant_id = @tenant",
    ]
    predicates.extend(f"c.{name} = @{name}" for name in normalize_filters(filters))
    order = "c.id"
    if vector_field is not None:
        if parse_fields(vector_field) != (vector_field,):
            raise ValueError("vector_field must be a top-level property name")
        predicates.append(f"IS_DEFINED(c.{vector_field})")
        order = f"VectorDistance(c.{vector_field}, @embedding)"
        projection += f", {order} AS distance"
    return f"SELECT TOP {MAX_CANDIDATES} {projection} FROM c WHERE {' AND '.join(predicates)} ORDER BY {order}"


def _identity(row: dict[str, Any]) -> tuple[str, int]:
    sync = row.get(SYNC)
    if not isinstance(sync, dict) or sync.get("kind") != "chunk":
        raise ValueError("Not a SharePoint chunk")
    identity = _string(sync.get("identity"), 100)
    revision = sync.get("revision")
    if (not re.fullmatch(r"sp-[a-f0-9]{64}", identity)
            or type(revision) is not int or revision <= 0
            or row.get("_partition_key") != identity
            or not re.fullmatch(re.escape(f"{identity}-{revision}-chunk-") + r"\d+", row.get("id", ""))):
        raise ValueError("Invalid SharePoint synchronization identity")
    _string(row.get("source_id"), 2048)
    _string(row.get("pipeline_id"), 2048)
    return identity, revision


def _manifest_matches(client, candidate: dict[str, Any], identity: str, revision: int) -> bool:
    try:
        state = client.read_item(identity + "-state", partition_key=identity)
    except CosmosResourceNotFoundError:
        return False
    sync = state.get(SYNC)
    return (
        isinstance(sync, dict) and sync.get("kind") == "state" and sync.get("completed") is True
        and sync.get("identity") == identity and sync.get("revision") == revision
        and all(state.get(key) == candidate.get(key) for key in ("source_id", "pipeline_id", "source_ref", PROVENANCE))
    )


def protected_documents(
    client,
    authorizer: SharePointAuthorizer,
    fields: str,
    allowed_fields: tuple[str, ...],
    top_k: int,
    *,
    vector_field: str | None = None,
    embedding: list[float] | None = None,
    filters: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    limit = normalize_top_k(top_k)
    selected = parse_fields(fields)
    if any(name not in allowed_fields or name.startswith("_") or name == vector_field for name in selected):
        raise ValueError("Requested fields are not permitted for protected SharePoint results")
    properties = client.read()
    paths = properties.get("partitionKey", {}).get("paths", [])
    if len(paths) != 1 or not isinstance(paths[0], str):
        raise ValueError("Protected SharePoint retrieval requires one document partition key")
    normalized = normalize_filters(filters)
    parameters = [
        {"name": "@tenant", "value": authorizer.caller.tenant_id},
        *[{"name": f"@{name}", "value": value} for name, value in normalized.items()],
    ]
    if vector_field is not None:
        parameters.append({"name": "@embedding", "value": embedding})
    rows = client.query_items(
        query=candidate_query(paths[0], vector_field, normalized),
        parameters=parameters, enable_cross_partition_query=True,
    )
    results = []
    for index, candidate in enumerate(rows):
        if index >= MAX_CANDIDATES or len(results) >= limit:
            break
        authorizer.remaining()
        try:
            provenance = Provenance.parse(candidate.get(PROVENANCE))
            identity, revision = _identity(candidate)
        except (ValueError, TypeError):
            logging.warning("Excluded a SharePoint candidate with invalid provenance")
            continue
        if (provenance.tenant_id != authorizer.caller.tenant_id
                or not _manifest_matches(client, candidate, identity, revision)
                or not authorizer.allowed(provenance)):
            continue
        try:
            document = client.read_item(candidate["id"], partition_key=identity)
        except CosmosResourceNotFoundError:
            continue
        if (any(document.get(key) != candidate.get(key) for key in
                ("id", "source_id", "pipeline_id", "source_ref", PROVENANCE, SYNC))
                or not _manifest_matches(client, candidate, identity, revision)):
            continue
        result = {name: document[name] for name in selected if name in document}
        if vector_field is not None:
            result["distance"] = candidate["distance"]
        results.append(public_document(result))
    return results
