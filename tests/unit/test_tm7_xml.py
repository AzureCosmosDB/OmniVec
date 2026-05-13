"""Structural correctness invariants for every ``.tm7`` file in
``docs/security/``.

These are not snapshots — they are *forever-true rules* about threat-model
shape that the team has committed to:

  * External Interactors (``GE.EI``) cannot be marked Out Of Scope.
    Rationale (Simone): an actor outside the trust boundary that we
    nevertheless interact with is *by definition* in scope of analysis;
    marking it OOS hides STRIDE threats on the wire.
  * A flow (``GE.DF``) cannot connect two ``GE.EI`` elements — at least one
    endpoint must be a process or store that *we* own.

Any future .tm7 change that violates these rules will fail here.
"""
from __future__ import annotations

import pathlib
import xml.etree.ElementTree as ET

import pytest


ABS_NS = "http://schemas.datacontract.org/2004/07/ThreatModeling.Model.Abstracts"
KB_NS = "http://schemas.datacontract.org/2004/07/ThreatModeling.KnowledgeBase"
XSD = "http://www.w3.org/2001/XMLSchema"
XSI = "http://www.w3.org/2001/XMLSchema-instance"

# Out Of Scope BooleanDisplayAttribute name guid (template-stable).
NAME_OUT_OF_SCOPE = "71f3d9aa-b8ef-4e54-8126-607a1d903103"

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCS_SECURITY = REPO_ROOT / "docs" / "security"


def _tm7_files() -> list[pathlib.Path]:
    if not DOCS_SECURITY.is_dir():
        return []
    return sorted(DOCS_SECURITY.glob("*.tm7"))


# Module-level skip helper — collected at import time so each file becomes
# its own test instance via parametrize.
TM7_FILES = _tm7_files()


def _iter_elements_with_oos(root: ET.Element):
    """Yield (gtype, oos_bool) per addressable element in a TMT model."""
    # Elements live in <a:KeyValueOfguidanyType><a:Value ...>
    # We just look at every <a:Value> that has a <GenericTypeId> child.
    for value in root.iter():
        if not value.tag.endswith("}Value"):
            continue
        gtid = value.find(f".//{{{ABS_NS}}}GenericTypeId")
        if gtid is None or not gtid.text:
            continue
        gtype = gtid.text
        # Find OOS boolean by Name guid.
        oos = False
        for ba in value.iter(f"{{{KB_NS}}}DisplayAttribute"):
            pass  # not actually used; left for clarity
        # The b:Name element under any BooleanDisplayAttribute with our guid.
        for nm in value.iter(f"{{{KB_NS}}}Name"):
            if nm.text == NAME_OUT_OF_SCOPE:
                # walk to the sibling b:Value
                parent_attr = None
                # Find enclosing anyType containing this Name.
                for any_t in value.iter():
                    if any_t.tag.endswith("}Name") and any_t is nm:
                        continue
                # Simpler: any sibling <b:Value> under the same parent of <b:Name>.
                # We re-traverse by inspecting all anyType siblings.
                pass
        # The clean way: walk every <b:Name>71f...</b:Name> and grab its sibling
        # <b:Value> within the same <a:anyType>.
        for any_t in value.iter():
            if not any_t.tag.endswith("}anyType"):
                continue
            nm = any_t.find(f"{{{KB_NS}}}Name")
            if nm is None or nm.text != NAME_OUT_OF_SCOPE:
                continue
            val = any_t.find(f"{{{KB_NS}}}Value")
            if val is not None and (val.text or "").lower() == "true":
                oos = True
                break
        yield gtype, oos, value


def _iter_flow_endpoints(root: ET.Element):
    """For each GE.DF flow, yield (source_guid, target_guid)."""
    for value in root.iter():
        if not value.tag.endswith("}Value"):
            continue
        gtid = value.find(f".//{{{ABS_NS}}}GenericTypeId")
        if gtid is None or gtid.text != "GE.DF":
            continue
        src = value.find(f"{{{ABS_NS}}}SourceGuid")
        dst = value.find(f"{{{ABS_NS}}}TargetGuid")
        if src is not None and dst is not None:
            yield src.text, dst.text


def _index_elements_by_guid(root: ET.Element) -> dict[str, str]:
    """Return {Guid: GenericTypeId} for every addressable element."""
    out = {}
    for value in root.iter():
        if not value.tag.endswith("}Value"):
            continue
        gtid = value.find(f".//{{{ABS_NS}}}GenericTypeId")
        guid = value.find(f"{{{ABS_NS}}}Guid")
        if gtid is None or guid is None or not gtid.text or not guid.text:
            continue
        out[guid.text] = gtid.text
    return out


# ---------------------------------------------------------------------------
# Parametrized tests — one instance per .tm7 file.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not TM7_FILES, reason="No .tm7 files present")
@pytest.mark.parametrize("tm7_path", TM7_FILES, ids=lambda p: p.name)
class TestTm7Invariants:

    def test_parses(self, tm7_path):
        ET.parse(tm7_path)

    def test_has_at_least_one_process(self, tm7_path):
        root = ET.parse(tm7_path).getroot()
        gtypes = [g for g, _, _ in _iter_elements_with_oos(root)]
        assert "GE.P" in gtypes, f"{tm7_path.name}: no GE.P process found"

    def test_no_external_interactor_is_out_of_scope(self, tm7_path):
        """Simone's rule: GE.EI can never be OOS."""
        root = ET.parse(tm7_path).getroot()
        offenders = []
        for gtype, oos, value in _iter_elements_with_oos(root):
            if gtype == "GE.EI" and oos:
                guid = value.find(f"{{{ABS_NS}}}Guid")
                offenders.append(guid.text if guid is not None else "<no-guid>")
        assert not offenders, (
            f"{tm7_path.name}: external interactor(s) marked Out Of Scope: {offenders}"
        )

    def test_no_flow_between_two_external_interactors(self, tm7_path):
        """Simone's rule: flows must touch at least one process or store."""
        root = ET.parse(tm7_path).getroot()
        index = _index_elements_by_guid(root)
        offenders = []
        for src, dst in _iter_flow_endpoints(root):
            src_type = index.get(src)
            dst_type = index.get(dst)
            if src_type == "GE.EI" and dst_type == "GE.EI":
                offenders.append((src, dst))
        assert not offenders, (
            f"{tm7_path.name}: flow(s) between two external interactors: {offenders}"
        )

    def test_flow_endpoints_reference_known_elements(self, tm7_path):
        """Every flow's source/target Guid must resolve to a real element."""
        root = ET.parse(tm7_path).getroot()
        index = _index_elements_by_guid(root)
        dangling = []
        for src, dst in _iter_flow_endpoints(root):
            if src not in index or dst not in index:
                dangling.append((src, dst))
        assert not dangling, (
            f"{tm7_path.name}: flow with dangling endpoint(s): {dangling}"
        )
