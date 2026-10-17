import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "docgrok"
    / "pipeline-worker"
    / "blob_limits.py"
)
SPEC = importlib.util.spec_from_file_location("docgrok_blob_limits", MODULE_PATH)
blob_limits = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(blob_limits)


class FakeBlobClient:
    def __init__(self, size):
        self.size = size

    def get_blob_properties(self):
        return SimpleNamespace(size=self.size)


def test_blob_at_limit_is_allowed():
    assert blob_limits.enforce_blob_size(
        FakeBlobClient(50 * 1024 * 1024),
        50 * 1024 * 1024,
        "buffered document",
    ) == 50 * 1024 * 1024


def test_oversized_blob_is_rejected_before_download():
    with pytest.raises(blob_limits.BlobSizeLimitExceeded) as caught:
        blob_limits.enforce_blob_size(
            FakeBlobClient(250 * 1024 * 1024),
            50 * 1024 * 1024,
            "buffered document",
        )

    assert caught.value.size_bytes == 250 * 1024 * 1024
    assert caught.value.limit_bytes == 50 * 1024 * 1024
    assert "buffered document" in str(caught.value)


@pytest.mark.parametrize("value", ["0", "-1", "abc", ""])
def test_byte_limits_must_be_positive(value):
    with pytest.raises(ValueError, match="must be a positive integer"):
        blob_limits.require_positive_byte_limit("LIMIT", value)
