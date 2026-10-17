"""Stage-1 broker lane protocol. Does not mutate foreign subscriptions or rules."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote
import re

import httpx
from azure.servicebus.management import CorrelationRuleFilter
from azure.servicebus import ServiceBusMessage
from azure.servicebus.aio.management import ServiceBusAdministrationClient

TOPIC_MARKER = "omnivec-pipeline-lanes-v1"
MAX_LANES = 128
SB_XML = "http://schemas.microsoft.com/netservices/2010/10/servicebus/connect"
XSI_XML = "http://www.w3.org/2001/XMLSchema-instance"


def broker_message(message: dict, lane_mode: bool) -> ServiceBusMessage:
    return ServiceBusMessage(
        json.dumps(message, separators=(",", ":")),
        **({"message_id": message["message_id"], "subject": message["pipeline_id"],
            "content_type": "application/json"} if lane_mode else {}),
    )


def atomic_subscription_xml(metadata: dict) -> bytes:
    """Python SDK cannot atomically create-with-rule; mirror the .NET REST subscription PUT."""
    atom = "http://www.w3.org/2005/Atom"
    root = ET.Element(f"{{{atom}}}entry")
    content = ET.SubElement(root, f"{{{atom}}}content", {"type": "application/xml"})
    description = ET.SubElement(content, f"{{{SB_XML}}}SubscriptionDescription")
    def field(name: str, value: str):
        ET.SubElement(description, f"{{{SB_XML}}}{name}").text = value
    field("LockDuration", "PT5M")
    field("RequiresSession", "false")
    field("DefaultMessageTimeToLive", "P1D")
    field("DeadLetteringOnMessageExpiration", "true")
    field("DeadLetteringOnFilterEvaluationExceptions", "false")
    rule = ET.SubElement(description, f"{{{SB_XML}}}DefaultRuleDescription")
    filter_ = ET.SubElement(rule, f"{{{SB_XML}}}Filter", {f"{{{XSI_XML}}}type": "CorrelationFilter"})
    ET.SubElement(filter_, f"{{{SB_XML}}}Label").text = metadata["pipeline_id"]
    ET.SubElement(rule, f"{{{SB_XML}}}Action", {f"{{{XSI_XML}}}type": "EmptyRuleAction"})
    ET.SubElement(rule, f"{{{SB_XML}}}Name").text = "pipeline"
    field("MaxDeliveryCount", "10")
    field("EnableBatchedOperations", "true")
    field("Status", "Active")
    field("UserMetadata", json.dumps(metadata, separators=(",", ":")))
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def subscription_name(pipeline_id: str) -> str:
    if not isinstance(pipeline_id, str) or not pipeline_id.strip():
        raise ValueError("Lane requires an exact nonempty pipeline ID")
    return "p-" + hashlib.sha256(pipeline_id.encode("utf-8")).hexdigest()[:32]


def validate_topic_name(topic: str) -> None:
    if not topic or not topic.strip() or topic.lower() == "embeddings":
        raise ValueError("Lane mode requires an explicitly configured separate marked topic")


def lane_metadata(pipeline: dict, policy: dict) -> dict:
    return {"routing_version": 1, "pipeline_id": pipeline["id"], **{
        key: policy[key] for key in (
            "resource_weight", "max_concurrency_per_worker", "resource_priority", "workload_class"
        )
    }}


def validate_metadata(name: str, metadata: dict) -> None:
    if (
        metadata.get("routing_version") != 1
        or name != subscription_name(metadata.get("pipeline_id"))
        or type(metadata.get("resource_weight")) is not int
        or not 1 <= metadata["resource_weight"] <= 100
        or type(metadata.get("max_concurrency_per_worker")) is not int
        or not 1 <= metadata["max_concurrency_per_worker"] <= 32
        or metadata.get("resource_priority") not in ("low", "normal", "high", "critical")
        or metadata.get("workload_class") not in ("shared", "burst", "dedicated")
    ):
        raise ValueError(f"Invalid lane ownership/metadata: {name}")


def validate_rules(pipeline_id: str, rules: list) -> None:
    if len(rules) != 1:
        raise ValueError("Lane must have exactly one subject-only correlation rule")
    rule = rules[0]
    filter_ = rule.filter
    if (
        not isinstance(filter_, CorrelationRuleFilter)
        or filter_.label != pipeline_id
        or any(getattr(filter_, key, None) is not None for key in (
            "correlation_id", "message_id", "to", "reply_to", "session_id",
            "reply_to_session_id", "content_type",
        ))
        or filter_.properties
        or rule.action is not None
    ):
        raise ValueError("Lane rule is not an exact subject-only correlation rule")


class LaneRegistry:
    def __init__(self, namespace: str, credential, topic: str, threshold: int = 5000):
        validate_topic_name(topic)
        if threshold <= 0:
            raise ValueError("Lane backpressure threshold must be positive")
        self.admin = ServiceBusAdministrationClient(namespace, credential)
        if not re.fullmatch(r"[A-Za-z0-9.-]+", namespace):
            raise ValueError("Service Bus namespace must be a fully qualified hostname")
        self.namespace, self.credential = namespace, credential
        self.topic = topic
        self.threshold = threshold
        self._gate = asyncio.Lock()
        self._expires = 0.0
        self._cache: dict[str, dict] = {}

    async def _refresh(self) -> dict:
        if time.monotonic() < self._expires:
            return self._cache
        properties = await self.admin.get_topic(self.topic)
        if properties.user_metadata != TOPIC_MARKER:
            raise ValueError("Lane topic is not marked omnivec-pipeline-lanes-v1")
        discovered = {}
        async for subscription in self.admin.list_subscriptions(self.topic):
            if len(discovered) >= MAX_LANES:
                raise ValueError("Lane count exceeds stage-1 limit (128)")
            discovered[subscription.name] = await self._validate_subscription(subscription)
        self._cache = discovered
        self._expires = time.monotonic() + 30
        return discovered

    async def _validate_subscription(self, subscription) -> dict:
        if (
            subscription.requires_session or subscription.forward_to
            or subscription.forward_dead_lettered_messages_to or subscription.status != "Active"
        ):
            raise ValueError(f"Lane must be active, non-session and non-forwarding: {subscription.name}")
        metadata = json.loads(subscription.user_metadata)
        validate_metadata(subscription.name, metadata)
        rules = []
        async for rule in self.admin.list_rules(self.topic, subscription.name):
            rules.append(rule)
            if len(rules) > 1:
                break
        validate_rules(metadata["pipeline_id"], rules)
        return metadata

    async def ensure_capacity(self, metadata: dict) -> None:
        name = subscription_name(metadata["pipeline_id"])
        validate_metadata(name, metadata)
        async with self._gate:
            lanes = await self._refresh()
            if name not in lanes:
                if len(lanes) >= MAX_LANES:
                    raise ValueError("Lane count exceeds stage-1 limit (128)")
                await self._create_atomic(name, metadata)
                subscription = await self.admin.get_subscription(self.topic, name)
                lanes[name] = await self._validate_subscription(subscription)
            if lanes.get(name) != metadata:
                raise ValueError(f"Lane metadata differs; explicit reconciled migration required: {name}")
            runtime = await self.admin.get_subscription_runtime_properties(self.topic, name)
            if runtime.active_message_count >= self.threshold:
                raise RuntimeError(f"Pipeline-local backpressure: {metadata['pipeline_id']}")

    async def _create_atomic(self, name: str, metadata: dict) -> None:
        token = await self.credential.get_token("https://servicebus.azure.net/.default")
        url = f"https://{self.namespace}/{quote(self.topic, safe='')}/subscriptions/{name}?api-version=2021-05"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.put(url, content=atomic_subscription_xml(metadata), headers={
                "Authorization": f"Bearer {token.token}", "Content-Type": "application/atom+xml",
            })
            # No If-Match header: create-only, matching .NET CreateSubscription(options, rule).
            # Only explicit 409 is an entity-exists race; all other failures remain visible.
            if response.status_code != 409:
                response.raise_for_status()

    async def close(self) -> None:
        await self.admin.close()
