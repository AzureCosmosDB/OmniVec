"""OneLake Iceberg read watcher.

OneLake's Iceberg REST catalog is read-only. This process scans it through
PyIceberg, sends OmniVec's self-contained Service Bus messages, and commits a
checkpoint in OneLake only after the corresponding messages are accepted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
from azure.identity import DefaultAzureCredential as SyncDefaultAzureCredential
from azure.identity.aio import DefaultAzureCredential
from azure.core.exceptions import ResourceExistsError
from azure.servicebus import ServiceBusMessage
from azure.servicebus.aio import ServiceBusClient
from azure.servicebus.exceptions import MessageSizeExceededError
from azure.storage.filedatalake.aio import DataLakeServiceClient
from pyiceberg.catalog import load_catalog

from helpers import (
    checkpoint_json, checkpoint_key, content_from_row, content_hash, message_id,
    pipeline_fingerprint, row_has_current_omnivec_embedding,
)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("omnivec.onelake_iceberg")

DEFAULT_WRITEBACK_COLUMNS = {
    "id_field": "id",
    "embedding_field": "embedding",
    "content_hash_field": "content_hash",
    "pipeline_id_field": "pipeline_id",
    "pipeline_generation_field": "pipeline_generation",
    "model_field": "embedding_model",
    "source_id_field": "source_id",
    "source_ref_field": "source_ref",
    "writer_marker_field": "omnivec_writer_marker",
    "run_id_field": "omnivec_run_id",
    "embedded_at_field": "embedded_at",
}


@dataclass
class Checkpoint:
    snapshot_id: str | None
    processed: dict[str, str]
    pipelines: dict[str, str]
    submitted_at: dict[str, float]
    known_refs: dict[str, list[str]]
    pipeline_revisions: dict[str, int]
    pending_empty_refs: dict[str, list[str]]


class OneLakeIcebergWatcher:
    def __init__(self) -> None:
        self.api_url = os.getenv("OMNIVEC_API_BASE_URL", "http://omnivec-api:80").rstrip("/")
        self.service_bus_namespace = os.environ["ONELAKE_ICEBERG_SERVICEBUS_NAMESPACE"]
        self.topic_name = os.getenv("ONELAKE_ICEBERG_SERVICEBUS_TOPIC", "embeddings")
        self.credential = DefaultAzureCredential()
        self.pyiceberg_credential = SyncDefaultAzureCredential()
        self._last_scan: dict[str, float] = {}

    async def run_forever(self) -> None:
        while True:
            try:
                await self.scan_all()
            except Exception:
                log.exception("OneLake Iceberg scan failed")
            await asyncio.sleep(int(os.getenv("ONELAKE_ICEBERG_DISCOVERY_INTERVAL_SECONDS", "30")))

    async def scan_all(self) -> None:
        async with httpx.AsyncClient(base_url=self.api_url, timeout=30) as client:
            sources = (await client.get("/api/sources")).json().get("sources", [])
            pipelines = (await client.get("/api/pipelines?include_stats=false")).json().get("pipelines", [])
            destinations = (await client.get("/api/destinations")).json().get("destinations", [])

        active = [p for p in pipelines if p.get("status") == "active"]
        dest_by_id = {
            d["id"]: d for d in destinations
            if d.get("enabled") and d.get("type") == "onelake-iceberg"
        }
        for source in sources:
            if source.get("type") != "onelake-iceberg" or not source.get("enabled"):
                continue
            now = asyncio.get_running_loop().time()
            poll_interval = int(source.get("config", {}).get("poll_interval_seconds", 60))
            if now - self._last_scan.get(source["id"], 0) < poll_interval:
                continue
            source_pipelines = [
                pipeline for pipeline in active
                if any(ps.get("source_id") == source["id"] for ps in pipeline.get("sources", []))
                and pipeline.get("destination_id") in dest_by_id
            ]
            if source_pipelines:
                await self.scan_source(source, source_pipelines, dest_by_id)
                self._last_scan[source["id"]] = now

    async def scan_source(
        self, source: dict[str, Any], pipelines: list[dict[str, Any]], destinations: dict[str, dict[str, Any]]
    ) -> None:
        config = source["config"]
        checkpoint = await self.load_checkpoint(source["id"], config)
        token = (await self.credential.get_token("https://storage.azure.com/.default")).token
        namespace = config["namespace"]
        namespace_tuple = tuple(namespace.split(".")) if isinstance(namespace, str) else tuple(namespace)
        catalog = load_catalog(
            f"omnivec-{source['id']}",
            type="rest",
            uri=config.get("catalog_uri", "https://onelake.table.fabric.microsoft.com/iceberg"),
            warehouse=config["warehouse"],
            token=token,
            **{
                "adls.account-name": "onelake",
                "adls.account-host": "onelake.blob.fabric.microsoft.com",
                "adls.credential": self.pyiceberg_credential,
            },
        )
        table = catalog.load_table((*namespace_tuple, config["table"]))
        current_snapshot = table.current_snapshot()
        snapshot_id = str(current_snapshot.snapshot_id) if current_snapshot else None
        source_version = int(current_snapshot.sequence_number) if current_snapshot else 0
        pipeline_state = {
            pipeline["id"]: pipeline_fingerprint(pipeline, destinations[pipeline["destination_id"]])
            for pipeline in pipelines
        }
        for pipeline_id, fingerprint in pipeline_state.items():
            if checkpoint.pipelines.get(pipeline_id) != fingerprint:
                checkpoint.pipeline_revisions[pipeline_id] = (
                    checkpoint.pipeline_revisions.get(pipeline_id, 0) + 1
                )
        retry_after = int(config.get("fabric_retry_interval_seconds", 900))
        now_epoch = time.time()
        has_expired_submission = any(
            now_epoch - submitted_at >= retry_after
            for submitted_at in checkpoint.submitted_at.values()
        )
        has_ref_baseline = all(
            pipeline["id"] in checkpoint.known_refs
            for pipeline in pipelines
        )
        if (
            snapshot_id == checkpoint.snapshot_id
            and pipeline_state == checkpoint.pipelines
            and has_ref_baseline
            and not has_expired_submission
        ):
            return

        projection = {config.get("id_field", "id"), *config.get("content_fields", ["content"])}
        for pipeline in pipelines:
            destination = destinations[pipeline["destination_id"]]
            writeback = {
                **DEFAULT_WRITEBACK_COLUMNS,
                **destination["config"].get("writeback_columns", {}),
            }
            projection.update(
                writeback[key]
                for key in (
                    "content_hash_field",
                    "pipeline_id_field",
                    "pipeline_generation_field",
                    "model_field",
                )
            )
        rows = table.scan(selected_fields=tuple(projection)).to_arrow().to_pylist()
        id_field = config.get("id_field", "id")
        rows_by_ref = {
            str(row.get(id_field)): row
            for row in rows
            if row.get(id_field) is not None and str(row.get(id_field))
        }
        all_refs = set(rows_by_ref)
        batch_size = int(config.get("batch_size", 200))
        source_fields = config.get("content_fields", ["content"])
        messages: list[tuple[dict[str, Any], str, str]] = []
        checkpoint_keys_to_remove: set[str] = set()
        writeback_signatures: set[tuple[str, ...]] = set()
        for pipeline in pipelines:
            pipeline_source = next(ps for ps in pipeline["sources"] if ps.get("source_id") == source["id"])
            fields = pipeline_source.get("content_fields") or source_fields
            fields = [field for field in fields if field in source_fields]
            generation = str(pipeline.get("generation", "1"))
            pipeline_revision = checkpoint.pipeline_revisions[pipeline["id"]]
            pipeline_changed = checkpoint.pipelines.get(pipeline["id"]) != pipeline_state[pipeline["id"]]
            destination = destinations[pipeline["destination_id"]]
            writeback = {
                **DEFAULT_WRITEBACK_COLUMNS,
                **destination["config"].get("writeback_columns", {}),
            }
            managed_fields = set(writeback.values())
            fields = [field for field in fields if field not in managed_fields]
            if not fields:
                log.warning("Pipeline %s has no user content fields allowed by source %s", pipeline["id"], source["id"])
                continue
            signature = tuple(sorted(managed_fields))
            if signature in writeback_signatures:
                raise ValueError(
                    f"Source {source['id']} has multiple active pipelines writing the same OneLake columns; "
                    "configure distinct write-back columns to prevent an embedding loop"
                )
            writeback_signatures.add(signature)
            current_refs: set[str] = set()
            for row in rows:
                source_ref = str(row.get(id_field, ""))
                if not source_ref:
                    continue
                content = content_from_row(row, fields)
                if not content:
                    continue
                current_refs.add(source_ref)
                digest = content_hash(content)
                key = checkpoint_key(pipeline["id"], pipeline_state[pipeline["id"]], source_ref)
                if row_has_current_omnivec_embedding(
                    row, digest, pipeline["id"], pipeline["docgrok_pipeline"], generation, writeback
                ) and not pipeline_changed:
                    checkpoint.processed.pop(key, None)
                    checkpoint.submitted_at.pop(key, None)
                    continue
                if (
                    checkpoint.processed.get(key) == digest
                    and now_epoch - checkpoint.submitted_at.get(key, 0) < retry_after
                ):
                    continue
                message = {
                    "message_id": message_id(
                        pipeline["id"], source["id"], source_ref, digest, source_version
                    ),
                    "pipeline_id": pipeline["id"],
                    "pipeline_name": pipeline["name"],
                    "docgrok_pipeline": pipeline["docgrok_pipeline"],
                    "source_id": source["id"],
                    "source_ref": source_ref,
                    "destination_id": destination["id"],
                    "destination_type": destination["type"],
                    "destination_config": destination["config"],
                    "content": content,
                    "content_hash": digest,
                    "source_version": source_version,
                    "pipeline_revision": pipeline_revision,
                    "partition_key_value": source_ref,
                    "pipeline_generation": generation,
                    "source_content_fields": {
                        field: str(row[field]) for field in fields if row.get(field) is not None
                    },
                }
                messages.append((message, key, digest))

            previous_refs = set(checkpoint.known_refs.get(pipeline["id"], []))
            deleted_refs = previous_refs - all_refs
            pending_empty_refs = set(checkpoint.pending_empty_refs.get(pipeline["id"], []))
            pending_empty_refs.difference_update(current_refs)
            pending_empty_refs.update((previous_refs - current_refs) & all_refs)
            cleared_empty_refs = {
                source_ref
                for source_ref in pending_empty_refs
                if rows_by_ref[source_ref].get(writeback["content_hash_field"]) is None
                and rows_by_ref[source_ref].get(writeback["pipeline_id_field"]) is None
            }
            pending_empty_refs.difference_update(cleared_empty_refs)
            checkpoint_keys_to_remove.update(
                checkpoint_key(
                    pipeline["id"], pipeline_state[pipeline["id"]], source_ref
                )
                for source_ref in cleared_empty_refs
            )
            for source_ref in sorted(deleted_refs | pending_empty_refs):
                delete_digest = content_hash(f"delete:{source_ref}")
                key = checkpoint_key(pipeline["id"], pipeline_state[pipeline["id"]], source_ref)
                if source_ref in deleted_refs:
                    checkpoint_keys_to_remove.add(key)
                if (
                    source_ref in pending_empty_refs
                    and checkpoint.processed.get(key) == delete_digest
                    and now_epoch - checkpoint.submitted_at.get(key, 0) < retry_after
                ):
                    continue
                messages.append(({
                    "message_id": message_id(
                        pipeline["id"], source["id"], source_ref, delete_digest, source_version
                    ),
                    "pipeline_id": pipeline["id"],
                    "pipeline_name": pipeline["name"],
                    "docgrok_pipeline": pipeline["docgrok_pipeline"],
                    "source_id": source["id"],
                    "source_ref": source_ref,
                    "destination_id": destination["id"],
                    "destination_type": destination["type"],
                    "destination_config": destination["config"],
                    "content": "",
                    "content_hash": delete_digest,
                    "source_version": source_version,
                    "pipeline_revision": pipeline_revision,
                    "partition_key_value": source_ref,
                    "pipeline_generation": generation,
                    "source_content_fields": {},
                    "message_type": "delete",
                }, key, delete_digest))
            checkpoint.known_refs[pipeline["id"]] = sorted(current_refs)
            checkpoint.pending_empty_refs[pipeline["id"]] = sorted(pending_empty_refs)

        async with ServiceBusClient(self.service_bus_namespace, self.credential) as service_bus:
            sender = service_bus.get_topic_sender(self.topic_name)
            async with sender:
                for start in range(0, len(messages), batch_size):
                    chunk = messages[start : start + batch_size]
                    batch = await sender.create_message_batch()
                    for message, _, _ in chunk:
                        outgoing = ServiceBusMessage(json.dumps(message, separators=(",", ":")))
                        try:
                            batch.add_message(outgoing)
                        except MessageSizeExceededError:
                            if len(batch) == 0:
                                raise  # one message cannot fit; do not checkpoint it
                            await sender.send_messages(batch)
                            batch = await sender.create_message_batch()
                            batch.add_message(outgoing)
                    if len(batch):
                        await sender.send_messages(batch)
                    for _, key, digest in chunk:
                        checkpoint.processed[key] = digest
                        checkpoint.submitted_at[key] = now_epoch

        for key in checkpoint_keys_to_remove:
            checkpoint.processed.pop(key, None)
            checkpoint.submitted_at.pop(key, None)
        checkpoint.snapshot_id = snapshot_id
        checkpoint.pipelines = pipeline_state
        await self.save_checkpoint(source["id"], config, checkpoint)
        log.info("Scanned source=%s snapshot=%s published=%s", source["id"], snapshot_id, len(messages))

    @staticmethod
    def _checkpoint_location(source_id: str, config: dict[str, Any]) -> tuple[str, str, str]:
        workspace_id, data_item_id = config["warehouse"].strip("/").split("/", 1)
        account_url = config.get("checkpoint_account_url", "https://onelake.dfs.fabric.microsoft.com")
        file_system = config.get("checkpoint_file_system") or workspace_id
        root = config.get("checkpoint_path", ".omnivec/checkpoints").strip("/")
        return account_url, file_system, f"{data_item_id}/Files/{root}/{source_id}.json"

    async def load_checkpoint(self, source_id: str, config: dict[str, Any]) -> Checkpoint:
        account_url, file_system, path = self._checkpoint_location(source_id, config)
        service = DataLakeServiceClient(account_url, credential=self.credential)
        async with service:
            file = service.get_file_system_client(file_system).get_file_client(path)
            try:
                downloaded = await file.download_file()
                raw = json.loads((await downloaded.readall()).decode("utf-8"))
                return Checkpoint(
                    raw.get("snapshot_id"),
                    raw.get("processed", {}),
                    raw.get("pipelines", {}),
                    raw.get("submitted_at", {}),
                    raw.get("known_refs", {}),
                    raw.get("pipeline_revisions", {}),
                    raw.get("pending_empty_refs", {}),
                )
            except Exception as exc:
                # A missing first checkpoint is expected; auth and service errors must be visible.
                if getattr(exc, "status_code", None) == 404:
                    return Checkpoint(None, {}, {}, {}, {}, {}, {})
                raise

    async def save_checkpoint(self, source_id: str, config: dict[str, Any], checkpoint: Checkpoint) -> None:
        account_url, file_system, path = self._checkpoint_location(source_id, config)
        service = DataLakeServiceClient(account_url, credential=self.credential)
        async with service:
            filesystem = service.get_file_system_client(file_system)
            directory = path.rsplit("/", 1)[0]
            # Create each checkpoint directory segment. OneLake's DFS endpoint
            # permits only paths below the configured workspace/item and does
            # not expose a container-create operation to this watcher.
            segments = directory.split("/")
            files_index = segments.index("Files")
            for index in range(files_index + 2, len(segments) + 1):
                try:
                    await filesystem.get_directory_client("/".join(segments[:index])).create_directory()
                except ResourceExistsError:
                    pass
            file = filesystem.get_file_client(path)
            await file.upload_data(
                checkpoint_json(
                    checkpoint.snapshot_id,
                    checkpoint.processed,
                    checkpoint.pipelines,
                    checkpoint.submitted_at,
                    checkpoint.known_refs,
                    checkpoint.pipeline_revisions,
                    checkpoint.pending_empty_refs,
                ),
                overwrite=True,
            )


if __name__ == "__main__":
    asyncio.run(OneLakeIcebergWatcher().run_forever())
