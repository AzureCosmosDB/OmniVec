"""Deterministic protocol/SDK/in-memory checks. NOT a live Service Bus isolation proof."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest
from azure.core.credentials import AccessToken
from azure.servicebus.management import CorrelationRuleFilter, RuleProperties, SqlRuleFilter

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "connectors" / "ingestion" / "onelake_iceberg"))
import pipeline_lanes as lanes


def metadata(id_="A"):
    return {
        "routing_version": 1, "pipeline_id": id_, "resource_weight": 10,
        "max_concurrency_per_worker": 1, "resource_priority": "normal", "workload_class": "shared",
    }


def rule(filter_=None):
    return RuleProperties("pipeline", filter=filter_ or CorrelationRuleFilter(label="A"),
                          action=None, created_at_utc=None)


async def items(values):
    for value in values:
        yield value


class Admin:
    def __init__(self):
        self.marker = lanes.TOPIC_MARKER
        self.subscriptions = {}
        self.rules = {}
        self.counts = {}
        self.creates = self.topic_reads = self.rule_reads = 0
        self.error = None
        self.xml = None

    def add(self, data):
        name = lanes.subscription_name(data["pipeline_id"])
        self.subscriptions[name] = SimpleNamespace(
            name=name, user_metadata=json.dumps(data), requires_session=False,
            forward_to=None, forward_dead_lettered_messages_to=None, status="Active",
        )
        self.rules[name] = [rule(CorrelationRuleFilter(label=data["pipeline_id"]))]

    async def get_topic(self, topic):
        self.topic_reads += 1
        return SimpleNamespace(user_metadata=self.marker)

    def list_subscriptions(self, topic):
        return items(list(self.subscriptions.values()))

    async def get_subscription(self, topic, name):
        return self.subscriptions[name]

    def list_rules(self, topic, name):
        self.rule_reads += 1
        return items(self.rules[name])

    async def create_subscription(self, name, data):
        self.creates += 1
        if self.error:
            raise self.error
        self.xml = lanes.atomic_subscription_xml(data)
        self.add(data)

    async def get_subscription_runtime_properties(self, topic, name):
        return SimpleNamespace(active_message_count=self.counts.get(name, 0))


def registry(admin):
    value = lanes.LaneRegistry.__new__(lanes.LaneRegistry)
    value.admin, value.topic, value.threshold = admin, "embeddings-lanes-v1", 5000
    value._gate, value._expires, value._cache = asyncio.Lock(), 0, {}
    value._create_atomic = admin.create_subscription
    return value


def test_exact_cross_language_protocol_and_docker_context_mirrors():
    assert lanes.subscription_name("pipeline-A") == "p-eee5dc2982fc13ded6ee1c3a73e3c5f9"
    assert lanes.subscription_name("π/東京") == "p-f75c96e1b8c42663d11080110f4e485a"
    for value in ("", "embeddings", "EMBEDDINGS"):
        with pytest.raises(ValueError):
            lanes.validate_topic_name(value)
    lanes.validate_metadata(lanes.subscription_name("A"), metadata())
    with pytest.raises(ValueError):
        lanes.validate_metadata(lanes.subscription_name("B"), metadata())
    producer = (ROOT / "connectors" / "ingestion" / "dotnet" / "Lanes" / "PipelineLanes.cs").read_text()
    worker = (ROOT / "connectors" / "worker" / "dotnet" / "Lanes" / "PipelineLanes.cs").read_text()
    assert producer.replace("OmniVec.ChangeFeed.Lanes", "OmniVec.Worker.Lanes") == worker


@pytest.mark.parametrize("rules", [
    [], [rule(SqlRuleFilter("1=1"))], [rule(CorrelationRuleFilter(label="B"))],
    [rule(CorrelationRuleFilter(label="A", session_id="hidden"))],
    [rule(), rule(SqlRuleFilter("1=1"))],
])
def test_invalid_rule_sets_fail_closed(rules):
    with pytest.raises(ValueError):
        lanes.validate_rules("A", rules)


def test_legacy_identity_preserved_and_lanes_set_actual_broker_fields():
    message = {"message_id": "stable", "pipeline_id": "A"}
    legacy = lanes.broker_message(message, False)
    isolated = lanes.broker_message(message, True)
    assert legacy.message_id != "stable" and legacy.subject is None
    assert isolated.message_id == "stable" and isolated.subject == "A"
    assert isolated.content_type == "application/json"


def test_atomic_creation_conflicts_cache_capacity_and_foreign_rules():
    async def check():
        admin = Admin()
        value = registry(admin)
        await asyncio.gather(*(value.ensure_capacity(metadata()) for _ in range(8)))
        assert admin.creates == 1 and admin.topic_reads == 1 and admin.rule_reads == 1
        root = ET.fromstring(admin.xml)
        default = root.find(f".//{{{lanes.SB_XML}}}DefaultRuleDescription")
        assert default.find(f"{{{lanes.SB_XML}}}Filter").get(f"{{{lanes.XSI_XML}}}type") == "CorrelationFilter"
        assert default.find(f".//{{{lanes.SB_XML}}}Label").text == "A"
        admin.counts[lanes.subscription_name("A")] = 5000
        with pytest.raises(RuntimeError):
            await value.ensure_capacity(metadata())
        forbidden = Admin()
        forbidden.error = RuntimeError("denied")
        with pytest.raises(RuntimeError):
            await registry(forbidden).ensure_capacity(metadata())
        foreign = Admin()
        foreign.marker = "foreign"
        with pytest.raises(ValueError):
            await registry(foreign).ensure_capacity(metadata())
        assert foreign.creates == 0
        broad = Admin()
        broad.add(metadata())
        broad.rules[lanes.subscription_name("A")].append(rule(SqlRuleFilter("1=1")))
        with pytest.raises(ValueError):
            await registry(broad).ensure_capacity(metadata())
        with pytest.raises(ValueError):
            await value.ensure_capacity({**metadata(), "resource_weight": 20})
    asyncio.run(check())


def test_helm_lane_config_all_queue_services_and_manual_scaling():
    import shutil
    import subprocess
    import yaml
    helm = shutil.which("helm")
    if not helm:
        pytest.skip("Helm executable unavailable")
    base = [helm, "template", "lanes", str(ROOT / "helm" / "omnivec"),
            "--set", "azure.serviceBus.namespace=local.servicebus.windows.net"]
    def render(*settings):
        args = base + [part for setting in settings for part in ("--set", setting)]
        return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    options = (
        "azure.serviceBus.pipelineLanes.topicName=embeddings-lanes-v1",
        "azure.serviceBus.pipelineLanes.producersEnabled=true",
        "azure.serviceBus.pipelineLanes.workerEnabled=true",
        "dotnetWorker.autoscaling.enabled=false",
        "databricksWatcher.enabled=true", "sharepointWatcher.enabled=true",
        "onelakeIcebergWatcher.enabled=true",
    )
    result = render(*options)
    assert result.returncode == 0, result.stderr
    docs = [d for d in yaml.safe_load_all(result.stdout) if d]
    deployments = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Deployment"}
    for name, prefix in (
        ("omnivec-cosmos-changefeed", "ChangeFeed__"),
        ("omnivec-blob-ingestor", "ChangeFeed__"),
        ("omnivec-databricks-watcher", "ChangeFeed__"),
        ("omnivec-sharepoint-watcher", "ChangeFeed__"),
        ("omnivec-dotnet-worker", "Worker__"),
        ("omnivec-onelake-iceberg-watcher", "ONELAKE_ICEBERG_"),
    ):
        env = {e["name"]: e.get("value") for e in deployments[name]["spec"]["template"]["spec"]["containers"][0]["env"]}
        python = prefix == "ONELAKE_ICEBERG_"
        enabled = prefix + ("PIPELINE_LANES_ENABLED" if python else "PipelineLanesEnabled")
        topic = prefix + ("PIPELINE_LANES_TOPIC" if python else "PipelineLanesTopicName")
        assert env[enabled] == "true" and env[topic] == "embeddings-lanes-v1"
    assert not any(d["kind"] == "ScaledObject" and d["metadata"]["name"] == "omnivec-dotnet-worker" for d in docs)
    for kind in ("hpa", "keda"):
        invalid = render(*options, "dotnetWorker.autoscaling.enabled=true", f"dotnetWorker.autoscaling.type={kind}")
        assert invalid.returncode != 0 and "fixed replicas" in invalid.stderr
    invalid = render("azure.serviceBus.pipelineLanes.producersEnabled=true")
    assert invalid.returncode != 0 and "explicit separate marked topicName" in invalid.stderr
    invalid = render("azure.serviceBus.pipelineLanes.producersEnabled=true", "azure.serviceBus.pipelineLanes.topicName=embeddings")
    assert invalid.returncode != 0 and "never embeddings" in invalid.stderr
    legacy = render()
    assert legacy.returncode == 0
    assert 'value: "false"' in legacy.stdout and "embeddings-lanes-v1" not in legacy.stdout


def test_atomic_rest_put_never_sends_unfiltered_subscription_and_only_accepts_409(monkeypatch):
    import httpx
    requests = []
    status = 201
    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def put(self, url, **kwargs):
            request = httpx.Request("PUT", url, **kwargs)
            requests.append(request)
            return httpx.Response(status, request=request)
    class Credential:
        async def get_token(self, *args, **kwargs):
            return AccessToken("local-fake-token", 4_000_000_000)
    async def check():
        nonlocal status
        value = registry(Admin())
        value.namespace, value.credential = "local.servicebus.windows.net", Credential()
        value._create_atomic = lanes.LaneRegistry._create_atomic.__get__(value)
        await value._create_atomic(lanes.subscription_name("A"), metadata())
        assert len(requests) == 1
        request = requests[0]
        assert request.method == "PUT"
        assert "if-match" not in request.headers
        root = ET.fromstring(request.content)
        assert root.find(f".//{{{lanes.SB_XML}}}DefaultRuleDescription/{{{lanes.SB_XML}}}Filter/{{{lanes.SB_XML}}}Label").text == "A"
        assert int(request.headers["Content-Length"]) == len(request.content)
        status = 409
        await value._create_atomic(lanes.subscription_name("A"), metadata())
        status = 403
        with pytest.raises(httpx.HTTPStatusError):
            await value._create_atomic(lanes.subscription_name("A"), metadata())
    monkeypatch.setattr(lanes.httpx, "AsyncClient", Client)
    asyncio.run(check())


def test_onelake_independent_plans_failure_continuation_legacy_and_cdf(monkeypatch):
    spec = importlib.util.spec_from_file_location("lane_test_onelake_main", ROOT / "connectors" / "ingestion" / "onelake_iceberg" / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    source = {"id": "source", "type": "onelake-iceberg", "enabled": True, "config": {}}
    pipelines = [{
        "id": id_, "status": "active", "destination_id": "dest", "sources": [{"source_id": "source"}],
    } for id_ in ("A", "B")]
    data = {
        "/api/sources": {"sources": [source]},
        "/api/pipelines?include_stats=false": {"pipelines": pipelines},
        "/api/destinations": {"destinations": [{"id": "dest", "enabled": True, "type": "garnet"}]},
    }
    class Api:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get(self, path):
            return SimpleNamespace(json=lambda: data[path])
    monkeypatch.setattr(module.httpx, "AsyncClient", Api)
    async def check():
        watcher = module.OneLakeIcebergWatcher.__new__(module.OneLakeIcebergWatcher)
        watcher.api_url, watcher.lane_mode, watcher._last_scan = "http://local.invalid", True, {}
        admin = Admin()
        watcher.lanes = registry(admin)
        seen = []
        async def scan(source, plans, destinations, scope, identity):
            seen.append((scope, [p["id"] for p in plans]))
            if plans[0]["id"] == "A":
                raise RuntimeError("failed A; no checkpoint")
        watcher.scan_source = scan
        await watcher.scan_all()
        assert seen == [("source--A--lanes-v1", ["A"]), ("source--B--lanes-v1", ["B"])]
        assert "source--A--lanes-v1" not in watcher._last_scan
        assert "source--B--lanes-v1" in watcher._last_scan
        watcher.lane_mode, watcher._last_scan = False, {}
        seen.clear()
        async def legacy_scan(source, plans, destinations, scope, identity):
            seen.append((scope, [p["id"] for p in plans]))
        watcher.scan_source = legacy_scan
        await watcher.scan_all()
        assert seen == [("source", ["A", "B"])]
        watcher.lane_mode = True
        source["config"]["change_data_feed"] = {"enabled": True}
        with pytest.raises(ValueError, match="CDF"):
            await watcher.scan_all()
    asyncio.run(check())
