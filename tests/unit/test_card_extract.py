"""Unit tests for the generic ``card_extract`` pipeline-worker core.

The ``card_extract`` module is intentionally stdlib-only, so this test
imports it directly from the pipeline-worker directory without pulling in
``worker.py``'s heavy deps (fitz / paddleocr / fastapi).
"""
from __future__ import annotations

import pathlib
import sys

import pytest

PW_DIR = pathlib.Path(__file__).resolve().parents[2] / "docgrok" / "pipeline-worker"
if str(PW_DIR) not in sys.path:
    sys.path.insert(0, str(PW_DIR))

import card_extract as ce  # noqa: E402


SPEC = {
    "model_id": "mdl-ext-aoai-chat",
    "instructions": "Split one card per distinct scenario.",
    "max_cards_per_chunk": 4,
    "fields": [
        {"name": "intent", "type": "text", "embed": True, "fts": True},
        {"name": "preconditions", "type": "list", "embed": True, "discriminator": True},
        {"name": "channel", "type": "enum", "values": ["online", "branch", "phone"],
         "embed": True, "discriminator": True},
        {"name": "requires_id", "type": "bool", "embed": True},
        {"name": "internal_note", "type": "text", "embed": False, "fts": False},
    ],
}


# --------------------------------------------------------------------------
# normalize_spec
# --------------------------------------------------------------------------
def test_normalize_spec_defaults_and_flags():
    spec = ce.normalize_spec(SPEC)
    assert [f["name"] for f in spec["fields"]] == \
        ["intent", "preconditions", "channel", "requires_id", "internal_note"]
    intent = spec["fields"][0]
    assert intent["embed"] is True and intent["fts"] is True and intent["discriminator"] is False
    note = spec["fields"][-1]
    assert note["embed"] is False
    assert spec["max_cards_per_chunk"] == 4
    assert spec["model_id"] == "mdl-ext-aoai-chat"
    # numeric guards
    assert spec["max_tokens"] >= 256
    assert isinstance(spec["temperature"], float)


def test_normalize_spec_requires_fields():
    with pytest.raises(ce.SpecError):
        ce.normalize_spec({"fields": []})
    with pytest.raises(ce.SpecError):
        ce.normalize_spec({})


def test_normalize_spec_rejects_bad_type_and_dupes_and_enum():
    with pytest.raises(ce.SpecError):
        ce.normalize_spec({"fields": [{"name": "x", "type": "blob"}]})
    with pytest.raises(ce.SpecError):
        ce.normalize_spec({"fields": [{"name": "x"}, {"name": "x"}]})
    with pytest.raises(ce.SpecError):
        ce.normalize_spec({"fields": [{"name": "x", "type": "enum"}]})  # missing values
    with pytest.raises(ce.SpecError):
        ce.normalize_spec({"fields": [{"type": "text"}]})  # missing name


# --------------------------------------------------------------------------
# build_messages
# --------------------------------------------------------------------------
def test_build_messages_mentions_fields_and_passage():
    spec = ce.normalize_spec(SPEC)
    msgs = ce.build_messages(spec, "Some passage text about address changes.")
    assert msgs[0]["role"] == "system"
    user = msgs[1]["content"]
    for name in ["intent", "preconditions", "channel", "requires_id"]:
        assert name in user
    assert "online|branch|phone" in user  # enum values surfaced
    assert "Some passage text about address changes." in user
    assert "Split one card per distinct scenario." in user  # instructions surfaced


# --------------------------------------------------------------------------
# parse_cards
# --------------------------------------------------------------------------
def test_parse_cards_fenced_json_and_coercion():
    spec = ce.normalize_spec(SPEC)
    content = (
        "```json\n"
        '{"cards": [\n'
        '  {"intent": "change address", "preconditions": ["future dated", "POA"],\n'
        '   "channel": "Online", "requires_id": "yes", "internal_note": "n/a"},\n'
        '  {"intent": "change address abroad", "preconditions": "abroad",\n'
        '   "channel": "mail", "requires_id": false, "internal_note": ""}\n'
        "]}\n"
        "```"
    )
    cards = ce.parse_cards(content, spec)
    assert len(cards) == 2
    c0 = cards[0]
    assert c0["preconditions"] == ["future dated", "POA"]
    assert c0["channel"] == "online"          # case-insensitive enum match
    assert c0["requires_id"] is True          # "yes" -> True
    c1 = cards[1]
    assert c1["preconditions"] == ["abroad"]  # scalar coerced to list
    assert c1["channel"] is None              # "mail" not in enum -> None
    assert c1["requires_id"] is False


def test_parse_cards_handles_bare_object_and_junk():
    spec = ce.normalize_spec(SPEC)
    bare = '{"intent": "x", "preconditions": [], "channel": null, "requires_id": null, "internal_note": ""}'
    assert len(ce.parse_cards(bare, spec)) == 1
    assert ce.parse_cards("not json at all", spec) == []
    assert ce.parse_cards("", spec) == []


def test_parse_cards_respects_max_cards():
    spec = ce.normalize_spec({**SPEC, "max_cards_per_chunk": 1})
    content = '{"cards": [{"intent": "a"}, {"intent": "b"}, {"intent": "c"}]}'
    assert len(ce.parse_cards(content, spec)) == 1


def test_parse_cards_drops_empty_cards():
    spec = ce.normalize_spec(SPEC)
    content = '{"cards": [{"intent": "", "preconditions": [], "channel": null, "requires_id": null, "internal_note": ""}]}'
    assert ce.parse_cards(content, spec) == []


# --------------------------------------------------------------------------
# rendering + metadata
# --------------------------------------------------------------------------
def test_render_card_text_only_embed_fields():
    spec = ce.normalize_spec(SPEC)
    card = {
        "intent": "change address",
        "preconditions": ["future dated", "POA"],
        "channel": "online",
        "requires_id": True,
        "internal_note": "secret ops note",
    }
    text = ce.render_card_text(card, spec)
    assert "intent: change address" in text
    assert "preconditions: future dated; POA" in text
    assert "channel: online" in text
    assert "requires_id: yes" in text
    assert "secret ops note" not in text  # embed=False excluded


def test_render_fts_and_metadata_discriminators():
    spec = ce.normalize_spec(SPEC)
    card = {
        "intent": "change address",
        "preconditions": ["future dated"],
        "channel": "branch",
        "requires_id": False,
        "internal_note": "x",
    }
    fts = ce.render_fts_text(card, spec)
    assert "intent: change address" in fts
    assert "channel" not in fts  # channel.fts is False

    meta = ce.card_metadata(card, spec)
    assert set(meta["_discriminators"].keys()) == {"preconditions", "channel"}
    assert meta["_discriminators"]["channel"] == "branch"
    assert meta["intent"] == "change address"


def test_render_card_text_fallback_when_no_embed_flags():
    spec = ce.normalize_spec({
        "fields": [
            {"name": "a", "type": "text", "embed": False},
            {"name": "b", "type": "text", "embed": False},
        ],
    })
    text = ce.render_card_text({"a": "hello", "b": "world"}, spec)
    assert "a: hello" in text and "b: world" in text
