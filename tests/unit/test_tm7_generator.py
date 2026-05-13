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
        assert len(tm7_generator.DIAG_OVERALL["elements"]) == 9

    def test_tb_count(self, tm7_generator):
        assert len(tm7_generator.DIAG_OVERALL["tbs"]) == 6

    def test_flow_count(self, tm7_generator):
        assert len(tm7_generator.DIAG_OVERALL["flows"]) == 10

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
        omni = tm7_generator.DIAG_OVERALL["elements"][2]
        assert omni["k"] == "process"
        assert "OmniVec" in omni["name"]
        assert not omni.get("oos", False)

    def test_flow_tuples_exact(self, tm7_generator):
        # Locking the (src, dst) pairs catches direction regressions.
        actual = [(s, d) for s, d, _ in tm7_generator.DIAG_OVERALL["flows"]]
        assert actual == [(0, 2), (0, 1), (2, 1), (2, 3), (2, 4), (2, 4), (2, 5), (2, 6), (2, 7), (7, 8)]


class TestDiagramControl:
    def test_counts(self, tm7_generator):
        d = tm7_generator.DIAG_CONTROL
        assert len(d["elements"]) == 7
        assert len(d["tbs"]) == 4
        assert len(d["flows"]) == 7

    def test_aad_oos(self, tm7_generator):
        aad = tm7_generator.DIAG_CONTROL["elements"][1]
        assert aad["k"] == "process" and aad.get("oos") is True

    def test_callers_external(self, tm7_generator):
        c = tm7_generator.DIAG_CONTROL["elements"][0]
        assert c["k"] == "external"

    def test_flow_tuples_exact(self, tm7_generator):
        actual = [(s, d) for s, d, _ in tm7_generator.DIAG_CONTROL["flows"]]
        assert actual == [(0, 2), (0, 1), (0, 3), (3, 1), (3, 4), (3, 5), (3, 6)]


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
