"""Stream Fabric Lakehouse Delta Change Data Feed events to OmniVec.

The existing PyIceberg watcher performs the initial prefill and periodic
recovery scans. This long-running Spark job handles low-latency inserts,
updates, and deletes after CDF is enabled on the physical Delta table.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import quote, quote_plus

import requests
from pyspark.sql import DataFrame, SparkSession, functions as F

from onelake_delta_cdf_helpers import build_messages, ordered_source_version


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-table", required=True)
    parser.add_argument("--checkpoint-location", required=True)
    parser.add_argument("--service-bus-namespace", required=True)
    parser.add_argument("--service-bus-topic", default="embeddings")
    parser.add_argument("--pipeline-config-base64", required=True)
    parser.add_argument("--trigger-interval-seconds", type=int, default=10)
    parser.add_argument("--starting-version", type=int)
    parser.add_argument("--max-files-per-trigger", type=int, default=1000)
    parser.add_argument(
        "--service-bus-auth-mode",
        choices=("aad", "sas"),
        default="aad",
    )
    parser.add_argument("--service-bus-key-vault-url")
    parser.add_argument("--service-bus-connection-secret-name")
    return parser.parse_args()


def _get_secret(vault_url: str, secret_name: str) -> str:
    try:
        import notebookutils

        return notebookutils.credentials.getSecret(vault_url, secret_name)
    except ImportError:
        import mssparkutils

        return mssparkutils.credentials.getSecret(vault_url, secret_name)


def _get_service_bus_access_token() -> str:
    try:
        import notebookutils

        return notebookutils.credentials.getToken("https://servicebus.azure.net/")
    except ImportError:
        import mssparkutils

        return mssparkutils.credentials.getToken("https://servicebus.azure.net/")


def _parse_connection_string(connection_string: str) -> dict[str, str]:
    values = {}
    for component in connection_string.split(";"):
        if not component:
            continue
        key, separator, value = component.partition("=")
        if not separator:
            raise ValueError("Invalid Service Bus connection string")
        values[key] = value
    required = {"Endpoint", "SharedAccessKeyName", "SharedAccessKey"}
    if not required.issubset(values):
        raise ValueError(
            "Service Bus connection string must include Endpoint, "
            "SharedAccessKeyName, and SharedAccessKey"
        )
    return values


def _create_sas_token(
    resource_uri: str,
    key_name: str,
    key: str,
    ttl_seconds: int = 3600,
) -> str:
    expires = int(time.time()) + ttl_seconds
    encoded_uri = quote_plus(resource_uri.lower())
    signature = base64.b64encode(
        hmac.new(
            key.encode("utf-8"),
            f"{encoded_uri}\n{expires}".encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("utf-8")
    return (
        f"SharedAccessSignature sr={encoded_uri}"
        f"&sig={quote_plus(signature)}&se={expires}&skn={quote_plus(key_name)}"
    )


def _send_message(
    endpoint: str,
    token: str,
    message: dict[str, Any],
) -> None:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
        "BrokerProperties": json.dumps(
            {
                "MessageId": message["message_id"],
                "Label": message["pipeline_id"],
            },
            separators=(",", ":"),
        ),
    }
    headers["Authorization"] = token
    response = requests.post(
        endpoint,
        headers=headers,
        data=json.dumps(message, separators=(",", ":")).encode("utf-8"),
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            "Service Bus send failed "
            f"status={response.status_code} "
            f"server_date={response.headers.get('Date', '')!r} "
            f"body={response.text[:500]!r}"
        )


def _publish_microbatch(
    batch: DataFrame,
    batch_id: int,
    plans: list[dict[str, Any]],
    endpoint: str,
    auth_mode: str,
    connection: dict[str, str] | None,
) -> None:
    rows = batch.orderBy("_commit_version", "_commit_timestamp").collect()
    messages: list[dict[str, Any]] = []
    for row in rows:
        record = row.asDict(recursive=True)
        source_version = ordered_source_version(
            record["_commit_timestamp"],
            int(record["_commit_version"]),
        )
        messages.extend(
            build_messages(
                record,
                record["_change_type"],
                source_version,
                plans,
            )
        )
    if not messages:
        print(f"OmniVec CDF batch {batch_id}: no source-content changes")
        return

    resource_uri = endpoint.removesuffix("/messages")
    send_concurrency = min(8, max(1, len(messages)))
    token_refresh_batch_size = 1000
    for start in range(0, len(messages), token_refresh_batch_size):
        if auth_mode == "aad":
            authorization = f"Bearer {_get_service_bus_access_token()}"
        else:
            if connection is None:
                raise ValueError("SAS authentication requires a connection string")
            authorization = _create_sas_token(
                resource_uri,
                connection["SharedAccessKeyName"],
                connection["SharedAccessKey"],
            )
        chunk = messages[start : start + token_refresh_batch_size]
        with ThreadPoolExecutor(max_workers=send_concurrency) as executor:
            futures = [
                executor.submit(_send_message, endpoint, authorization, message)
                for message in chunk
            ]
            for future in futures:
                future.result()
    print(f"OmniVec CDF batch {batch_id}: published {len(messages)} messages")


def main() -> None:
    args = parse_args()
    if args.trigger_interval_seconds < 1:
        raise ValueError("trigger-interval-seconds must be at least 1")
    if args.max_files_per_trigger < 1:
        raise ValueError("max-files-per-trigger must be at least 1")

    plans = json.loads(
        base64.b64decode(args.pipeline_config_base64).decode("utf-8")
    )
    if not isinstance(plans, list) or not plans:
        raise ValueError("pipeline-config-base64 must decode to a non-empty list")
    connection = None
    if args.service_bus_auth_mode == "sas":
        if not (
            args.service_bus_key_vault_url
            and args.service_bus_connection_secret_name
        ):
            raise ValueError(
                "SAS authentication requires Key Vault URL and secret name"
            )
        connection = _parse_connection_string(
            _get_secret(
                args.service_bus_key_vault_url,
                args.service_bus_connection_secret_name,
            )
        )

    spark = SparkSession.builder.getOrCreate()
    table_properties = {
        row.key: row.value
        for row in spark.sql(f"SHOW TBLPROPERTIES {args.target_table}").collect()
    }
    if table_properties.get("delta.enableChangeDataFeed", "").lower() != "true":
        raise RuntimeError(
            f"Delta Change Data Feed is not enabled on {args.target_table}; run "
            f"ALTER TABLE {args.target_table} SET TBLPROPERTIES "
            "('delta.enableChangeDataFeed' = 'true') before starting this job"
        )

    stream = (
        spark.readStream.format("delta")
        .option("readChangeFeed", "true")
        .option("maxFilesPerTrigger", args.max_files_per_trigger)
    )
    if args.starting_version is not None:
        stream = stream.option("startingVersion", args.starting_version)
    changes = stream.table(args.target_table).filter(
        F.col("_change_type").isin("insert", "update_postimage", "delete")
    )

    namespace = args.service_bus_namespace.removesuffix(".servicebus.windows.net")
    expected_endpoint = f"sb://{namespace}.servicebus.windows.net/"
    if connection is not None and (
        connection["Endpoint"].lower() != expected_endpoint.lower()
    ):
        raise ValueError(
            "Service Bus connection string Endpoint does not match "
            "--service-bus-namespace"
        )
    endpoint = (
        f"https://{namespace}.servicebus.windows.net/"
        f"{quote(args.service_bus_topic, safe='')}/messages"
    )
    query = (
        changes.writeStream.foreachBatch(
            lambda batch, batch_id: _publish_microbatch(
                batch,
                batch_id,
                plans,
                endpoint,
                args.service_bus_auth_mode,
                connection,
            )
        )
        .option("checkpointLocation", args.checkpoint_location)
        .trigger(processingTime=f"{args.trigger_interval_seconds} seconds")
        .start()
    )
    while query.isActive:
        query.awaitTermination(30)
        time.sleep(1)
    query.awaitTermination()


if __name__ == "__main__":
    main()
