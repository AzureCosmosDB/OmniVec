"""Execute both installers' agent settings validation without Azure access."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
PREFIX = "OMNIVEC_AGENT_"


@pytest.fixture(params=["ps1", "sh"])
def validator(request):
    platform = request.param
    executable = shutil.which("pwsh" if platform == "ps1" else "bash")
    if platform == "sh" and os.name == "nt":
        candidate = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe"
        executable = str(candidate) if candidate.is_file() else None
    if not executable:
        pytest.skip(f"{platform} interpreter unavailable")
    source = (ROOT / "hooks" / f"postprovision.{platform}").read_text(encoding="utf-8")
    marker = "$AGENT_DEFAULT_MODEL_ID = " if platform == "ps1" else "AGENT_DEFAULT_MODEL_ID=$("
    start = source.index(marker)
    block = source[start:source.index("# Azure rejects PublicIP", start)]
    if platform == "ps1":
        script = (
            "$ErrorActionPreference='Stop'\n"
            "function Get-AzdValue($name) { [Environment]::GetEnvironmentVariable($name) }\n"
            + block
            + "\n@{model=[string]$AGENT_DEFAULT_MODEL_ID;tag=$AGENT_IMAGE_TAG;"
            "remediation=$AGENT_ALLOW_K8S_REMEDIATION} | ConvertTo-Json -Compress\n"
        )
        command = [executable, "-NoProfile", "-NonInteractive", "-Command", script]
    else:
        script = (
            'set -eu\nget_azd_value() { printf "%s" "${!1-}"; }\n'
            + block
            + '\nprintf \'{"model":"%s","tag":"%s","remediation":"%s"}\\n\' '
            '"$AGENT_DEFAULT_MODEL_ID" "$AGENT_IMAGE_TAG" "$AGENT_ALLOW_K8S_REMEDIATION"\n'
        )
        command = [executable, "-c", script]

    def run(settings):
        env = {k: v for k, v in os.environ.items() if not k.startswith(PREFIX)}
        env.update({PREFIX + k: v for k, v in settings.items()})
        return subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)

    return run


@pytest.mark.parametrize("settings,expected", [
    ({}, {"model": "", "tag": "latest", "remediation": "false"}),
    ({"DEFAULT_MODEL_ID": "mdl-chat-123", "IMAGE_TAG": "recovery-v1", "ALLOW_K8S_REMEDIATION": "true"},
     {"model": "mdl-chat-123", "tag": "recovery-v1", "remediation": "true"}),
    ({"DEFAULT_MODEL_ID": "a" * 160, "IMAGE_TAG": "_" * 128},
     {"model": "a" * 160, "tag": "_" * 128, "remediation": "false"}),
])
def test_valid_agent_configuration(validator, settings, expected):
    result = validator(settings)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == expected


@pytest.mark.parametrize("setting,value", [
    ("DEFAULT_MODEL_ID", "a" * 161),
    ("DEFAULT_MODEL_ID", "_model"),
    ("DEFAULT_MODEL_ID", "mdl-chat\ninjected: true"),
    ("DEFAULT_MODEL_ID", "mdl-chat\r"),
    ("DEFAULT_MODEL_ID", "mdl-chat,agent.enabled=false"),
    ("DEFAULT_MODEL_ID", "mdl-\u00e9"),
    ("IMAGE_TAG", "a" * 129),
    ("IMAGE_TAG", "-tag"),
    ("IMAGE_TAG", "tag\nother"),
    ("IMAGE_TAG", "tag:invalid"),
    ("IMAGE_TAG", 'tag"'),
    ("ALLOW_K8S_REMEDIATION", "False"),
    ("ALLOW_K8S_REMEDIATION", "0"),
    ("ALLOW_K8S_REMEDIATION", "true,agent.enabled=false"),
])
def test_invalid_agent_configuration_fails_closed(validator, setting, value):
    result = validator({setting: value})
    assert result.returncode != 0
    assert "must" in result.stderr.lower()


@pytest.mark.parametrize("setting,key", [("DEFAULT_MODEL_ID", "model"), ("IMAGE_TAG", "tag")])
def test_trailing_linefeed_cannot_reach_helm(validator, setting, key):
    result = validator({setting: "valid\n"})
    # Bash command substitution strips trailing linefeeds; PowerShell rejects them.
    if result.returncode == 0:
        assert json.loads(result.stdout)[key] == "valid"
    else:
        assert "must" in result.stderr.lower()
