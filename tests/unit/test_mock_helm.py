"""Rendered mock workloads must be selectable and reachable under network policy."""
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


def test_mock_helm_selectors_and_network_policy_match_pods():
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
    documents = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
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
