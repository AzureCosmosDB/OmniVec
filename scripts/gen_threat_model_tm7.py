#!/usr/bin/env python3
"""Generate ``docs/security/threat-model.tm7`` (Microsoft Threat Modeling Tool).

Approach: surgically replace the inner ``<Borders>`` and ``<Lines>`` payload
inside a known-good template tm7. The template's ``<KnowledgeBase>`` block is
preserved byte-for-byte so TMT can resolve generic stencils
(``GE.P`` / ``GE.DS`` / ``GE.EI`` / ``GE.DF`` / ``GE.TB.B``) when rendering —
a synthesized empty KB triggers ``ArgumentNullException`` in TMT 2016+.

Set ``OMNIVEC_TM7_TEMPLATE`` to point at any working .tm7 you have on disk
(default path is the Cmas BoA reference template under ``Downloads\\``).

Source of truth for the architecture: ``docs/security/threat-model.md``.
"""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from xml.sax.saxutils import escape

# --- Namespaces -------------------------------------------------------------
ABS = "http://schemas.datacontract.org/2004/07/ThreatModeling.Model.Abstracts"
KB_NS = "http://schemas.datacontract.org/2004/07/ThreatModeling.KnowledgeBase"
XSD = "http://www.w3.org/2001/XMLSchema"

# Standard TMT property GUIDs (template-stable; do not change).
NAME_OUT_OF_SCOPE = "71f3d9aa-b8ef-4e54-8126-607a1d903103"
NAME_REASON_OOS = "752473b6-52d4-4776-9a24-202153f7d579"
NAME_DATAFLOW_ORDER = "15ccd509-98eb-49ad-b9c2-b4a2926d1780"

W, H = 100, 100  # default stencil size

# --- OmniVec architecture (mirrors threat-model.md §1 mermaid) --------------
# Indices used by FLOWS:
#  0 user            8 aoai
#  1 web             9 cmeta
#  2 api            10 cvec
#  3 search         11 blob
#  4 router         12 kv
#  5 pworker        13 sb
#  6 ingestor       14 csrc
#  7 incluster      15 bsrc
#                   16 dotnet-worker (queue consumer)
#                   17 aad (login.microsoftonline.com)
#                   18 appinsights (Azure Monitor)
ELEMENTS: list[dict] = [
    {"k": "external", "name": "End-user browser",                 "x": 60,   "y": 540},
    {"k": "process",  "name": "omnivec-web (Next.js)",            "x": 360,  "y": 320},
    {"k": "process",  "name": "omnivec-api (FastAPI)",            "x": 720,  "y": 320},
    {"k": "process",  "name": "omnivec-search (Go)",              "x": 1080, "y": 320},
    {"k": "process",  "name": "docgrok-router (Rust)",            "x": 360,  "y": 720},
    {"k": "process",  "name": "docgrok-pipeline-worker",          "x": 720,  "y": 720},
    {"k": "process",  "name": "omnivec-ingestor (.NET)",          "x": 360,  "y": 1120},
    {"k": "process",  "name": "in-cluster embedders",             "x": 1080, "y": 720},
    {"k": "process",  "name": "Azure OpenAI",                     "x": 1440, "y": 320},
    {"k": "store",    "name": "CosmosDB omnivec.metadata",        "x": 1440, "y": 620},
    {"k": "store",    "name": "Customer CosmosDB (vectors destination)", "x": 1440, "y": 1420},
    {"k": "store",    "name": "Customer Blob (attachments source)", "x": 1800, "y": 1420},
    {"k": "store",    "name": "Azure Key Vault",                  "x": 1800, "y": 320},
    {"k": "store",    "name": "Azure Service Bus",                "x": 1800, "y": 620},
    {"k": "store",    "name": "Customer CosmosDB (source)",       "x": 1440, "y": 1120},
    {"k": "store",    "name": "Customer Blob source",             "x": 1800, "y": 1120},
    {"k": "process",  "name": "omnivec-dotnet-worker (queue)",    "x": 720,  "y": 1120},
    {"k": "external", "name": "Azure AD (login.microsoftonline.com)", "x": 60, "y": 240},
    {"k": "store",    "name": "Azure Monitor / App Insights",     "x": 2160, "y": 320},
]

TBS: list[dict] = [
    {"name": "TB-1 Internet / AAD",         "x": 30,   "y": 200, "w": 260,  "h": 460},
    {"name": "TB-2 AKS cluster",            "x": 320,  "y": 280, "w": 1000, "h": 1020},
    {"name": "TB-2a Web / API tier",        "x": 340,  "y": 290, "w": 960,  "h": 220},
    {"name": "TB-2b DocGrok tier",          "x": 340,  "y": 690, "w": 960,  "h": 220},
    {"name": "TB-2c Ingestor tier",         "x": 340,  "y": 1090, "w": 600, "h": 220},
    {"name": "TB-3 Azure managed services", "x": 1410, "y": 290, "w": 840,  "h": 480},
    {"name": "TB-4 Customer-owned",         "x": 1410, "y": 1090, "w": 480, "h": 480},
]

FLOWS: list[tuple[int, int, str]] = [
    (0, 1,  "HTTPS + Bearer (AAD JWT or minted token)"),
    (0, 2,  "HTTPS + Bearer (admin / AAD / minted)"),
    (2, 17, "JWKS fetch (cached, optional cert pin)"),
    (1, 2,  "internal HTTP"),
    (1, 3,  "internal HTTP"),
    (2, 9,  "metadata + auth_token read/write"),
    (2, 12, "secret fetch"),
    (2, 13, "enqueue"),
    (2, 4,  "extract/embed"),
    (3, 10, "vector query"),
    (3, 4,  "/embed (query embedding)"),
    (3, 9,  "index config read"),
    (4, 8,  "API key OR AAD"),
    (4, 7,  "embed (cluster)"),
    (4, 9,  "model record read"),
    (4, 5,  "dispatch work"),
    (5, 13, "enqueue chunks"),
    (5, 15, "fetch source PDF"),
    (5, 11, "fetch attachment binary"),
    (5, 4,  "embed callback"),
    (5, 10, "vector write"),
    (6, 14, "change-feed read"),
    (6, 11, "fetch attachment binary"),
    (6, 13, "enqueue work (queue mode)"),
    (6, 4,  "/embed/batch (inline mode)"),
    (6, 14, "vector patch (inline mode)"),
    (16, 13, "drain SB topic"),
    (16, 4,  "/embed/batch (queue mode)"),
    (16, 10, "vector write"),
    (16, 9,  "model record read"),
    (16, 11, "fetch attachment binary"),
    (2, 18, "telemetry / traces / metrics"),
    (3, 18, "telemetry / traces / metrics"),
    (6, 18, "telemetry / traces / metrics"),
    (16, 18, "telemetry / traces / metrics"),
]

# --- Helpers ----------------------------------------------------------------
_zid = [200]  # start above the template KB's max z:Id (~78)


def next_zid() -> str:
    _zid[0] += 1
    return f"i{_zid[0]}"


def gid() -> str:
    return str(uuid.uuid4())


def _hdr(name: str) -> str:
    return (
        f'<a:anyType i:type="b:HeaderDisplayAttribute" xmlns:b="{KB_NS}">'
        f"<b:DisplayName>{escape(name)}</b:DisplayName><b:Name/>"
        f'<b:Value i:nil="true"/></a:anyType>'
    )


def _str(display: str, value: str, name_guid: str = "") -> str:
    n = f"<b:Name>{name_guid}</b:Name>" if name_guid else "<b:Name/>"
    return (
        f'<a:anyType i:type="b:StringDisplayAttribute" xmlns:b="{KB_NS}">'
        f"<b:DisplayName>{escape(display)}</b:DisplayName>{n}"
        f'<b:Value i:type="c:string" xmlns:c="{XSD}">{escape(value)}</b:Value>'
        f"</a:anyType>"
    )


def _bool(display: str, value: bool, name_guid: str) -> str:
    return (
        f'<a:anyType i:type="b:BooleanDisplayAttribute" xmlns:b="{KB_NS}">'
        f"<b:DisplayName>{escape(display)}</b:DisplayName>"
        f"<b:Name>{name_guid}</b:Name>"
        f'<b:Value i:type="c:boolean" xmlns:c="{XSD}">'
        f"{str(value).lower()}</b:Value></a:anyType>"
    )


def _kv_wrap(g: str, zid: str, i_type: str, gtype: str, props: str, geom: str) -> str:
    return (
        "<a:KeyValueOfguidanyType>"
        f"<a:Key>{g}</a:Key>"
        f'<a:Value z:Id="{zid}" i:type="{i_type}">'
        f'<GenericTypeId xmlns="{ABS}">{gtype}</GenericTypeId>'
        f'<Guid xmlns="{ABS}">{g}</Guid>'
        f"{props}"
        f'<TypeId xmlns="{ABS}">{gtype}</TypeId>'
        f"{geom}"
        "</a:Value></a:KeyValueOfguidanyType>"
    )


def shape_xml(el: dict) -> str:
    g = gid()
    el["_guid"] = g
    kind = el["k"]
    if kind == "process":
        gtype, i_type, header = "GE.P", "StencilEllipse", "Generic Process"
    elif kind == "external":
        gtype, i_type, header = "GE.EI", "StencilRectangle", "Generic External Interactor"
    elif kind == "store":
        gtype, i_type, header = "GE.DS", "StencilParallelLines", "Generic Data Store"
    else:
        raise ValueError(kind)

    x, y = el["x"], el["y"]
    w, h = el.get("w", W), el.get("h", H)
    props = (
        f'<Properties xmlns="{ABS}">'
        + _hdr(header)
        + _str("Name", el["name"])
        + _bool("Out Of Scope", False, NAME_OUT_OF_SCOPE)
        + _str("Reason For Out Of Scope", "", NAME_REASON_OOS)
        + "</Properties>"
    )
    geom = (
        f'<Height xmlns="{ABS}">{h}</Height>'
        f'<Left xmlns="{ABS}">{x}</Left>'
        f'<StrokeDashArray i:nil="true" xmlns="{ABS}"/>'
        f'<StrokeThickness xmlns="{ABS}">1</StrokeThickness>'
        f'<Top xmlns="{ABS}">{y}</Top>'
        f'<Width xmlns="{ABS}">{w}</Width>'
    )
    return _kv_wrap(g, next_zid(), i_type, gtype, props, geom)


def boundary_xml(tb: dict) -> str:
    # GE.TB.B uses Header / Name / Dataflow Order — NO OutOfScope/Reason.
    g = gid()
    props = (
        f'<Properties xmlns="{ABS}">'
        + _hdr("Generic Trust Border Boundary")
        + _str("Name", tb["name"])
        + _str("Dataflow Order", "0", NAME_DATAFLOW_ORDER)
        + "</Properties>"
    )
    geom = (
        f'<Height xmlns="{ABS}">{tb["h"]}</Height>'
        f'<Left xmlns="{ABS}">{tb["x"]}</Left>'
        f'<StrokeDashArray i:nil="true" xmlns="{ABS}"/>'
        f'<StrokeThickness xmlns="{ABS}">1</StrokeThickness>'
        f'<Top xmlns="{ABS}">{tb["y"]}</Top>'
        f'<Width xmlns="{ABS}">{tb["w"]}</Width>'
    )
    return _kv_wrap(gid_for_tb := g, next_zid(), "BorderBoundary", "GE.TB.B", props, geom)


def line_xml(src_g: str, dst_g: str, label: str, sx: int, sy: int, dx: int, dy: int) -> str:
    g = gid()
    hx, hy = (sx + dx) // 2, (sy + dy) // 2
    props = (
        f'<Properties xmlns="{ABS}">'
        + _hdr("Generic Data Flow")
        + _str("Name", label)
        + _str("Dataflow Order", "0", NAME_DATAFLOW_ORDER)
        + _bool("Out Of Scope", False, NAME_OUT_OF_SCOPE)
        + _str("Reason For Out Of Scope", "", NAME_REASON_OOS)
        + "</Properties>"
    )
    geom = (
        f'<HandleX xmlns="{ABS}">{hx}</HandleX>'
        f'<HandleY xmlns="{ABS}">{hy}</HandleY>'
        f'<PortSource xmlns="{ABS}">East</PortSource>'
        f'<PortTarget xmlns="{ABS}">West</PortTarget>'
        f'<SourceGuid xmlns="{ABS}">{src_g}</SourceGuid>'
        f'<SourceX xmlns="{ABS}">{sx}</SourceX>'
        f'<SourceY xmlns="{ABS}">{sy}</SourceY>'
        f'<TargetGuid xmlns="{ABS}">{dst_g}</TargetGuid>'
        f'<TargetX xmlns="{ABS}">{dx}</TargetX>'
        f'<TargetY xmlns="{ABS}">{dy}</TargetY>'
    )
    return _kv_wrap(g, next_zid(), "Connector", "GE.DF", props, geom)


def replace_block(s: str, opening_tag_re: str, close_tag: str, new_inner: str) -> str:
    m = re.search(opening_tag_re, s)
    if not m:
        raise RuntimeError(f"opening tag not found: {opening_tag_re!r}")
    open_end = m.end()
    close_start = s.index(close_tag, open_end)
    return s[:open_end] + new_inner + s[close_start:]


def main() -> None:
    template_path = Path(os.environ.get(
        "OMNIVEC_TM7_TEMPLATE",
        r"C:\Users\prsasatt\Downloads\CmasBoA-ThreatModel 2025-01-20 (2).tm7",
    ))
    if not template_path.exists():
        raise SystemExit(
            f"Template tm7 not found: {template_path}\n"
            "Set OMNIVEC_TM7_TEMPLATE to a known-good .tm7 file."
        )
    out = Path(__file__).resolve().parent.parent / "docs" / "security" / "threat-model.tm7"

    s = template_path.read_text(encoding="utf-8")

    borders = "".join(shape_xml(e) for e in ELEMENTS) + "".join(boundary_xml(t) for t in TBS)
    lines = []
    for src, dst, label in FLOWS:
        sg = ELEMENTS[src]["_guid"]
        dg = ELEMENTS[dst]["_guid"]
        sx = ELEMENTS[src]["x"] + W // 2
        sy = ELEMENTS[src]["y"] + H // 2
        dx = ELEMENTS[dst]["x"] + W // 2
        dy = ELEMENTS[dst]["y"] + H // 2
        lines.append(line_xml(sg, dg, label, sx, sy, dx, dy))
    lines_inner = "".join(lines)

    s = replace_block(s, r"<Borders[^>]*>", "</Borders>", borders)
    s = replace_block(s, r"<Lines[^>]*>",   "</Lines>",   lines_inner)
    s = re.sub(r"<Header>[^<]*</Header>", "<Header>OmniVec</Header>", s, count=1)

    new_meta = (
        "<MetaInformation>"
        "<Assumptions>Public-network-access enabled on AOAI / Blob / Cosmos today; no private endpoints. AAD SSO required for browser; admin bearer token used by API and CLI.</Assumptions>"
        "<Contributors>OmniVec Team</Contributors>"
        "<ExternalDependencies>Azure OpenAI; Azure CosmosDB; Azure Blob Storage; Azure Key Vault; Azure Service Bus; Azure AD.</ExternalDependencies>"
        "<HighLevelSystemDescription>OmniVec ingests customer documents from CosmosDB / Blob sources, extracts text via docgrok-router, embeds via Azure OpenAI or in-cluster models (CLIP / BGE / DSE-Qwen2), and stores vectors in CosmosDB for similarity search served by omnivec-search. Browser UI served by omnivec-web with AAD SSO; admin and CLI use a bearer token to omnivec-api.</HighLevelSystemDescription>"
        "<Owner>OmniVec Team</Owner>"
        "<Reviewer>OmniVec Team</Reviewer>"
        "<ThreatModelName>OmniVec</ThreatModelName>"
        "</MetaInformation>"
    )
    s = re.sub(r"<MetaInformation>.*?</MetaInformation>", new_meta, s, count=1, flags=re.DOTALL)

    # Clear ThreatInstances (template has entries referencing original GUIDs).
    s = re.sub(
        r"<ThreatInstances([^>]*)>.*?</ThreatInstances>",
        r"<ThreatInstances\1/>",
        s, count=1, flags=re.DOTALL,
    )

    # Update DrawingSurfaceModel display name.
    s = re.sub(
        r'(<a:anyType i:type="b:StringDisplayAttribute"[^>]*>'
        r"<b:DisplayName>Name</b:DisplayName><b:Name/>"
        r'<b:Value i:type="c:string"[^>]*>)[^<]*(</b:Value>)',
        r"\1OmniVec System\2",
        s, count=1,
    )

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(s, encoding="utf-8")
    print(f"wrote {out} ({len(s):,} bytes)")
    print(f"elements={len(ELEMENTS)} trust-boundaries={len(TBS)} flows={len(FLOWS)}")


if __name__ == "__main__":
    main()
