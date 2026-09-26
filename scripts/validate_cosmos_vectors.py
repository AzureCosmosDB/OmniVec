"""Validate an isolated OmniVec Cosmos DB source and vector destination."""

import asyncio
import math
import os
import sys
from collections import Counter

from azure.cosmos.aio import CosmosClient
from azure.identity.aio import DefaultAzureCredential


ENDPOINT = os.environ.get("COSMOS_ENDPOINT") or sys.exit("COSMOS_ENDPOINT is required")
DATABASE = os.environ.get("COSMOS_DATABASE", "testdb")
SOURCE_CONTAINER = os.environ.get("SOURCE_CONTAINER", "local-bge-load-source")
DESTINATION_CONTAINER = os.environ.get(
    "DESTINATION_CONTAINER",
    "local-bge-load-vectors",
)
SOURCE_ID_PREFIX = os.environ.get("SOURCE_ID_PREFIX", "local-bge-10k-")
SOURCE_ID = os.environ.get("SOURCE_ID")
EXPECTED_COUNT = int(os.environ.get("EXPECTED_COUNT", "10000"))
EXPECTED_SOURCE_COUNT = int(os.environ.get("EXPECTED_SOURCE_COUNT", EXPECTED_COUNT))
EXPECTED_DESTINATION_COUNT = int(
    os.environ.get("EXPECTED_DESTINATION_COUNT", EXPECTED_COUNT)
)
EXPECTED_DIMENSIONS = int(os.environ.get("EXPECTED_DIMENSIONS", "384"))


async def query_scalar(container, query):
    values = [value async for value in container.query_items(query)]
    return values[0] if values else 0


async def main():
    credential = DefaultAzureCredential()
    client = CosmosClient(ENDPOINT, credential)
    database = client.get_database_client(DATABASE)
    source = database.get_container_client(SOURCE_CONTAINER)
    destination = database.get_container_client(DESTINATION_CONTAINER)

    source_count = await query_scalar(
        source,
        "SELECT VALUE COUNT(1) FROM c "
        f"WHERE STARTSWITH(c.id, '{SOURCE_ID_PREFIX}')",
    )

    ids = Counter()
    invalid_vectors = 0
    invalid_vector_ids = []
    missing_partition_keys = 0
    destination_count = 0
    sample_keys = None

    query = "SELECT c.id, c.document_id, c.partition_id, c.embedding FROM c"
    if SOURCE_ID:
        query += f" WHERE c.source_id = '{SOURCE_ID}'"
    async for item in destination.query_items(query):
        destination_count += 1
        if sample_keys is None:
            sample_keys = sorted(item)

        item_id = item.get("id")
        document_id = item.get("document_id")
        if item_id:
            ids[item_id] += 1
        if not document_id:
            missing_partition_keys += 1

        vector = item.get("embedding")
        if (
            not isinstance(vector, list)
            or len(vector) != EXPECTED_DIMENSIONS
            or any(
                not isinstance(value, (int, float)) or not math.isfinite(value)
                for value in vector
            )
        ):
            invalid_vectors += 1
            if len(invalid_vector_ids) < 10:
                invalid_vector_ids.append(item_id)

    duplicate_ids = sum(count - 1 for count in ids.values() if count > 1)
    passed = all(
        (
            source_count == EXPECTED_SOURCE_COUNT,
            destination_count == EXPECTED_DESTINATION_COUNT,
            invalid_vectors == 0,
            missing_partition_keys == 0,
            duplicate_ids == 0,
        )
    )

    print(f"source_count={source_count}")
    print(f"destination_count={destination_count}")
    print(f"invalid_vectors={invalid_vectors}")
    print(f"invalid_vector_ids={invalid_vector_ids}")
    print(f"missing_partition_keys={missing_partition_keys}")
    print(f"duplicate_ids={duplicate_ids}")
    print(f"sample_keys={sample_keys}")
    print(f"result={'PASS' if passed else 'WAIT_OR_FAIL'}")

    await client.close()
    await credential.close()
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
