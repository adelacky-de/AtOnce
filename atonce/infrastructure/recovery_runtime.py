"""Small runtime checks used by explicit recovery flows."""

import hashlib
from pathlib import Path


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_source_write_capability(layer) -> None:
    """Require the v0.1 source contract without importing QGIS at module import."""

    from qgis.core import QgsVectorDataProvider

    if layer is None:
        raise ValueError("A loaded source candidate is required.")
    is_read_only = getattr(layer, "isReadOnly", None)
    if callable(is_read_only) and is_read_only():
        raise ValueError(f"Source layer '{layer.name()}' is read-only.")
    provider = layer.dataProvider()
    capabilities = provider.capabilities()
    if not (capabilities & QgsVectorDataProvider.ChangeAttributeValues):
        raise ValueError(
            f"Source layer '{layer.name()}' cannot update attributes required for v0.1 write-back."
        )
