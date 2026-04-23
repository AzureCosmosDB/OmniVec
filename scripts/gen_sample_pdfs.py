#!/usr/bin/env python3
"""Generate tiny, text-extractable PDFs for the blob-pdf e2e demo.

Uses only the Python stdlib (no reportlab, no external deps) so it runs
on any developer laptop and in CI. Output is a minimal PDF/1.4 with
a single Helvetica-typed page per file, containing a few lines of text.
DocGrok / PyPDF2 / pdfplumber can extract the text without issues.

Usage: gen_sample_pdfs.py <out_dir>
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List


SAMPLES: dict[str, List[str]] = {
    "azure-cosmos-db.pdf": [
        "Azure Cosmos DB",
        "",
        "Azure Cosmos DB is a globally distributed, multi-model database",
        "service for modern app development. It offers turnkey global",
        "distribution, elastic scaling of throughput and storage, and",
        "single-digit millisecond latency at the 99th percentile.",
        "",
        "Key features:",
        "- Multi-region writes with 99.999 percent availability SLA",
        "- Automatic and instant scalability",
        "- Native support for NoSQL and vector search workloads",
        "- Five well-defined consistency models",
        "- Change feed for event-driven processing",
    ],
    "azure-blob-storage.pdf": [
        "Azure Blob Storage",
        "",
        "Azure Blob Storage is Microsoft's object storage solution for",
        "the cloud. It is optimized for storing massive amounts of",
        "unstructured data such as text and binary data.",
        "",
        "Common use cases:",
        "- Serving images or documents directly to a browser",
        "- Storing files for distributed access",
        "- Streaming video and audio",
        "- Data lake storage for analytics workloads",
        "- Ingestion source for AI and embedding pipelines",
        "",
        "OmniVec reads blobs from a container, extracts text via DocGrok,",
        "embeds each chunk with a registered model, and writes vectors",
        "to the configured destination store.",
    ],
    "azure-kubernetes-service.pdf": [
        "Azure Kubernetes Service",
        "",
        "Azure Kubernetes Service (AKS) simplifies deploying a managed",
        "Kubernetes cluster in Azure. AKS handles critical tasks like",
        "health monitoring and maintenance so you focus on applications.",
        "",
        "Benefits:",
        "- Managed control plane at no cost",
        "- Integrated monitoring with Azure Monitor and Log Analytics",
        "- Built-in autoscaling for nodes and pods",
        "- Native integration with Azure AD and managed identities",
        "- Supports Windows and Linux node pools",
        "",
        "OmniVec deploys its API, worker, controller, search, web UI,",
        "and DocGrok services onto AKS via Helm charts.",
    ],
}


def _escape(s: str) -> bytes:
    """Escape a string for inclusion inside a PDF literal string."""
    return (
        s.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .encode("latin-1", errors="replace")
    )


def _build_content_stream(lines: List[str]) -> bytes:
    """Build a PDF content stream that renders the given lines with Helvetica."""
    parts: List[bytes] = [b"BT", b"/F1 12 Tf", b"14 TL"]
    # Start at (72, 760) in default PDF user space (612x792 letter).
    parts.append(b"72 760 Td")
    for i, line in enumerate(lines):
        if i == 0:
            parts.append(b"(" + _escape(line) + b") Tj")
        else:
            parts.append(b"T*")
            parts.append(b"(" + _escape(line) + b") Tj")
    parts.append(b"ET")
    return b"\n".join(parts)


def _write_pdf(path: Path, lines: List[str]) -> None:
    stream = _build_content_stream(lines)
    # Each object is encoded below with its trailing "\nendobj\n".
    objects: List[bytes] = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        (
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
            b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>"
        ),
        b"<</Length " + str(len(stream)).encode("ascii") + b">>\nstream\n"
        + stream
        + b"\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica/Encoding/WinAnsiEncoding>>",
    ]

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: List[int] = []
    for idx, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{idx} 0 obj\n".encode("ascii") + obj + b"\nendobj\n"

    xref_offset = len(out)
    size = len(objects) + 1
    out += f"xref\n0 {size}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<</Size {size}/Root 1 0 R>>\nstartxref\n{xref_offset}\n%%EOF\n"
    ).encode("ascii")

    path.write_bytes(bytes(out))


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: gen_sample_pdfs.py <out_dir>", file=sys.stderr)
        return 2
    out_dir = Path(sys.argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, lines in SAMPLES.items():
        target = out_dir / name
        _write_pdf(target, lines)
        print(f"wrote {target} ({target.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
