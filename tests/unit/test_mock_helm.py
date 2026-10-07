"""Rendered mock workloads must be selectable and reachable under network policy."""
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


@pytest.fixture
def rendered_documents():
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("Helm is required for rendered-manifest validation")
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [helm, "template", "release-check", str(root / "helm" / "omnivec"),
         "--namespace", "different-release-namespace",
         "--set", "api.mockBenchmark.enabled=true",
         "--set", "api.mockBenchmark.shardCount=3",
         "--set", "networkPolicy.enabled=true",
         "--set", "api.adminToken=local-test-only"],
        check=True, capture_output=True, text=True,
    )
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def test_mock_helm_selectors_and_network_policy_match_pods(rendered_documents):
    documents = rendered_documents
    deployments = [
        doc for doc in documents if doc["kind"] == "Deployment"
        and doc["metadata"]["name"].startswith("omnivec-mock-")
    ]
    assert len(deployments) == 7
    for deployment in deployments:
        labels = deployment["spec"]["template"]["metadata"]["labels"]
        assert deployment["spec"]["selector"]["matchLabels"].items() <= labels.items()
        if "runner" in deployment["metadata"]["name"]:
            assert labels["omnivec-mock-component"] == "source"
        elif "sink" in deployment["metadata"]["name"]:
            assert labels["omnivec-mock-component"] == "destination"
    mock_policies = [
        doc for doc in documents if doc["kind"] == "NetworkPolicy"
        and doc["metadata"]["name"].startswith("omnivec-mock-")
    ]
    assert len(mock_policies) == 4
    assert {doc["metadata"]["namespace"] for doc in mock_policies} == {
        deployment["metadata"]["namespace"] for deployment in deployments
    }


def test_core_network_policies_match_workload_namespace_ports_and_router(rendered_documents):
    policies = {
        doc["metadata"]["name"]: doc for doc in rendered_documents
        if doc["kind"] == "NetworkPolicy"
    }
    services = {
        doc["metadata"]["name"]: doc for doc in rendered_documents
        if doc["kind"] == "Service"
    }
    namespace = services["omnivec-api"]["metadata"]["namespace"]
    assert all(policy["metadata"]["namespace"] == namespace for policy in policies.values())
    for app in ("omnivec-api", "omnivec-web", "omnivec-search"):
        allowed_ports = {
            port["port"]
            for rule in policies[f"{app}-allow"]["spec"]["ingress"]
            for port in rule["ports"]
        }
        assert {
            port["targetPort"] for port in services[app]["spec"]["ports"]
        } <= allowed_ports
    router = next(
        doc for doc in rendered_documents
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "docgrok"
    )
    router_app = router["spec"]["template"]["metadata"]["labels"]["app"]
    selector = policies["docgrok-router-allow"]["spec"]["podSelector"]["matchExpressions"][0]
    assert selector["key"] == "app" and selector["operator"] == "In"
    assert router_app in selector["values"]
    upstream = policies["docgrok-embedders-allow"]["spec"]["ingress"][0]["from"][0]
    assert router_app in upstream["podSelector"]["matchExpressions"][0]["values"]
