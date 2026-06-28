"""Generic, dataset-agnostic LLM card extraction for the DocGrok pipeline.

The ``card_extract`` pipeline stage turns each text chunk into one or more
self-contained "cards" whose shape is defined **entirely** by a user-supplied
extraction spec carried in the stage config — and therefore stored inside the
transform / pipeline definition. Nothing in this module is specific to any
dataset, domain, customer or document type.

Stage config (the "extraction spec")::

    {
      "model_id": "<chat model id registered in the DocGrok router>",
      "instructions": "<optional extra guidance for the extractor>",
      "max_cards_per_chunk": 6,
      "fields": [
        {"name": "intent",        "type": "text", "embed": true, "fts": true},
        {"name": "preconditions", "type": "list", "embed": true, "discriminator": true},
        {"name": "steps",         "type": "text", "embed": true},
        {"name": "keywords",      "type": "list", "embed": true, "fts": true}
      ]
    }

Field attributes:

* ``name``          — the field key on each card (required, unique).
* ``type``          — one of ``text | list | enum | bool``.
* ``embed``         — include the field in the text that gets embedded
                      (default ``True``).
* ``fts``           — include the field in the text indexed for full-text
                      search (default ``False``).
* ``discriminator`` — mark the field as a distinguishing facet, kept as
                      metadata for filtering / boosting (default ``False``).
* ``values``        — allowed values when ``type == "enum"`` (required for enum).
* ``description``   — optional natural-language hint passed to the extractor.

The module is intentionally dependency-light (standard library only) so it is
trivially unit-testable and importable from both the pipeline worker and any
offline evaluation harness.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List

FIELD_TYPES = ("text", "list", "enum", "bool")


class SpecError(ValueError):
    """Raised when a card_extract config / extraction spec is invalid."""


# ---------------------------------------------------------------------------
# Spec normalization
# ---------------------------------------------------------------------------
def normalize_spec(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize a raw stage config into a canonical spec dict.

    Raises ``SpecError`` for any structurally invalid spec so the transform
    validator can reject it early.
    """
    raw_fields = cfg.get("fields") or []
    if not isinstance(raw_fields, list) or not raw_fields:
        raise SpecError("card_extract requires a non-empty 'fields' list in its config")

    fields: List[Dict[str, Any]] = []
    seen = set()
    for i, f in enumerate(raw_fields):
        if not isinstance(f, dict):
            raise SpecError(f"fields[{i}] must be an object")
        name = str(f.get("name") or "").strip()
        if not name:
            raise SpecError(f"fields[{i}] is missing 'name'")
        if name in seen:
            raise SpecError(f"duplicate field name '{name}'")
        seen.add(name)
        ftype = str(f.get("type") or "text").strip().lower()
        if ftype not in FIELD_TYPES:
            raise SpecError(
                f"field '{name}' has invalid type '{ftype}' (allowed: {', '.join(FIELD_TYPES)})"
            )
        values = f.get("values") or f.get("enum") or []
        if not isinstance(values, list):
            raise SpecError(f"field '{name}' 'values' must be a list")
        if ftype == "enum" and not values:
            raise SpecError(f"enum field '{name}' requires a non-empty 'values' list")
        fields.append({
            "name": name,
            "type": ftype,
            "embed": bool(f.get("embed", True)),
            "fts": bool(f.get("fts", False)),
            "discriminator": bool(f.get("discriminator", False)),
            "values": [str(v) for v in values],
            "description": str(f.get("description") or "").strip(),
        })

    return {
        "fields": fields,
        "model_id": cfg.get("model_id"),
        "instructions": str(cfg.get("instructions") or "").strip(),
        "max_cards_per_chunk": max(1, int(cfg.get("max_cards_per_chunk", 6))),
        "max_tokens": max(256, int(cfg.get("max_tokens", 1200))),
        "temperature": float(cfg.get("temperature", 0.1)),
    }


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are a precise information-extraction engine. You read a passage from a "
    "document and produce one or more self-contained knowledge CARDS. Each card "
    "captures a single distinct topic, scenario or procedure found in the passage. "
    "If the passage covers several distinct cases that differ by their conditions, "
    "emit a SEPARATE card for each so they are never blended together. Extract only "
    "what the passage supports — never invent facts. Respond with STRICT JSON only."
)


def _field_hint(f: Dict[str, Any]) -> str:
    t = f["type"]
    if t == "enum":
        t = "enum(" + "|".join(f["values"]) + ")"
    desc = f" — {f['description']}" if f["description"] else ""
    return f'- "{f["name"]}" ({t}){desc}'


def build_messages(spec: Dict[str, Any], text: str) -> List[Dict[str, str]]:
    """Build the chat messages that ask an LLM to extract cards from ``text``."""
    fields = spec["fields"]
    field_lines = "\n".join(_field_hint(f) for f in fields)
    names = [f["name"] for f in fields]
    shape = ", ".join(f"{json.dumps(n)}: ..." for n in names)
    rules = [
        f'Return JSON of the form: {{"cards": [{{{shape}}}]}}.',
        "Each card MUST contain exactly these keys: "
        + ", ".join(json.dumps(n) for n in names) + ".",
        "Types: text -> string; list -> array of short strings; bool -> true/false; "
        "enum -> exactly one of the allowed values (or null if none apply).",
        "Use null (or [] for a list) when the passage does not provide a field.",
        f"Emit at most {spec['max_cards_per_chunk']} cards; prefer fewer, higher-quality cards.",
        "Output JSON only — no markdown fences, no commentary.",
    ]
    if spec["instructions"]:
        rules.append("Additional guidance: " + spec["instructions"])
    numbered = "\n".join(f"{i + 1}. {r}" for i, r in enumerate(rules))
    user = (
        "FIELDS to extract for each card:\n" + field_lines + "\n\n"
        "RULES:\n" + numbered + "\n\n"
        "PASSAGE:\n" + text
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# Response parsing + value coercion
# ---------------------------------------------------------------------------
def _strip_fences(s: str) -> str:
    s = (s or "").strip()
    if s.startswith("```"):
        parts = s.split("```")
        # ```json\n{...}\n``` -> parts[1] holds the body
        s = parts[1] if len(parts) >= 2 else s.strip("`")
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    return s.strip()


def _nonempty(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, (list, str)):
        return len(v) > 0
    return True


def _coerce(value: Any, f: Dict[str, Any]) -> Any:
    t = f["type"]
    if t == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("true", "yes", "1", "y")
        return None
    if t == "list":
        if value is None:
            return []
        if isinstance(value, list):
            return [str(x).strip() for x in value if str(x).strip()]
        return [s.strip() for s in re.split(r"[;\n]", str(value)) if s.strip()]
    if t == "enum":
        if value is None:
            return None
        v = str(value).strip()
        for allowed in f["values"]:
            if v.lower() == allowed.lower():
                return allowed
        return None
    # text
    if value is None:
        return ""
    if isinstance(value, list):
        return "; ".join(str(x) for x in value)
    return str(value).strip()


def parse_cards(content: str, spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Parse an LLM response into a list of typed cards conforming to ``spec``.

    Tolerant of code fences and surrounding prose; returns ``[]`` on failure so
    the stage can fall back to the original chunk.
    """
    raw = _strip_fences(content or "")
    if not raw:
        return []
    data: Any
    try:
        data = json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except Exception:
            return []

    cards_raw = data.get("cards") if isinstance(data, dict) else data
    if cards_raw is None and isinstance(data, dict):
        # A bare single-card object, returned without the {"cards": [...]} wrapper.
        cards_raw = [data]
    if isinstance(cards_raw, dict):
        cards_raw = [cards_raw]
    if not isinstance(cards_raw, list):
        return []

    out: List[Dict[str, Any]] = []
    for c in cards_raw[: spec["max_cards_per_chunk"]]:
        if not isinstance(c, dict):
            continue
        card = {f["name"]: _coerce(c.get(f["name"]), f) for f in spec["fields"]}
        if any(_nonempty(card[f["name"]]) for f in spec["fields"]):
            out.append(card)
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _render(card: Dict[str, Any], fields: List[Dict[str, Any]],
            predicate: Callable[[Dict[str, Any]], bool]) -> str:
    lines = []
    for f in fields:
        if not predicate(f):
            continue
        v = card.get(f["name"])
        if not _nonempty(v):
            continue
        if isinstance(v, list):
            v = "; ".join(v)
        elif isinstance(v, bool):
            v = "yes" if v else "no"
        lines.append(f"{f['name']}: {v}")
    return "\n".join(lines)


def render_card_text(card: Dict[str, Any], spec: Dict[str, Any]) -> str:
    """The natural-language text that gets embedded (fields with ``embed``).

    If no field is flagged ``embed``, all fields are rendered as a safe default.
    """
    fields = spec["fields"]
    any_embed = any(f["embed"] for f in fields)
    pred = (lambda f: f["embed"]) if any_embed else (lambda f: True)
    return _render(card, fields, pred)


def render_fts_text(card: Dict[str, Any], spec: Dict[str, Any]) -> str:
    """Text composed of the ``fts``-flagged fields (empty string if none)."""
    return _render(card, spec["fields"], lambda f: f["fts"])


def card_metadata(card: Dict[str, Any], spec: Dict[str, Any]) -> Dict[str, Any]:
    """All extracted field values, plus a ``_discriminators`` subset that holds
    only the fields flagged ``discriminator`` (for filtering / boosting)."""
    fields = spec["fields"]
    meta = {f["name"]: card.get(f["name"]) for f in fields}
    meta["_discriminators"] = {
        f["name"]: card.get(f["name"]) for f in fields if f["discriminator"]
    }
    return meta
