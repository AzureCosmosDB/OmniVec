"""Tests for ``scripts/gen_threat_model_tm7.py``.

We treat each ``DIAG_*`` dict as the contract for one .tm7 file. The tests
assert:

* Exact element counts, trust-boundary (TB) counts, flow counts.
* Element names + kinds (e.g. "Azure AD" is process+oos=True, "Callers" is
  external).
* Flow tuples — any change in source/target index will fail the test.
* End-to-end generation: invoke ``build_one`` against a template, parse the
  XML, count ``GE.P`` / ``GE.EI`` / ``GE.DS`` / ``GE.TB.B`` / ``GE.DF``
  occurrences, assert they match the dict.

For the end-to-end step we use one of the already-rendered .tm7 files in
``docs/security/`` as the template so the test doesn't depend on the
external CmasBoA reference template.
"""
from __future__ import annotations

import collections
import pathlib
import xml.etree.ElementTree as ET

import pytest


ABS_NS = "http://schemas.datacontract.org/2004/07/ThreatModeling.Model.Abstracts"
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCS_SECURITY = REPO_ROOT / "docs" / "security"


# ---------------------------------------------------------------------------
# Dict-level contract assertions.
# ---------------------------------------------------------------------------
class TestDiagramOverall:
    def test_element_count(self, tm7_generator):
        # 2026-05-21 review (final): OmniVec AKS = 3 components (Web/API, DocGrok,
        # Ingestion connector). TB-3 holds 3 SEPARATE resource shapes:
        # Cosmos metadata, Key Vault, App Insights. Plus Callers, AAD, Foundry,
        # Customer data plane, Local config = 11 total.
        assert len(tm7_generator.DIAG_OVERALL["elements"]) == 11

    def test_tb_count(self, tm7_generator):
        # Local config moved INSIDE TB-2 (it lives in the pod), so its
        # standalone trust boundary was removed: 7 -> 6.
        assert len(tm7_generator.DIAG_OVERALL["tbs"]) == 6

    def test_flow_count(self, tm7_generator):
        # 6 cross-trust + 9 inside-TB-3 (3 OmniVec components × 3 resources) = 15.
        # Local-config (envcfg) has NO outgoing arrows — env-var / Helm-value
        # reads are pod-internal state, not a DFD flow.
        assert len(tm7_generator.DIAG_OVERALL["flows"]) == 18

    def test_tb3_has_three_distinct_resource_shapes(self, tm7_generator):
        # Per 2026-05-21 reviewer refinement: Cosmos metadata, Key Vault, and
        # App Insights must each be their own shape inside TB-3 — NOT merged.
        elems = tm7_generator.DIAG_OVERALL["elements"]
        assert "CosmosDB metadata account" in elems[5]["name"]
        assert "Key Vault" in elems[9]["name"]
        assert "Application Insights" in elems[10]["name"]
        # And they must all be "store" kind (data stores).
        assert elems[5]["k"] == "store"
        assert elems[9]["k"] == "store"
        assert elems[10]["k"] == "store"

    def test_callers_is_external(self, tm7_generator):
        callers = tm7_generator.DIAG_OVERALL["elements"][0]
        assert callers["k"] == "external"
        assert callers["name"].startswith("Callers")
        assert not callers.get("oos", False)

    def test_azure_ad_is_oos_process(self, tm7_generator):
        aad = tm7_generator.DIAG_OVERALL["elements"][1]
        assert aad["k"] == "process"
        assert aad["name"].startswith("Azure AD")
        assert aad.get("oos") is True

    def test_foundry_is_oos_process(self, tm7_generator):
        foundry = tm7_generator.DIAG_OVERALL["elements"][3]
        assert foundry["k"] == "process"
        assert "Foundry" in foundry["name"]
        assert foundry.get("oos") is True

    def test_omnivec_main_is_in_scope_process(self, tm7_generator):
        # After the 2026-05-21 split, the Web/API control plane is at index 2.
        omni = tm7_generator.DIAG_OVERALL["elements"][2]
        assert omni["k"] == "process"
        assert "Web / API control plane" in omni["name"]
        assert not omni.get("oos", False)

    def test_three_aks_components(self, tm7_generator):
        # AKS box has exactly: Web/API control plane (2), DocGrok (6),
        # Ingestion source/destination connector (7).
        names = [tm7_generator.DIAG_OVERALL["elements"][i]["name"] for i in (2, 6, 7)]
        assert "Web / API control plane" in names[0]
        assert "DocGrok" in names[1]
        assert "Ingestion source/destination connector" in names[2]

    def test_flow_tuples_exact(self, tm7_generator):
        # Locks the post-2026-05-21 review topology (final):
        #   * No callers->AAD flow
        #   * Three OmniVec components: 2=Web/API, 6=DocGrok, 7=Ingestion connector
        #   * TB-3 holds THREE SEPARATE resource shapes:
        #       5=Cosmos metadata, 9=Key Vault, 10=App Insights
        #     and each OmniVec component has its own arrow to each TB-3 resource
        #   * Unified read+write to customer plane (one flow)
        #   * Local config (8) reads into each OmniVec component at start
        actual = [(s, d) for s, d, _ in tm7_generator.DIAG_OVERALL["flows"]]
        assert actual == [
            (0, 2),    # Callers -> Web/API
            (2, 1),    # Web/API -> AAD (JWKS)
            (2, 6),    # Web/API -> DocGrok (search/RAG)
            (7, 6),    # Connector -> DocGrok (ingest)
            (6, 3),    # DocGrok -> Foundry
            (7, 4),    # Connector -> Customer data plane (unified)
            (2, 5),    # Web/API -> Cosmos metadata
            (2, 9),    # Web/API -> Key Vault
            (2, 10),   # Web/API -> App Insights
            (6, 5),    # DocGrok -> Cosmos metadata
            (6, 9),    # DocGrok -> Key Vault
            (6, 10),   # DocGrok -> App Insights
            (7, 5),    # Connector -> Cosmos metadata
            (7, 9),    # Connector -> Key Vault
            (7, 10),   # Connector -> App Insights
            (2, 8),    # Web/API -> envcfg (initiator reads pod-internal config @ startup)
            (6, 8),    # DocGrok -> envcfg
            (7, 8),    # Connector -> envcfg
        ]


class TestDiagramControl:
    def test_counts(self, tm7_generator):
        # 2026-05-21 review: added Local configuration node (envcfg) and its
        # two read flows; removed the callers->AAD OIDC sign-in flow.
        d = tm7_generator.DIAG_CONTROL
        assert len(d["elements"]) == 8
        assert len(d["tbs"]) == 5
        assert len(d["flows"]) == 8

    def test_aad_oos(self, tm7_generator):
        aad = tm7_generator.DIAG_CONTROL["elements"][1]
        assert aad["k"] == "process" and aad.get("oos") is True

    def test_callers_external(self, tm7_generator):
        c = tm7_generator.DIAG_CONTROL["elements"][0]
        assert c["k"] == "external"

    def test_no_callers_to_aad_flow(self, tm7_generator):
        # 2026-05-21 review: External Interactors are black boxes, so we do
        # NOT model the callers->AAD OIDC sign-in handshake.
        pairs = [(s, d) for s, d, _ in tm7_generator.DIAG_CONTROL["flows"]]
        assert (0, 1) not in pairs

    def test_flow_tuples_exact(self, tm7_generator):
        actual = [(s, d) for s, d, _ in tm7_generator.DIAG_CONTROL["flows"]]
        assert actual == [
            (0, 2),    # Callers -> Web (UI assets)
            (0, 3),    # Callers -> API (admin CRUD / token mint)
            (3, 1),    # API -> AAD (JWKS)
            (3, 4),    # API -> metadata
            (3, 5),    # API -> Key Vault
            (3, 6),    # API -> Agent
            (7, 3),    # Local config -> API
            (7, 2),    # Local config -> Web
        ]


class TestDiagramSearch:
    def test_counts(self, tm7_generator):
        d = tm7_generator.DIAG_SEARCH
        assert len(d["elements"]) == 8
        assert len(d["tbs"]) == 5
        assert len(d["flows"]) == 8

    def test_aad_and_foundry_oos(self, tm7_generator):
        els = tm7_generator.DIAG_SEARCH["elements"]
        assert els[1].get("oos") is True  # aad
        assert els[5].get("oos") is True  # foundry

    def test_flow_tuples_exact(self, tm7_generator):
        actual = [(s, d) for s, d, _ in tm7_generator.DIAG_SEARCH["flows"]]
        assert actual == [(0, 2), (0, 3), (2, 1), (2, 3), (3, 6), (3, 4), (4, 5), (3, 7)]


class TestDiagramIngest:
    def test_counts(self, tm7_generator):
        d = tm7_generator.DIAG_INGEST
        assert len(d["elements"]) == 8
        assert len(d["tbs"]) == 5
        assert len(d["flows"]) == 11

    def test_foundry_oos(self, tm7_generator):
        f = tm7_generator.DIAG_INGEST["elements"][6]
        assert f["k"] == "process" and f.get("oos") is True

    def test_flow_tuples_exact(self, tm7_generator):
        actual = [(s, d) for s, d, _ in tm7_generator.DIAG_INGEST["flows"]]
        assert actual == [
            (2, 0), (2, 0), (2, 1), (2, 3), (4, 3),
            (4, 5), (5, 6), (4, 7), (2, 5), (2, 0), (2, 7),
        ]


class TestDiagramsRegistry:
    def test_all_four_registered(self, tm7_generator):
        assert len(tm7_generator.DIAGRAMS) == 4

    def test_unique_output_filenames(self, tm7_generator):
        outs = [d["out"] for d in tm7_generator.DIAGRAMS]
        assert len(outs) == len(set(outs))

    @pytest.mark.parametrize(
        "out",
        ["threat-model.tm7", "threat-model-control.tm7",
         "threat-model-search.tm7", "threat-model-ingestion.tm7"],
    )
    def test_known_output_paths(self, tm7_generator, out):
        assert any(d["out"] == out for d in tm7_generator.DIAGRAMS)


# ---------------------------------------------------------------------------
# End-to-end build_one() → parse XML → count generic types.
# ---------------------------------------------------------------------------
KIND_TO_GTYPE = {
    "process": "GE.P",
    "external": "GE.EI",
    "store": "GE.DS",
}


@pytest.mark.parametrize("diagram_name", ["DIAG_OVERALL", "DIAG_CONTROL", "DIAG_SEARCH", "DIAG_INGEST"])
def test_build_one_produces_expected_counts(tm7_generator, tmp_path, diagram_name):
    # Use an already-rendered diagram in docs/security/ as a template.
    template_path = DOCS_SECURITY / "threat-model.tm7"
    if not template_path.exists():
        pytest.skip("template .tm7 not present in docs/security/")
    template_text = template_path.read_text(encoding="utf-8")
    diagram = getattr(tm7_generator, diagram_name)

    rendered = tm7_generator.build_one(template_text, diagram)
    out_path = tmp_path / diagram["out"]
    out_path.write_text(rendered, encoding="utf-8")

    root = ET.fromstring(rendered)
    gids = [g.text for g in root.findall(f".//{{{ABS_NS}}}GenericTypeId")]
    counts = collections.Counter(gids)

    # Expected counts derived from the diagram dict.
    expected_kinds = collections.Counter(el["k"] for el in diagram["elements"])
    for kind, gtype in KIND_TO_GTYPE.items():
        assert counts[gtype] == expected_kinds.get(kind, 0), (
            f"{diagram_name}: expected {expected_kinds.get(kind, 0)} of {gtype}, got {counts[gtype]}")

    assert counts["GE.TB.B"] == len(diagram["tbs"])
    assert counts["GE.DF"] == len(diagram["flows"])
