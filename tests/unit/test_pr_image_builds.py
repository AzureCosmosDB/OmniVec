"""Required image checks must run on PRs without publishing or registry secrets."""
from pathlib import Path

import yaml


def test_required_pr_image_builds_are_reported_without_publishing():
    root = Path(__file__).resolve().parents[2]
    text = (root / ".github" / "workflows" / "pull-request-images.yml").read_text()
    workflow = yaml.load(text, Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"pull_request"}
    assert workflow["on"]["pull_request"] == {"branches": ["main", "dev"]}
    assert workflow["permissions"] == {"contents": "read"}
    assert "secrets." not in text
    job = workflow["jobs"]["build"]
    assert "if" not in job
    assert "name" not in job
    contexts = {
        f"build ({entry['image']}, {entry['context']}, {entry['dockerfile']})"
        for entry in job["strategy"]["matrix"]["include"]
    }
    assert contexts == {
        "build (omnivec-api, ., Dockerfile)",
        "build (omnivec-web, web, web/Dockerfile)",
        "build (omnivec-changefeed, ., connectors/ingestion/dotnet/Dockerfile)",
        "build (docgrok-mock-embedding, docgrok/services/embedding/mock, docgrok/services/embedding/mock/Dockerfile)",
        "build (omnivec-mock-source, connectors/mock, connectors/mock/Source/Dockerfile)",
        "build (omnivec-mock-destination, connectors/mock, connectors/mock/Destination/Dockerfile)",
    }
    steps = job["steps"]
    build = next(s for s in steps if s.get("uses", "").startswith("docker/build-push-action@"))
    assert "if" not in build
    assert build["with"]["push"] == "false"
    assert build["with"]["context"] == "${{ matrix.context }}"
    assert build["with"]["file"] == "${{ matrix.dockerfile }}"
    assert not any(s.get("uses", "").startswith("docker/login-action@") for s in steps)


def test_root_ingestion_build_context_keeps_both_dotnet_projects():
    root = Path(__file__).resolve().parents[2]
    ignore = (root / ".dockerignore").read_text().splitlines()
    assert "connectors/" not in ignore
    for path in ("connectors/ingestion/", "connectors/ingestion/dotnet/",
                 "connectors/worker/", "connectors/worker/dotnet/"):
        assert f"!{path}" in ignore
    for project in ("ingestion", "worker"):
        for directory in ("bin", "obj"):
            assert f"connectors/{project}/dotnet/{directory}/" in ignore
