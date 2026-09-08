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
        "build (omnivec-changefeed, connectors/ingestion/dotnet, connectors/ingestion/dotnet/Dockerfile)",
    }
    steps = job["steps"]
    build = next(s for s in steps if s.get("uses", "").startswith("docker/build-push-action@"))
    assert "if" not in build
    assert build["with"]["push"] == "false"
    assert build["with"]["context"] == "${{ matrix.context }}"
    assert build["with"]["file"] == "${{ matrix.dockerfile }}"
    assert not any(s.get("uses", "").startswith("docker/login-action@") for s in steps)
