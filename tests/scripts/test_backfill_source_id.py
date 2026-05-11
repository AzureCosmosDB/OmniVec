"""Unit tests for scripts/backfill_source_id.py (T-VEC-2 backfill).

Pure unit tests against ``_derive_source_id``: no network, no Cosmos, no
Postgres. The integration paths (``_backfill_cosmos`` / ``_backfill_postgres``)
exercise external services and are out of scope for offline CI.
"""
from __future__ import annotations

import os
import sys
import unittest

# Make scripts/ importable
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_ROOT, "scripts"))

import backfill_source_id as bf  # noqa: E402


class DeriveSourceIdTests(unittest.TestCase):
    def test_single_source_pipeline_unambiguous(self):
        # Single candidate: always returns it, even when the doc_id wouldn't
        # otherwise match the prefix heuristic.
        self.assertEqual(
            bf._derive_source_id("legacy-doc-42", "pipe", ["only-source"]),
            "only-source",
        )

    def test_multi_source_prefix_match(self):
        self.assertEqual(
            bf._derive_source_id("pipe1-srcA-doc-1", "pipe1", ["srcA", "srcB"]),
            "srcA",
        )
        self.assertEqual(
            bf._derive_source_id("pipe1-srcB-doc-2", "pipe1", ["srcA", "srcB"]),
            "srcB",
        )

    def test_multi_source_exact_match(self):
        # Edge case: the doc_id ends exactly with the source id (no trailing
        # segment).
        self.assertEqual(
            bf._derive_source_id("pipe1-srcA", "pipe1", ["srcA", "srcB"]),
            "srcA",
        )

    def test_doc_id_missing_pipeline_prefix(self):
        # Multi-source with a doc_id that doesn't follow the convention =>
        # returns None (forces operator review / --strategy=primary).
        self.assertIsNone(
            bf._derive_source_id("orphan-doc-99", "pipe1", ["srcA", "srcB"]),
        )

    def test_multi_source_no_match(self):
        # Pipeline prefix is right but neither source id appears.
        self.assertIsNone(
            bf._derive_source_id("pipe1-other-doc", "pipe1", ["srcA", "srcB"]),
        )

    def test_substring_collision_uses_dash_boundary(self):
        # 'srcA' must not match 'srcAlpha-...': the heuristic anchors on a
        # trailing dash. Order in candidates shouldn't matter for correctness.
        self.assertEqual(
            bf._derive_source_id("pipe1-srcAlpha-doc", "pipe1", ["srcA", "srcAlpha"]),
            "srcAlpha",
        )


if __name__ == "__main__":
    unittest.main()
