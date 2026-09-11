from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).parents[2] / "search"))

from schemas import GarnetStore, IndexSpec, ModelEmbedding, SearchResult  # noqa: E402
from searcher import _garnet_score, _parse_garnet_results  # noqa: E402


def test_garnet_store_is_valid_index_store():
    index = IndexSpec(
        id="garnet",
        store=GarnetStore(endpoint="cache.example:10000"),
        embedding=ModelEmbedding(model_id="text-model"),
    )
    assert index.store.vector_set == "omnivec-vectors"
    assert index.store.use_entra_auth is False


def test_parse_garnet_vsim_results():
    raw = [
        b"element-1",
        b"0.25",
        (
            b'{"id":"element-1","source_id":"src","source_ref":"row-1",'
            b'"pipeline_id":"pipe","content":"hello"}'
        ),
    ]
    hits = _parse_garnet_results(raw, "cosine", include_vector=False)
    assert hits == [
        {
            "id": "element-1",
            "score": 0.75,
            "distance": 0.25,
            "text": "hello",
            "text_parts": None,
            "metadata": {
                "id": "element-1",
                "source_id": "src",
                "source_ref": "row-1",
                "pipeline_id": "pipe",
            },
            "source": "src",
            "source_ref": "row-1",
        }
    ]


def test_parse_garnet_source_content_fields_matches_response_schema():
    raw = [
        b"element-1",
        b"0",
        (
            b'{"id":"element-1","source_id":"src","source_ref":"row-1",'
            b'"source_content_fields":{"title":"Vector databases","body":"Semantic search"}}'
        ),
    ]
    hit = _parse_garnet_results(raw, "cosine", include_vector=False)[0]
    assert hit["text"] == "Vector databases\n\nSemantic search"
    assert hit["text_parts"] == [
        {"field": "title", "value": "Vector databases"},
        {"field": "body", "value": "Semantic search"},
    ]
    SearchResult(index_id="garnet", rank=1, **hit)


def test_garnet_l2_score_is_bounded():
    assert _garnet_score(3.0, "l2") == 0.25
