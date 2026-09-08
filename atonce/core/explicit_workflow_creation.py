"""QGIS-free construction of standalone explicit workflow definitions."""

from typing import Iterable, Optional, Sequence
from uuid import uuid4

from ..models.dependency_graph import (
    DependencyEdge,
    DependencyGraph,
    DependencyNode,
    NodeKind,
    OperationKind,
)
from ..models.workflow import (
    EXPLICIT_GRAPH_ORIGIN,
    SOURCE_ROLE,
    DeliveryRef,
    LayerRef,
    WorkflowDefinition,
)


def _copy_source(source: LayerRef) -> LayerRef:
    return LayerRef.from_dict(source.to_dict())


def _copy_deliveries(deliveries: Iterable[DeliveryRef]) -> list:
    return [DeliveryRef.from_dict(item.to_dict()) for item in (deliveries or [])]


def _delivery_nodes_and_edges(derived_node_id, deliveries):
    nodes = []
    edges = []
    for delivery in deliveries:
        node_id = f"delivery:forward:{delivery.delivery_id}"
        nodes.append(
            DependencyNode(
                node_id,
                delivery.name,
                NodeKind.DELIVERY,
                format=delivery.format,
                metadata={
                    "delivery_id": delivery.delivery_id,
                    "path": delivery.path,
                    "format": delivery.format,
                },
            )
        )
        edges.append(
            DependencyEdge(
                f"edge:{derived_node_id}->{node_id}",
                derived_node_id,
                node_id,
                OperationKind.EXPORT,
                enabled_by_default=delivery.enabled_by_default,
            )
        )
    return nodes, edges


def build_standalone_filter_workflow(
    *,
    name: str,
    source: LayerRef,
    derived_name: str,
    expression: str,
    deliveries: Sequence[DeliveryRef] = (),
    workflow_id: Optional[str] = None,
    derived_node_id: Optional[str] = None,
) -> WorkflowDefinition:
    """Build one normal explicit SOURCE → FILTER → DERIVED graph."""

    if source.role != SOURCE_ROLE or not source.stable_id or not source.current_layer_id:
        raise ValueError("A standalone FILTER requires one bound source with immutable lineage.")
    source_node_id = f"source:{source.stable_id}"
    derived_node_id = derived_node_id or f"derived:filter:{uuid4()}"
    configured_deliveries = _copy_deliveries(deliveries)
    nodes = [
        DependencyNode(
            source_node_id,
            source.name,
            NodeKind.SOURCE,
            metadata={
                "workflow_role": SOURCE_ROLE,
                "source_lineage_id": source.stable_id,
            },
        ),
        DependencyNode(derived_node_id, derived_name.strip(), NodeKind.DERIVED),
    ]
    edges = [
        DependencyEdge(
            f"edge:{source_node_id}->{derived_node_id}",
            source_node_id,
            derived_node_id,
            OperationKind.FILTER,
            parameters={"expression": str(expression or "").strip()},
        )
    ]
    delivery_nodes, delivery_edges = _delivery_nodes_and_edges(
        derived_node_id, configured_deliveries
    )
    nodes.extend(delivery_nodes)
    edges.extend(delivery_edges)
    return WorkflowDefinition(
        workflow_id=workflow_id or str(uuid4()),
        name=str(name or "").strip(),
        layers=[_copy_source(source)],
        forward_deliveries=configured_deliveries,
        dependency_graph=DependencyGraph(nodes, edges),
        dependency_graph_origin=EXPLICIT_GRAPH_ORIGIN,
    )


def build_standalone_merge_workflow(
    *,
    name: str,
    sources: Sequence[LayerRef],
    derived_name: str,
    deliveries: Sequence[DeliveryRef] = (),
    workflow_id: Optional[str] = None,
    derived_node_id: Optional[str] = None,
) -> WorkflowDefinition:
    """Build one normal explicit two-parent SOURCE → MERGE → DERIVED graph."""

    if len(sources) != 2:
        raise ValueError("A standalone MERGE requires exactly two sources.")
    if any(
        source.role != SOURCE_ROLE
        or not source.stable_id
        or not source.current_layer_id
        for source in sources
    ):
        raise ValueError("A standalone MERGE requires two bound sources with immutable lineage.")
    if len({source.current_layer_id for source in sources}) != 2:
        raise ValueError("Standalone MERGE sources must be two distinct loaded layers.")
    if len({source.stable_id for source in sources}) != 2:
        raise ValueError("Standalone MERGE sources must have distinct immutable lineage IDs.")

    derived_node_id = derived_node_id or f"derived:merge:{uuid4()}"
    configured_deliveries = _copy_deliveries(deliveries)
    nodes = [
        DependencyNode(
            f"source:{source.stable_id}",
            source.name,
            NodeKind.SOURCE,
            metadata={
                "workflow_role": SOURCE_ROLE,
                "source_lineage_id": source.stable_id,
            },
        )
        for source in sources
    ]
    nodes.append(DependencyNode(derived_node_id, derived_name.strip(), NodeKind.DERIVED))
    edges = [
        DependencyEdge(
            f"edge:source:{source.stable_id}->{derived_node_id}",
            f"source:{source.stable_id}",
            derived_node_id,
            OperationKind.MERGE,
        )
        for source in sources
    ]
    delivery_nodes, delivery_edges = _delivery_nodes_and_edges(
        derived_node_id, configured_deliveries
    )
    nodes.extend(delivery_nodes)
    edges.extend(delivery_edges)
    return WorkflowDefinition(
        workflow_id=workflow_id or str(uuid4()),
        name=str(name or "").strip(),
        layers=[_copy_source(source) for source in sources],
        forward_deliveries=configured_deliveries,
        dependency_graph=DependencyGraph(nodes, edges),
        dependency_graph_origin=EXPLICIT_GRAPH_ORIGIN,
    )
