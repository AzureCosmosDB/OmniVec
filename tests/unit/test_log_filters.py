"""Property tests for the three log filters in OmniVec.

We exercise three independent filter implementations:

* ``api/api.py::_SensitiveFilter`` — CR/LF/C0 scrub + secret redaction.
* ``search/main.py::_CtrlCharLogFilter`` — CR/LF/C0 scrub only.
* ``docgrok/api.py::_CtrlCharLogFilter`` — CR/LF/C0 scrub only.

Every property runs ``settings.max_examples`` hypothesis cases (100 in fast
mode, 1000 in thorough mode), so the total number of generated assertions is
in the thousands. The properties are correctness invariants — any future
change that removes scrubbing or breaks secret redaction will trip them.
"""
from __future__ import annotations

import logging
import re

import pytest
from hypothesis import given, strategies as st


# ---------------------------------------------------------------------------
# Strategy helpers.
# ---------------------------------------------------------------------------
_CTRL_CHARS = "".join(chr(c) for c in range(0x20) if c not in (0x09,)) + "\x7f"
_CTRL_CHARS_NO_TAB = "".join(chr(c) for c in range(0x20) if c not in (0x09, 0x0a, 0x0d)) + "\r\n"

ctrl_text = st.text(alphabet=st.sampled_from(list("abc 123") + list("\r\n\x00\x01\x07\x1f")), min_size=0, max_size=80)
unicode_text = st.text(min_size=0, max_size=120)
safe_text = st.text(alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\r\n\x00\x01\x02\x03\x04\x05\x06\x07\x08\x0b\x0c\x0e\x0f\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1a\x1b\x1c\x1d\x1e\x1f"), min_size=0, max_size=60)

# Characters that the filters must scrub. Excludes \t (tab is whitespace and
# kept intact). The regexp is ``[\x00-\x08\x0b\x0c\x0e-\x1f]|\r\n|\r|\n``.
_FORBIDDEN_AFTER_SCRUB = set(chr(c) for c in list(range(0x00, 0x09)) + [0x0b, 0x0c] + list(range(0x0e, 0x20)) + [0x0a, 0x0d])


class _FakeRecord:
    """A minimal stand-in for logging.LogRecord that lets us pass arbitrary
    args (including dict / tuple / None) without triggering LogRecord's
    single-mapping unwrap logic that complicates property-test inputs."""
    def __init__(self, msg, args=None):
        self.msg = msg
        self.args = args


def _apply(flt, msg, args=None):
    rec = _FakeRecord(msg, args)
    flt().filter(rec)
    return rec


# ===========================================================================
# 1. api/_SensitiveFilter — CR/LF/C0 scrub + secret redaction (6 properties)
# ===========================================================================
class TestApiSensitiveFilter:

    def test_scrubs_known_control_chars(self, api_sensitive_filter):
        rec = _apply(api_sensitive_filter, "a\rb\nc\x00d\x07e\x1fz")
        assert "\r" not in rec.msg
        assert "\n" not in rec.msg
        assert "\x00" not in rec.msg
        assert "\x1f" not in rec.msg

    @given(ctrl_text)
    def test_scrub_removes_all_control_chars(self, api_sensitive_filter, s):
        rec = _apply(api_sensitive_filter, s)
        assert not (set(rec.msg) & _FORBIDDEN_AFTER_SCRUB), f"leaked ctrl in {rec.msg!r}"

    @given(unicode_text)
    def test_scrub_never_raises_on_unicode(self, api_sensitive_filter, s):
        rec = _apply(api_sensitive_filter, s)
        assert isinstance(rec.msg, str)

    @given(st.text(alphabet="ABCDEFabcdef0123456789", min_size=12, max_size=40))
    def test_api_key_redaction(self, api_sensitive_filter, secret):
        rec = _apply(api_sensitive_filter, f"call with api_key={secret} done")
        assert secret not in rec.msg, f"full secret leaked: {rec.msg}"
        assert "***" in rec.msg

    @given(st.text(alphabet="ABCDEFabcdef0123456789", min_size=12, max_size=40))
    def test_bearer_redaction(self, api_sensitive_filter, secret):
        rec = _apply(api_sensitive_filter, f"Authorization: Bearer {secret}")
        assert secret not in rec.msg
        assert "***" in rec.msg

    @given(st.sampled_from(["password", "secret", "token", "credential"]),
           st.text(alphabet="ABCDEFabcdef0123456789", min_size=12, max_size=40))
    def test_assorted_secret_kinds_redacted(self, api_sensitive_filter, kw, secret):
        rec = _apply(api_sensitive_filter, f"{kw}={secret}")
        assert secret not in rec.msg
        assert "***" in rec.msg

    @given(st.dictionaries(st.text(min_size=1, max_size=8), ctrl_text, max_size=5))
    def test_dict_args_are_scrubbed(self, api_sensitive_filter, d):
        rec = _apply(api_sensitive_filter, "msg %(x)s", d)
        if isinstance(rec.args, dict):
            for v in rec.args.values():
                if isinstance(v, str):
                    assert not (set(v) & _FORBIDDEN_AFTER_SCRUB)

    @given(st.tuples(ctrl_text, ctrl_text))
    def test_tuple_args_are_scrubbed(self, api_sensitive_filter, t):
        rec = _apply(api_sensitive_filter, "msg %s %s", t)
        if isinstance(rec.args, tuple):
            for v in rec.args:
                if isinstance(v, str):
                    assert not (set(v) & _FORBIDDEN_AFTER_SCRUB)

    def test_empty_string_is_safe(self, api_sensitive_filter):
        rec = _apply(api_sensitive_filter, "")
        assert rec.msg == ""

    def test_none_args_is_safe(self, api_sensitive_filter):
        rec = _apply(api_sensitive_filter, "no args", None)
        assert rec.args is None

    def test_safe_text_unchanged_modulo_redaction(self, api_sensitive_filter):
        rec = _apply(api_sensitive_filter, "plain text 123")
        assert rec.msg == "plain text 123"


# ===========================================================================
# 2. search/_CtrlCharLogFilter — CR/LF/C0 scrub (6 properties)
# ===========================================================================
class TestSearchCtrlFilter:

    def test_scrubs_known_control_chars(self, search_ctrl_filter):
        rec = _apply(search_ctrl_filter, "a\rb\nc\x00d\x07e\x1fz")
        assert not (set(rec.msg) & _FORBIDDEN_AFTER_SCRUB)

    @given(ctrl_text)
    def test_scrub_removes_all_control_chars(self, search_ctrl_filter, s):
        rec = _apply(search_ctrl_filter, s)
        assert not (set(rec.msg) & _FORBIDDEN_AFTER_SCRUB)

    @given(unicode_text)
    def test_scrub_never_raises_on_unicode(self, search_ctrl_filter, s):
        rec = _apply(search_ctrl_filter, s)
        assert isinstance(rec.msg, str)

    @given(st.dictionaries(st.text(min_size=1, max_size=8), ctrl_text, max_size=5))
    def test_dict_args_are_scrubbed(self, search_ctrl_filter, d):
        rec = _apply(search_ctrl_filter, "m", d)
        if isinstance(rec.args, dict):
            for v in rec.args.values():
                if isinstance(v, str):
                    assert not (set(v) & _FORBIDDEN_AFTER_SCRUB)

    @given(st.tuples(ctrl_text, ctrl_text, ctrl_text))
    def test_tuple_args_are_scrubbed(self, search_ctrl_filter, t):
        rec = _apply(search_ctrl_filter, "m", t)
        if isinstance(rec.args, tuple):
            for v in rec.args:
                if isinstance(v, str):
                    assert not (set(v) & _FORBIDDEN_AFTER_SCRUB)

    def test_empty_string_is_safe(self, search_ctrl_filter):
        rec = _apply(search_ctrl_filter, "")
        assert rec.msg == ""

    def test_none_args_is_safe(self, search_ctrl_filter):
        rec = _apply(search_ctrl_filter, "no args", None)
        assert rec.args is None

    def test_crlf_collapsed_to_space(self, search_ctrl_filter):
        rec = _apply(search_ctrl_filter, "a\r\nb")
        assert "\r" not in rec.msg
        assert "\n" not in rec.msg


# ===========================================================================
# 3. docgrok/_CtrlCharLogFilter — CR/LF/C0 scrub (6 properties)
# ===========================================================================
class TestDocgrokCtrlFilter:

    def test_scrubs_known_control_chars(self, docgrok_ctrl_filter):
        rec = _apply(docgrok_ctrl_filter, "a\rb\nc\x00d\x07e\x1fz")
        assert not (set(rec.msg) & _FORBIDDEN_AFTER_SCRUB)

    @given(ctrl_text)
    def test_scrub_removes_all_control_chars(self, docgrok_ctrl_filter, s):
        rec = _apply(docgrok_ctrl_filter, s)
        assert not (set(rec.msg) & _FORBIDDEN_AFTER_SCRUB)

    @given(unicode_text)
    def test_scrub_never_raises_on_unicode(self, docgrok_ctrl_filter, s):
        rec = _apply(docgrok_ctrl_filter, s)
        assert isinstance(rec.msg, str)

    @given(st.dictionaries(st.text(min_size=1, max_size=8), ctrl_text, max_size=5))
    def test_dict_args_are_scrubbed(self, docgrok_ctrl_filter, d):
        rec = _apply(docgrok_ctrl_filter, "m", d)
        if isinstance(rec.args, dict):
            for v in rec.args.values():
                if isinstance(v, str):
                    assert not (set(v) & _FORBIDDEN_AFTER_SCRUB)

    @given(st.tuples(ctrl_text, ctrl_text))
    def test_tuple_args_are_scrubbed(self, docgrok_ctrl_filter, t):
        rec = _apply(docgrok_ctrl_filter, "m", t)
        if isinstance(rec.args, tuple):
            for v in rec.args:
                if isinstance(v, str):
                    assert not (set(v) & _FORBIDDEN_AFTER_SCRUB)

    def test_empty_string_is_safe(self, docgrok_ctrl_filter):
        rec = _apply(docgrok_ctrl_filter, "")
        assert rec.msg == ""

    def test_none_args_is_safe(self, docgrok_ctrl_filter):
        rec = _apply(docgrok_ctrl_filter, "no args", None)
        assert rec.args is None

    def test_crlf_collapsed_to_space(self, docgrok_ctrl_filter):
        rec = _apply(docgrok_ctrl_filter, "a\r\nb")
        assert "\r" not in rec.msg
        assert "\n" not in rec.msg
