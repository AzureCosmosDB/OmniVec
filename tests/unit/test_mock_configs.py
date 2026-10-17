import pytest


def test_registered_mock_component_defaults(api_app):
    from mock_configs import MockSourceConfig, MockSinkConfig, MockEmbeddingConfig
    source = MockSourceConfig()
    sink = MockSinkConfig()
    model = MockEmbeddingConfig()
    assert source.document_count == 100000
    assert source.document_size_bytes == 8
    assert source.documents_per_second == sink.accepted_documents_per_second == 0
    assert sink.embedding_dimensions == model.embedding_dim == 1024
    assert source.embedding_transport == "json"
    assert MockSourceConfig(embedding_transport="fp32").embedding_transport == "fp32"
    with pytest.raises(ValueError):
        MockSourceConfig(embedding_transport="counts-only")


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), float("-inf")])
def test_mock_component_rates_reject_invalid_values(api_app, value):
    from mock_configs import MockSourceConfig, MockSinkConfig, MockEmbeddingConfig
    for config, field in [
        (MockSourceConfig, "documents_per_second"),
        (MockSinkConfig, "accepted_documents_per_second"),
        (MockEmbeddingConfig, "latency_ms"),
    ]:
        with pytest.raises(ValueError):
            config(**{field: value})


@pytest.mark.parametrize("field,value", [
    ("document_count", 0), ("document_count", 10000001),
    ("document_size_bytes", 0), ("document_size_bytes", 1048577),
    ("batch_size", 0), ("batch_size", 2049),
])
def test_mock_source_bounds(api_app, field, value):
    from mock_configs import MockSourceConfig
    with pytest.raises(ValueError):
        MockSourceConfig(**{field: value})


def test_mock_configs_reject_unknown_fields_and_accept_fractional_rates(api_app):
    from mock_configs import MockSourceConfig, MockSinkConfig, MockEmbeddingConfig
    assert MockSourceConfig(documents_per_second=0.5).documents_per_second == 0.5
    assert MockSinkConfig(accepted_documents_per_second=1.5).accepted_documents_per_second == 1.5
    for config in (MockSourceConfig, MockSinkConfig, MockEmbeddingConfig):
        with pytest.raises(ValueError):
            config(typo=True)
