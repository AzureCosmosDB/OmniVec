"""Unit tests for batch 7 (T-AAD-2 thumbprint helpers + RES-4 per-token rate-limit).

Pure unit tests — no network, no Cosmos.
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from typing import Optional

# Make api/ importable
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_ROOT, "api"))
sys.path.insert(0, os.path.join(_ROOT, "search"))

os.environ.setdefault("OMNIVEC_ADMIN_TOKEN", "test-token")
os.environ.setdefault("STORE_BACKEND", "memory")


class ThumbprintHelpersTests(unittest.TestCase):
    """T-AAD-2: SHA-256 thumbprint normalisation + parse."""

    def setUp(self):
        import api as api_mod
        self.api = api_mod

    def test_normalise_strips_separators(self):
        f = self.api._normalise_thumbprint
        # Same fingerprint, three formats
        a = "AA:bb:CC:dd:ee:ff:11:22:33:44:55:66:77:88:99:00:aa:bb:cc:dd:ee:ff:11:22:33:44:55:66:77:88:99:00"
        b = "AA bb CC dd ee ff 11 22 33 44 55 66 77 88 99 00 aa bb cc dd ee ff 11 22 33 44 55 66 77 88 99 00"
        c = "aabbccddeeff11223344556677889900aabbccddeeff11223344556677889900"
        self.assertEqual(f(a), f(b))
        self.assertEqual(f(a), f(c))
        self.assertEqual(len(f(c)), 64)

    def test_normalise_drops_non_hex(self):
        # Non-hex characters silently skipped
        self.assertEqual(self.api._normalise_thumbprint("zZ-aa-XX-bb"), "aabb")

    def test_parse_filters_malformed(self):
        valid = "a" * 64
        too_short = "ab"
        with_separators = ":".join(["aa"] * 32)  # 64 hex chars after stripping
        out = self.api._parse_pinned_thumbprints(f"{valid},{too_short},{with_separators}, ")
        self.assertIn(valid, out)
        self.assertIn("a" * 0 + "aa" * 32, out)  # i.e. 64 'a' too — wait, separators removed
        self.assertEqual(len(out), 2)
        self.assertNotIn(too_short, out)

    def test_parse_empty(self):
        self.assertEqual(self.api._parse_pinned_thumbprints(""), [])
        self.assertEqual(self.api._parse_pinned_thumbprints("   "), [])
        self.assertEqual(self.api._parse_pinned_thumbprints(",,"), [])


class SearchRateLimitOverrideTests(unittest.TestCase):
    """RES-4: per-token override semantics."""

    def setUp(self):
        # Force a known global limit and clear state between tests.
        import auth as search_auth
        self.auth = search_auth
        # 5 RPM global makes the test fast and deterministic
        search_auth.RATE_LIMIT_RPM = 5
        search_auth._rate_state.clear()

    def _hit(self, key: str, override: Optional[int], n: int) -> int:
        """Fire ``n`` checks back-to-back, return how many were allowed."""
        allowed = 0
        for _ in range(n):
            if self.auth.check_rate_limit(key, override_rpm=override):
                allowed += 1
        return allowed

    def test_global_limit_when_no_override(self):
        # 7 requests, global cap = 5 → only 5 allowed.
        self.assertEqual(self._hit("k1", None, 7), 5)

    def test_override_zero_means_uncapped(self):
        # 0 = uncapped per token → all 50 allowed even though global is 5.
        self.assertEqual(self._hit("k2", 0, 50), 50)

    def test_override_positive_clamps_below_global(self):
        # Override 2 < global 5 → 2 allowed out of 7.
        self.assertEqual(self._hit("k3", 2, 7), 2)

    def test_override_positive_above_global_takes_effect(self):
        # Override 10 > global 5 → all 7 allowed (override wins).
        self.assertEqual(self._hit("k4", 10, 7), 7)

    def test_buckets_isolated_per_key(self):
        # Two tokens with the same global cap don't share state.
        self.assertEqual(self._hit("a", None, 5), 5)
        self.assertEqual(self._hit("b", None, 5), 5)
        # 'a' is exhausted, 'b' is fresh.
        self.assertFalse(self.auth.check_rate_limit("a"))
        self.assertEqual(len(self.auth._rate_state["a"]), 5)


class AuthResultRateLimitKeyTests(unittest.TestCase):
    """RES-4: rate_limit_key prefers token_id when present."""

    def setUp(self):
        import auth as search_auth
        self.auth = search_auth

    def test_uses_token_id_when_present(self):
        r = self.auth.AuthResult(subject="alice", scope="search", source="store", token_id="t-123")
        self.assertEqual(r.rate_limit_key, "tok:t-123")

    def test_falls_back_to_subject(self):
        r = self.auth.AuthResult(subject="bootstrap", scope="search", source="env")
        self.assertEqual(r.rate_limit_key, "sub:bootstrap")

    def test_two_tokens_same_name_distinct_buckets(self):
        a = self.auth.AuthResult(subject="shared", scope="search", source="store", token_id="t-A")
        b = self.auth.AuthResult(subject="shared", scope="search", source="store", token_id="t-B")
        self.assertNotEqual(a.rate_limit_key, b.rate_limit_key)


if __name__ == "__main__":
    unittest.main()
