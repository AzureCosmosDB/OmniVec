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

# --- OmniVec architecture (mirrors threat-model.md §2 — high-level view) ----
# Per reviewer feedback: ≤10 shapes, logical components, two-line flow labels
# (line 1 = purpose, line 2 = how secured). Detailed per-pod / per-flow views
# live in the scenario mermaid diagrams in threat-model.md §3.
#
# Indices used by FLOWS:
#  0 Callers (external — browser, CLI, embedded user app)
#  1 Azure AD (external, OOS)
#  2 OmniVec (process — API + Search + DocGrok + Ingestion, single-tenant in customer AKS)
#  3 Azure OpenAI / Foundry (external, OOS — customer subscription)
#  4 Customer data plane (external — source CosmosDB/Blob, vectors destination)
ELEMENTS: list[dict] = [
    {"k": "external", "name": "Callers\n(browser, CLI, embedded user app)",        "x": 80,   "y": 540},
    {"k": "external", "name": "Azure AD (login.microsoftonline.com)",              "x": 80,   "y": 200,
        "oos": True, "oos_reason": "Microsoft-operated identity provider in the customer's tenant. OmniVec does not run, secure, configure, or rotate keys for AAD — it only consumes JWTs. Security of AAD itself is Microsoft's and the tenant admin's responsibility."},
    {"k": "process",  "name": "OmniVec\n(API, Search, DocGrok, Ingestion)\nsingle-tenant in customer AKS", "x": 700, "y": 540},
    {"k": "external", "name": "Azure OpenAI / Foundry\n(in customer subscription)", "x": 1300, "y": 200,
        "oos": True, "oos_reason": "Lives in customer subscription; OmniVec consumes the model endpoint over HTTPS via Managed Identity only. Customer owns model deployment, content filters, network ACLs, and RBAC."},
    {"k": "external", "name": "Customer data plane\n(source CosmosDB/Blob, vectors destination)", "x": 1300, "y": 540},
]

TBS: list[dict] = [
    {"name": "TB-1 Internet (callers — public HTTPS surface)",         "x": 30,   "y": 480, "w": 280,  "h": 240},
    {"name": "TB-1a Microsoft-operated identity (out of scope)",       "x": 30,   "y": 140, "w": 280,  "h": 200},
    {"name": "TB-2 OmniVec / AKS (single tenant)",                     "x": 600,  "y": 440, "w": 460,  "h": 340},
    {"name": "TB-3a Customer Azure subscription — Foundry (out of scope)", "x": 1240, "y": 140, "w": 380, "h": 220},
    {"name": "TB-4 Customer data plane",                               "x": 1240, "y": 480, "w": 380,  "h": 240},
]

# Flow label format (two lines, per reviewer guidance):
#   line 1: purpose / what it does
#   line 2: how it is secured (protocol · auth · authorization)
FLOWS: list[tuple[int, int, str]] = [
    (0, 2, "Queries / admin / token-mint\nHTTPS · AAD bearer (browser/CLI) or scope=search bearer (embedded app)"),
    (0, 1, "OIDC sign-in\nHTTPS · OIDC code flow"),
    (2, 1, "JWT validation (JWKS fetch)\nHTTPS · public endpoint · cached 1h"),
    (2, 3, "Embed call (consume only)\nHTTPS · Managed Identity (UAMI) or API key"),
    (2, 4, "Read documents/attachments, write vectors, change-feed\nHTTPS · Managed Identity (UAMI) or SAS · host allowlist · parser sandbox"),
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
    oos = bool(el.get("oos", False))
    oos_reason = el.get("oos_reason", "")
    props = (
        f'<Properties xmlns="{ABS}">'
        + _hdr(header)
        + _str("Name", el["name"])
        + _bool("Out Of Scope", oos, NAME_OUT_OF_SCOPE)
        + _str("Reason For Out Of Scope", oos_reason, NAME_REASON_OOS)
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
