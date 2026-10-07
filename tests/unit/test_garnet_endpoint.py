import pytest


@pytest.mark.parametrize("tls,port", [(True, 6380), (False, 6379)])
def test_garnet_endpoint_aliases_match(api_app, tls, port):
    from garnet_endpoint import garnet_endpoint_identity
    expected = ("example.test", port, tls)
    for endpoint in ["EXAMPLE.test.", "redis://example.test/",
                     f"rediss://example.test:{port}", f"example.test:{port}"]:
        assert garnet_endpoint_identity({"endpoint": endpoint, "tls": tls}) == expected


def test_garnet_tls_port_and_ipv6_are_distinct(api_app):
    from garnet_endpoint import garnet_endpoint_identity
    assert garnet_endpoint_identity({"endpoint": "[::1]", "tls": False}) == ("::1", 6379, False)
    assert garnet_endpoint_identity({"endpoint": "host:6380", "tls": True}) != (
        garnet_endpoint_identity({"endpoint": "host:6380", "tls": False}))


@pytest.mark.parametrize("endpoint", ["", "host:0", "host:65536", "host:bad",
                                     "http://host", "redis://user:pass@host", "redis://host/key",
                                     "redis://host?x=1", "redis://host#fragment"])
def test_garnet_invalid_endpoints_fail_at_configuration_boundary(api_app, endpoint):
    from models import GarnetSourceConfig, GarnetDestinationConfig
    for config in [GarnetSourceConfig, GarnetDestinationConfig]:
        with pytest.raises(ValueError):
            config(endpoint=endpoint, hash_key="docs") if config is GarnetSourceConfig else config(endpoint=endpoint)
