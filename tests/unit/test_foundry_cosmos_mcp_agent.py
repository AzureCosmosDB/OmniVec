"""Foundry consent/resume client regressions without remote requests."""

import importlib
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def client_script(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[2] / "scripts"))
    return importlib.import_module("foundry_cosmos_mcp_agent")


def stub_clients(script, monkeypatch, response):
    calls = {"agents": [], "conversations": [], "responses": []}

    def create_agent(**kwargs):
        calls["agents"].append(kwargs)
        return SimpleNamespace(name=kwargs["agent_name"], version="7")

    def create_conversation():
        calls["conversations"].append(True)
        return SimpleNamespace(id="conversation-1")

    def create_response(**kwargs):
        calls["responses"].append(kwargs)
        return response

    openai = SimpleNamespace(
        conversations=SimpleNamespace(create=create_conversation),
        responses=SimpleNamespace(create=create_response),
    )
    openai.with_options = lambda **_: openai
    project = SimpleNamespace(
        agents=SimpleNamespace(create_version=create_agent),
        get_openai_client=lambda: nullcontext(openai),
    )
    monkeypatch.setattr(script, "AIProjectClient", lambda **_: nullcontext(project))
    monkeypatch.setattr(script, "AzureDeveloperCliCredential", lambda **_: object())
    return calls


def create_arguments():
    return [
        "--project-endpoint", "https://project.example.test",
        "--connection", "per-user-oauth", "--server-url", "https://mcp.example.test/api/mcp",
        "--model", "chat", "--container", "protected", "--fields", "id,content,source_ref",
        "--agent-name", "policy-agent", "--question", "What is the meal allowance?",
    ]


def test_creation_surfaces_oauth_consent_instead_of_an_empty_answer(client_script, monkeypatch, capsys):
    response = SimpleNamespace(
        id="response-consent", status="completed", output_text="",
        output=[SimpleNamespace(type="oauth_consent_request", consent_link="https://consent.example.test/user")],
    )
    calls = stub_clients(client_script, monkeypatch, response)
    client_script.main(create_arguments())
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "requires_consent" and output["answer"] is None
    assert output["consent_links"] == ["https://consent.example.test/user"]
    assert output["response_id"] == "response-consent" and output["agent_version"] == "7"
    assert len(calls["agents"]) == 1 and len(calls["conversations"]) == 1
    assert calls["responses"][0]["extra_body"]["agent_reference"]["version"] == "7"


def test_resume_pins_the_existing_version_without_recreating_resources(client_script, monkeypatch, capsys):
    response = SimpleNamespace(id="response-answer", status="completed", output_text="47 USD.", output=[])
    calls = stub_clients(client_script, monkeypatch, response)
    client_script.main([
        "--project-endpoint", "https://project.example.test",
        "--resume-response-id", "response-consent", "--agent-name", "policy-agent",
        "--agent-version", "7", "--question", "What is the meal allowance?",
    ])
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "completed" and output["answer"] == "47 USD."
    assert calls["agents"] == [] and calls["conversations"] == []
    request = calls["responses"][0]
    assert request["previous_response_id"] == "response-consent" and "conversation" not in request
    assert request["extra_body"]["agent_reference"] == {
        "name": "policy-agent", "version": "7", "type": "agent_reference",
    }


def test_missing_answer_without_consent_is_an_explicit_failure(client_script, monkeypatch, capsys):
    stub_clients(client_script, monkeypatch, SimpleNamespace(
        id="response-empty", status="completed", output_text="", output=[],
    ))
    with pytest.raises(SystemExit, match="did not complete"):
        client_script.main(create_arguments())
    assert json.loads(capsys.readouterr().out)["status"] == "answer_missing"


def test_resume_requires_the_saved_agent_version_before_any_remote_call(client_script, monkeypatch):
    monkeypatch.setattr(client_script, "AIProjectClient", lambda **_: pytest.fail("Unexpected remote request"))
    with pytest.raises(SystemExit):
        client_script.main([
            "--project-endpoint", "https://project.example.test",
            "--resume-response-id", "response-consent", "--agent-name", "policy-agent",
        ])
