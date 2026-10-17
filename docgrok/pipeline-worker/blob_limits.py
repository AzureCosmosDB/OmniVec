class BlobSizeLimitExceeded(ValueError):
    def __init__(self, size_bytes: int, limit_bytes: int, budget_name: str):
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes
        self.budget_name = budget_name
        super().__init__(
            f"Blob size {size_bytes} bytes exceeds {budget_name} limit {limit_bytes} bytes"
        )


def require_positive_byte_limit(name: str, value: str) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if limit <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return limit


def enforce_blob_size(blob_client, limit_bytes: int, budget_name: str) -> int:
    properties = blob_client.get_blob_properties()
    size_bytes = int(properties.size)
    if size_bytes > limit_bytes:
        raise BlobSizeLimitExceeded(size_bytes, limit_bytes, budget_name)
    return size_bytes
