"""OmniVec Text Chunker — splits text into overlapping chunks for embedding."""

import hashlib
from typing import List, Tuple


def chunk_text(
    text: str,
    chunk_size: int = 1000,
    chunk_overlap: int = 200,
    chunk_unit: str = "chars",
) -> List[Tuple[str, int]]:
    """Split text into overlapping chunks.

    Returns list of (chunk_text, chunk_index) tuples.
    Tries to break on paragraph/sentence boundaries when possible.
    """
    if not text or not text.strip():
        return []

    if chunk_unit == "tokens":
        return _chunk_by_tokens(text, chunk_size, chunk_overlap)
    return _chunk_by_chars(text, chunk_size, chunk_overlap)


def _chunk_by_chars(text: str, size: int, overlap: int) -> List[Tuple[str, int]]:
    """Character-based chunking with overlap, preferring paragraph/sentence breaks."""
    if len(text) <= size:
        return [(text, 0)]

    chunks = []
    start = 0
    idx = 0

    while start < len(text):
        end = min(start + size, len(text))

        # If not at the end, try to find a good break point
        if end < len(text):
            search_start = start + int(size * 0.8)
            # Look for paragraph break (\n\n) in the last 20% of the chunk
            para_break = text.rfind("\n\n", search_start, end)
            if para_break > search_start:
                end = para_break + 2
            else:
                # Fall back to sentence break (. ! ?)
                best = -1
                for pat in [". ", "! ", "? ", ".\n", "!\n", "?\n"]:
                    pos = text.rfind(pat, search_start, end)
                    if pos > best:
                        best = pos
                if best > search_start:
                    end = best + 2

        chunk = text[start:end].strip()
        if chunk:
            chunks.append((chunk, idx))
            idx += 1

        if end >= len(text):
            break
        start = end - overlap
        # Prevent infinite loop
        if start <= (chunks[-1][1] if not chunks else 0):
            start = end

    return chunks


def _chunk_by_tokens(text: str, size: int, overlap: int) -> List[Tuple[str, int]]:
    """Token-based chunking using whitespace tokenization (approx ~1.3 tokens/word)."""
    words = text.split()
    if len(words) <= size:
        return [(text, 0)]

    chunks = []
    start = 0
    idx = 0

    while start < len(words):
        end = min(start + size, len(words))
        chunk = " ".join(words[start:end])
        if chunk.strip():
            chunks.append((chunk, idx))
            idx += 1
        if end >= len(words):
            break
        start = end - overlap

    return chunks


def _source_hash(source_ref: str) -> str:
    return hashlib.sha256(source_ref.encode("utf-8")).hexdigest()[:12]


def _pipeline_hash(pipeline_id: str) -> str:
    return hashlib.sha256(pipeline_id.encode("utf-8")).hexdigest()[:8]


def _source_name(source_ref: str) -> str:
    """Extract filename without extension from source_ref (e.g. 'docs/report.pdf' -> 'report')."""
    import os
    base = os.path.basename(source_ref)
    name, _ = os.path.splitext(base)
    return name


def make_chunk_doc_id(pipeline_id: str, source_ref: str, chunk_index: int, pattern: str = "") -> str:
    """Build chunk document ID from pattern or default.

    Pattern variables:
      {source}         - source filename without extension (e.g. 'report')
      {source_ref}     - full source ref path (e.g. 'docs/report.pdf')
      {source_hash}    - 12-char SHA256 of source_ref
      {chunk}          - zero-padded chunk index (e.g. '003')
      {pipeline}       - pipeline ID
      {pipeline_hash}  - 8-char SHA256 of pipeline ID
    """
    if not pattern:
        sh = _source_hash(source_ref)
        return f"{pipeline_id}-{sh}-chunk-{chunk_index:03d}"

    return pattern.format(
        source=_source_name(source_ref),
        source_ref=source_ref.replace("/", "-").replace("\\", "-"),
        source_hash=_source_hash(source_ref),
        chunk=f"{chunk_index:03d}",
        pipeline=pipeline_id,
        pipeline_hash=_pipeline_hash(pipeline_id),
    )


def make_chunk_prefix(pipeline_id: str, source_ref: str, pattern: str = "") -> str:
    """Prefix for all chunks of a given source document (for cleanup queries)."""
    if not pattern:
        sh = _source_hash(source_ref)
        return f"{pipeline_id}-{sh}-chunk-"

    # Build prefix by rendering pattern with chunk="", then trimming trailing separator
    prefix = pattern.format(
        source=_source_name(source_ref),
        source_ref=source_ref.replace("/", "-").replace("\\", "-"),
        source_hash=_source_hash(source_ref),
        chunk="",
        pipeline=pipeline_id,
        pipeline_hash=_pipeline_hash(pipeline_id),
    )
    return prefix
