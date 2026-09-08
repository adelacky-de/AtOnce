"""Adapters for separating stable AtOnce source identity from QGIS bindings.

The QGIS provider object remains authoritative for geometry/attributes, but derived
lineage must use the immutable workflow source lineage ID rather than the ephemeral
QGIS project layer ID assigned on each add/re-add.
"""


class StableSourceLayerView:
    """Delegate a QGIS layer while exposing a stable AtOnce ``id()``.

    ``QgisGateway.build_derived_memory_layer`` only needs normal layer methods plus
    ``id()``. Wrapping source layers at this boundary lets existing propagation
    logic write `_atonce_source_layer` from the stable lineage identity without
    changing provider behavior or mutating the source layer.
    """

    def __init__(self, layer, lineage_id: str):
        self._layer = layer
        self._lineage_id = str(lineage_id or "")
        if not self._lineage_id:
            raise ValueError("Stable source lineage ID is required.")

    def id(self):
        return self._lineage_id

    @property
    def bound_layer(self):
        return self._layer

    def __getattr__(self, name):
        return getattr(self._layer, name)
