"""Serializable workflow definition models.

For source layers ``layer_id`` is the immutable AtOnce lineage identity. A separate
``binding_id`` stores the current QGIS project layer ID and may change after an
explicit Relink Source operation. Older workflows migrate safely because their
original ``layer_id`` becomes both values on first read.

Issue #22 G4 adds a persisted generic dependency DAG to the workflow definition.
Legacy v0.1 A/B -> C -> GeoPackage/XLSX workflows are deterministically adapted
into that graph on read, while the proven v0.1 fields remain intact for the
existing execution engine. Per-run edge selection is intentionally not persisted
here; only saved edge defaults belong to workflow configuration.

Legacy-adapted graphs retain an explicit origin marker. Their serialized graph is
useful for durable project inspection, but ``effective_dependency_graph`` always
regenerates it from the current v0.1 fields. This prevents Relink/Replace Source,
workbook relink, or derived replacement from leaving stale graph identities.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .dependency_graph import (
    DependencyEdge,
    DependencyGraph,
    DependencyNode,
    NodeKind,
    OperationKind,
)


SOURCE_ROLE = "source"
DERIVED_ROLE = "derived"
SAME_BUSINESS_KEY = "same_business_key"
WORKFLOW_SCHEMA_VERSION = 5
LEGACY_GRAPH_ORIGIN = "legacy_v0.1"
EXPLICIT_GRAPH_ORIGIN = "explicit"
FREEFORM_GRAPH_ORIGIN = "freeform_explicit"


@dataclass
class LayerRef:
    """Serializable reference to a QGIS layer or planned derived layer."""

    layer_id: Optional[str]
    name: str
    role: str
    source_uri: Optional[str] = None
    provider: Optional[str] = None
    binding_id: Optional[str] = None

    def __post_init__(self):
        if self.role == SOURCE_ROLE and self.binding_id is None:
            self.binding_id = self.layer_id

    @property
    def stable_id(self) -> str:
        """Immutable AtOnce source identity (normal layer ID for non-sources)."""

        return str(self.layer_id or "")

    @property
    def current_layer_id(self) -> str:
        """Current QGIS project binding used to resolve the actual layer object."""

        if self.role == SOURCE_ROLE:
            return str(self.binding_id or self.layer_id or "")
        return str(self.layer_id or "")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "layer_id": self.layer_id,
            "name": self.name,
            "role": self.role,
            "source_uri": self.source_uri,
            "provider": self.provider,
            "binding_id": self.binding_id,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "LayerRef":
        role = payload.get("role", "")
        old_layer_id = payload.get("layer_id")
        stable_id = payload.get("lineage_id") or old_layer_id
        binding_id = payload.get("binding_id") or old_layer_id
        if role != SOURCE_ROLE:
            stable_id = old_layer_id
            binding_id = None
        return cls(
            layer_id=stable_id,
            name=payload.get("name", ""),
            role=role,
            source_uri=payload.get("source_uri"),
            provider=payload.get("provider"),
            binding_id=binding_id,
        )


@dataclass
class FieldMapping:
    """User-confirmed relationship between business-key fields on two sources."""

    left_layer_id: str
    left_field: str
    right_layer_id: str
    right_field: str
    relationship_type: str = SAME_BUSINESS_KEY
    confirmed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "left_layer_id": self.left_layer_id,
            "left_field": self.left_field,
            "right_layer_id": self.right_layer_id,
            "right_field": self.right_field,
            "relationship_type": self.relationship_type,
            "confirmed": self.confirmed,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "FieldMapping":
        return cls(
            left_layer_id=payload.get("left_layer_id", ""),
            left_field=payload.get("left_field", ""),
            right_layer_id=payload.get("right_layer_id", ""),
            right_field=payload.get("right_field", ""),
            relationship_type=payload.get("relationship_type", SAME_BUSINESS_KEY),
            confirmed=bool(payload.get("confirmed", False)),
        )


@dataclass
class ExportRef:
    """Configuration for one linked XLSX export."""

    export_id: str
    name: str
    path: str
    filter_expression: str = ""
    source_layer_id: Optional[str] = None
    relink_revision_number: Optional[int] = None
    relink_sha256: str = ""
    relink_modified: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "export_id": self.export_id,
            "name": self.name,
            "path": self.path,
            "filter_expression": self.filter_expression,
            "source_layer_id": self.source_layer_id,
            "relink_revision_number": self.relink_revision_number,
            "relink_sha256": self.relink_sha256,
            "relink_modified": self.relink_modified,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ExportRef":
        revision = payload.get("relink_revision_number")
        return cls(
            export_id=payload.get("export_id", ""),
            name=payload.get("name", ""),
            path=payload.get("path", ""),
            filter_expression=payload.get("filter_expression", ""),
            source_layer_id=payload.get("source_layer_id"),
            relink_revision_number=int(revision) if revision not in (None, "") else None,
            relink_sha256=payload.get("relink_sha256", ""),
            relink_modified=bool(payload.get("relink_modified", False)),
        )


@dataclass
class DeliveryRef:
    """Forward-only delivery configuration; never a reverse-sync workbook."""

    delivery_id: str
    name: str
    format: str
    path: str
    enabled_by_default: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "delivery_id": self.delivery_id,
            "name": self.name,
            "format": self.format,
            "path": self.path,
            "enabled_by_default": self.enabled_by_default,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "DeliveryRef":
        return cls(
            delivery_id=str(payload.get("delivery_id", "")),
            name=str(payload.get("name", "")),
            format=str(payload.get("format", "")),
            path=str(payload.get("path", "")),
            enabled_by_default=bool(payload.get("enabled_by_default", True)),
        )


@dataclass
class WorkflowDefinition:
    """Persisted AtOnce workflow configuration."""

    workflow_id: str
    name: str
    schema_version: int = WORKFLOW_SCHEMA_VERSION
    layers: List[LayerRef] = field(default_factory=list)
    gpkg_path: str = ""
    exports: List[ExportRef] = field(default_factory=list)
    forward_deliveries: List[DeliveryRef] = field(default_factory=list)
    field_mappings: List[FieldMapping] = field(default_factory=list)
    lineage_generation: int = 1
    dependency_graph: Optional[DependencyGraph] = None
    dependency_graph_origin: str = ""

    @property
    def source_layers(self) -> List[LayerRef]:
        return [layer for layer in self.layers if layer.role == SOURCE_ROLE]

    @property
    def derived_layer(self) -> Optional[LayerRef]:
        return next((layer for layer in self.layers if layer.role == DERIVED_ROLE), None)

    @property
    def primary_field_mapping(self) -> Optional[FieldMapping]:
        return self.field_mappings[0] if self.field_mappings else None

    def source_by_lineage_id(self, lineage_id: str) -> Optional[LayerRef]:
        wanted = str(lineage_id or "")
        return next((item for item in self.source_layers if item.stable_id == wanted), None)

    @property
    def source_binding_map(self) -> Dict[str, str]:
        return {
            item.stable_id: item.current_layer_id
            for item in self.source_layers
            if item.stable_id and item.current_layer_id
        }

    @staticmethod
    def _legacy_source_node_id(source: LayerRef, index: int) -> str:
        token = source.stable_id or f"index-{index + 1}"
        return f"source:{token}"

    @staticmethod
    def _legacy_export_node_id(export: ExportRef, index: int) -> str:
        token = export.export_id or f"index-{index + 1}"
        return f"delivery:xlsx:{token}"

    @staticmethod
    def _legacy_forward_delivery_node_id(delivery: DeliveryRef, index: int) -> str:
        token = delivery.delivery_id or f"index-{index + 1}"
        return f"delivery:forward:{token}"

    def build_legacy_dependency_graph(self) -> DependencyGraph:
        """Adapt the proven v0.1 workflow into deterministic generic DAG IDs.

        This adapter does not change execution semantics. All v0.1 edges default
        to enabled because the existing executor refreshes the complete registered
        downstream set. Later milestones may let users edit these saved defaults.
        """

        nodes: List[DependencyNode] = []
        edges: List[DependencyEdge] = []
        source_node_ids: List[str] = []

        for index, source in enumerate(self.source_layers):
            node_id = self._legacy_source_node_id(source, index)
            source_node_ids.append(node_id)
            nodes.append(
                DependencyNode(
                    node_id=node_id,
                    name=source.name or f"Source {index + 1}",
                    kind=NodeKind.SOURCE,
                    metadata={
                        "workflow_role": SOURCE_ROLE,
                        "source_lineage_id": source.stable_id,
                    },
                )
            )

        derived = self.derived_layer
        derived_node_id = "derived:primary"
        nodes.append(
            DependencyNode(
                node_id=derived_node_id,
                name=(derived.name if derived and derived.name else "Layer C"),
                kind=NodeKind.DERIVED,
                metadata={
                    "workflow_role": DERIVED_ROLE,
                    "layer_id": derived.layer_id if derived else None,
                },
            )
        )

        source_operation = (
            OperationKind.MERGE if len(source_node_ids) > 1 else OperationKind.PASSTHROUGH
        )
        for source_node_id in source_node_ids:
            edges.append(
                DependencyEdge(
                    edge_id=f"edge:{source_node_id}->{derived_node_id}",
                    from_node=source_node_id,
                    to_node=derived_node_id,
                    operation=source_operation,
                    enabled_by_default=True,
                    parameters={"adapter": "v0.1"},
                )
            )

        gpkg_node_id = "delivery:gpkg"
        nodes.append(
            DependencyNode(
                node_id=gpkg_node_id,
                name="GeoPackage",
                kind=NodeKind.DELIVERY,
                format="gpkg",
                metadata={
                    "workflow_role": "gpkg",
                    "path": self.gpkg_path,
                },
            )
        )
        edges.append(
            DependencyEdge(
                edge_id=f"edge:{derived_node_id}->{gpkg_node_id}",
                from_node=derived_node_id,
                to_node=gpkg_node_id,
                operation=OperationKind.EXPORT,
                enabled_by_default=True,
                parameters={"adapter": "v0.1"},
            )
        )

        for index, export in enumerate(self.exports):
            export_node_id = self._legacy_export_node_id(export, index)
            nodes.append(
                DependencyNode(
                    node_id=export_node_id,
                    name=export.name or "XLSX",
                    kind=NodeKind.DELIVERY,
                    format="xlsx",
                    metadata={
                        "workflow_role": "xlsx_export",
                        "export_id": export.export_id,
                        "path": export.path,
                    },
                )
            )
            edges.append(
                DependencyEdge(
                    edge_id=f"edge:{derived_node_id}->{export_node_id}",
                    from_node=derived_node_id,
                    to_node=export_node_id,
                    operation=OperationKind.EXPORT,
                    enabled_by_default=True,
                    parameters={
                        "adapter": "v0.1",
                        "filter_expression": export.filter_expression,
                    },
                )
            )

        for index, delivery in enumerate(self.forward_deliveries):
            node_id = self._legacy_forward_delivery_node_id(delivery, index)
            nodes.append(
                DependencyNode(
                    node_id=node_id,
                    name=delivery.name or "GeoJSON",
                    kind=NodeKind.DELIVERY,
                    format=delivery.format,
                    metadata={
                        "workflow_role": "forward_delivery",
                        "delivery_id": delivery.delivery_id,
                        "path": delivery.path,
                        "format": delivery.format,
                    },
                )
            )
            edges.append(
                DependencyEdge(
                    edge_id=f"edge:{derived_node_id}->{node_id}",
                    from_node=derived_node_id,
                    to_node=node_id,
                    operation=OperationKind.EXPORT,
                    enabled_by_default=delivery.enabled_by_default,
                    parameters={"adapter": "v0.1"},
                )
            )

        return DependencyGraph(nodes=nodes, edges=edges)

    def effective_dependency_graph_origin(self) -> str:
        if self.dependency_graph_origin:
            return self.dependency_graph_origin
        if self.dependency_graph is not None:
            return EXPLICIT_GRAPH_ORIGIN
        return LEGACY_GRAPH_ORIGIN

    def effective_dependency_graph(self) -> DependencyGraph:
        """Return the current graph without letting legacy adapters go stale."""

        if self.effective_dependency_graph_origin() == LEGACY_GRAPH_ORIGIN:
            return self.build_legacy_dependency_graph()
        return self.dependency_graph or DependencyGraph()

    def to_dict(self) -> Dict[str, Any]:
        graph_origin = self.effective_dependency_graph_origin()
        graph = self.effective_dependency_graph()
        return {
            "workflow_id": self.workflow_id,
            "name": self.name,
            "schema_version": max(self.schema_version, WORKFLOW_SCHEMA_VERSION),
            "layers": [layer.to_dict() for layer in self.layers],
            "gpkg_path": self.gpkg_path,
            "exports": [export.to_dict() for export in self.exports],
            "forward_deliveries": [item.to_dict() for item in self.forward_deliveries],
            "field_mappings": [mapping.to_dict() for mapping in self.field_mappings],
            "lineage_generation": self.lineage_generation,
            "dependency_graph_origin": graph_origin,
            "dependency_graph": graph.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "WorkflowDefinition":
        graph_payload = payload.get("dependency_graph")
        graph = (
            DependencyGraph.from_dict(graph_payload)
            if isinstance(graph_payload, dict)
            else None
        )
        graph_origin = str(payload.get("dependency_graph_origin") or "")
        if not graph_origin:
            graph_origin = EXPLICIT_GRAPH_ORIGIN if graph is not None else LEGACY_GRAPH_ORIGIN

        workflow = cls(
            workflow_id=payload.get("workflow_id", ""),
            name=payload.get("name", ""),
            schema_version=max(
                int(payload.get("schema_version", 1)),
                WORKFLOW_SCHEMA_VERSION,
            ),
            layers=[LayerRef.from_dict(item) for item in payload.get("layers", [])],
            gpkg_path=payload.get("gpkg_path", ""),
            exports=[ExportRef.from_dict(item) for item in payload.get("exports", [])],
            forward_deliveries=[
                DeliveryRef.from_dict(item)
                for item in payload.get("forward_deliveries", [])
                if isinstance(item, dict)
            ],
            field_mappings=[FieldMapping.from_dict(item) for item in payload.get("field_mappings", [])],
            lineage_generation=max(1, int(payload.get("lineage_generation", 1))),
            dependency_graph=graph,
            dependency_graph_origin=graph_origin,
        )
        if workflow.dependency_graph is None:
            workflow.dependency_graph = workflow.build_legacy_dependency_graph()
        return workflow
