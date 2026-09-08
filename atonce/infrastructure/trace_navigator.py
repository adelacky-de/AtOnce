"""Read-only QGIS navigation from an authoritative detected change to its source feature."""

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from qgis.core import (
    QgsExpression,
    QgsFeatureRequest,
    QgsMapLayerType,
    QgsProject,
)

from ..core.lineage import (
    LINEAGE_FIELD_SOURCE_KEY,
    canonical_feature_ancestors,
    feature_ancestors,
)
from ..core.trace_planning import TraceTarget


class TraceNavigationError(RuntimeError):
    """Raised when immutable lineage cannot resolve one safe QGIS navigation target."""


@dataclass(frozen=True)
class TraceNavigationResult:
    layer_id: str
    layer_name: str
    feature_id: int
    source_key: str
    zoomed: bool
    attribute_table_opened: bool
    warning: Optional[str] = None


class QgisTraceNavigator:
    """Select/zoom/open attribute context without changing source data."""

    def __init__(self, iface, project: Optional[QgsProject] = None):
        self.iface = iface
        self.project = project or QgsProject.instance()

    def trace(self, target: TraceTarget) -> TraceNavigationResult:
        layer = self.project.mapLayer(target.source_layer_id)
        if layer is None or layer.type() != QgsMapLayerType.VectorLayer:
            raise TraceNavigationError(
                f"Source layer '{target.source_layer_name}' is missing or is not a vector layer."
            )

        key_index = layer.fields().indexOf(LINEAGE_FIELD_SOURCE_KEY)
        if key_index < 0:
            raise TraceNavigationError(
                f"Source layer '{layer.name()}' no longer contains {LINEAGE_FIELD_SOURCE_KEY}."
            )

        quoted = QgsExpression.quotedString(target.source_feature_key)
        filter_expression = f'"{LINEAGE_FIELD_SOURCE_KEY}" = {quoted}'
        request = QgsFeatureRequest().setFilterExpression(filter_expression)
        matches = list(layer.getFeatures(request))
        if len(matches) == 0:
            raise TraceNavigationError(
                f"No source feature matches immutable UUID {target.source_feature_key!r} on '{layer.name()}'."
            )
        if len(matches) != 1:
            raise TraceNavigationError(
                f"Immutable UUID {target.source_feature_key!r} matches {len(matches)} features on '{layer.name()}'; trace is ambiguous."
            )

        feature = matches[0]
        if not self.iface.setActiveLayer(layer):
            raise TraceNavigationError(
                f"QGIS could not activate source layer '{layer.name()}'."
            )

        # QGIS selection is navigation state only. Do not enter an edit session or
        # touch provider/source values in this path.
        layer.removeSelection()
        layer.selectByIds([feature.id()])

        zoomed = False
        if feature.hasGeometry() and feature.geometry() is not None and not feature.geometry().isEmpty():
            self.iface.mapCanvas().zoomToSelected(layer)
            zoomed = True

        opened = False
        warning = None
        try:
            dialog = self.iface.showAttributeTable(layer, filter_expression)
            opened = dialog is not None
            if not opened:
                warning = "Source feature selected, but QGIS did not open the filtered attribute table."
        except Exception as exc:
            warning = f"Source feature selected, but the attribute table could not be opened: {exc}"

        return TraceNavigationResult(
            layer_id=layer.id(),
            layer_name=layer.name(),
            feature_id=int(feature.id()),
            source_key=target.source_feature_key,
            zoomed=zoomed,
            attribute_table_opened=opened,
            warning=warning,
        )

    def trace_ancestors(
        self,
        ancestors: Iterable,
        source_bindings: Optional[Mapping[str, str]] = None,
    ):
        """Navigate every immutable source feature contributing to one result."""

        bindings = dict(source_bindings or {})
        results = []
        for ancestor in canonical_feature_ancestors(ancestors):
            results.append(
                self.trace(
                    TraceTarget(
                        source_layer_id=bindings.get(
                            ancestor.source_layer_id, ancestor.source_layer_id
                        ),
                        source_feature_key=ancestor.source_feature_key,
                        source_layer_name=ancestor.source_layer_id,
                        field_name="",
                        source_field_name="",
                    )
                )
            )
        return tuple(results)

    def trace_feature(
        self,
        layer,
        feature,
        source_bindings: Optional[Mapping[str, str]] = None,
    ):
        """Consume a materialized feature's durable single/multi-parent lineage."""

        values = {
            name: feature[name]
            for name in feature.fields().names()
        }
        ancestors = feature_ancestors(
            values,
            str(layer.id()),
            str(values.get(LINEAGE_FIELD_SOURCE_KEY) or ""),
        )
        if not ancestors:
            raise TraceNavigationError("Feature is missing immutable AtOnce lineage.")
        return self.trace_ancestors(ancestors, source_bindings)
