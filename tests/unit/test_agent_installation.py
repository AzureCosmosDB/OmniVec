"""Offline guardrails for agent workload-identity installation wiring."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_agent_has_exact_federation_on_existing_installation_identity():
    source = ROOT.joinpath("infra", "modules", "federation.bicep").read_text(encoding="utf-8")
    agent = source.split("resource fedCredAgent ", 1)[1]
    assert "parent: identity" in agent
    assert "name: 'omnivec-agent-federation'" in agent
    assert "subject: 'system:serviceaccount:omnivec:omnivec-agent'" in agent
    assert "issuer: oidcIssuerUrl" in agent
    assert "audiences: ['api://AzureADTokenExchange']" in agent
    assert "dependsOn: [fedCredDocgrok]" in agent
    assert source.count("subject: 'system:serviceaccount:omnivec:") == 3
    assert "roleAssignments" not in source


def test_normal_installer_passes_its_own_aks_issuer_to_federation():
    installer = ROOT.joinpath("azure.yaml").read_text(encoding="utf-8")
    main = ROOT.joinpath("infra", "main.bicep").read_text(encoding="utf-8")
    assert "provider: bicep" in installer
    assert "module: main" in installer
    federation = main.split("module federation ", 1)[1].split("// OUTPUTS", 1)[0]
    assert "'modules/federation.bicep'" in federation
    assert "scope: rg" in federation
    assert "identityName: identityName" in federation
    assert "oidcIssuerUrl: aks.outputs.oidcIssuerUrl" in federation
