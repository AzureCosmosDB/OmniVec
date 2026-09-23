"""Process-wide Cosmos DB user-agent suffixes for OmniVec API services."""

from __future__ import annotations

import inspect
import os

import azure.cosmos
import azure.cosmos.cosmos_client as _cosmos_client
from azure.cosmos import CosmosClient as _CosmosClient


COSMOS_METADATA_USER_AGENT = "OmniVec-MetadataCosmos/1.0"
COSMOS_DATA_USER_AGENT = "OmniVec-DataCosmos/1.0"


def _cosmos_user_agent_suffix() -> str:
    for frame in inspect.stack(context=0)[2:]:
        filename = os.path.basename(frame.filename)
        if filename == "store.py":
            return COSMOS_METADATA_USER_AGENT
        if filename == "api.py" and frame.function == "get_changefeed_leases":
            return COSMOS_METADATA_USER_AGENT
    return COSMOS_DATA_USER_AGENT


class OmniVecCosmosClient(_CosmosClient):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("user_agent_suffix", _cosmos_user_agent_suffix())
        super().__init__(*args, **kwargs)


azure.cosmos.CosmosClient = OmniVecCosmosClient
_cosmos_client.CosmosClient = OmniVecCosmosClient
