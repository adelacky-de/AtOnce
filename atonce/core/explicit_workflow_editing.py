"""QGIS-free identity-preserving edits for existing explicit workflows."""

from typing import Mapping, Optional, Sequence

from ..models.dependency_graph import (
    DependencyEdge,
    DependencyGraph,
    DependencyNode,
    NodeKind,
    OperationKind,
)
from ..models.workflow import EXPLICIT_GRAPH_ORIGIN, DeliveryRef, WorkflowDefinition


def apply_explicit_workflow_edits(
    workflow: WorkflowDefinition,
    *,
    name: Optional[str] = None,
    filter_expressions: Optional[Mapping[str, str]] = None,
    deliveries: Optional[Sequence[DeliveryRef]] = None,
) -> WorkflowDefinition:
    """Clone an explicit workflow while preserving every graph identity."""

    if workflow.effective_dependency_graph_origin() != EXPLICIT_GRAPH_ORIGIN:
        raise ValueError("Only explicit FILTER, MERGE, and FILTER-chain workflows are editable here.")

    candidate = WorkflowDefinition.from_dict(workflow.to_dict())
    if name is not None:
        candidate.name = str(name).strip()

    expression_updates = {
        str(edge_id): str(expression).strip()
        for edge_id, expression in (filter_expressions or {}).items()
    }
    configured_deliveries = (
        [DeliveryRef.from_dict(item.to_dict()) for item in deliveries]
        if deliveries is not None
        else [DeliveryRef.from_dict(item.to_dict()) for item in candidate.forward_deliveries]
    )
    delivery_by_id = {
        delivery.delivery_id: delivery for delivery in configured_deliveries
    }

    graph = candidate.effective_dependency_graph()
    updated_nodes = []
    for node in graph.nodes:
        delivery_id = str(node.metadata.get("delivery_id") or "")
        delivery = delivery_by_id.get(delivery_id)
        if node.kind != NodeKind.DELIVERY or delivery is None:
            updated_nodes.append(node)
            continue
        metadata = dict(node.metadata)
        metadata.update(
            {
                "delivery_id": delivery.delivery_id,
                "path": delivery.path,
                "format": delivery.format,
            }
        )
        updated_nodes.append(
            DependencyNode(
                node_id=node.node_id,
                name=delivery.name,
                kind=node.kind,
                format=delivery.format,
                current_revision=node.current_revision,
                metadata=metadata,
            )
        )

    updated_edges = []
    node_by_id = {node.node_id: node for node in updated_nodes}
    for edge in graph.edges:
        parameters = dict(edge.parameters)
        if edge.edge_id in expression_updates:
            parameters["expression"] = expression_updates[edge.edge_id]
        enabled_by_default = edge.enabled_by_default
        target = node_by_id.get(edge.to_node)
        delivery_id = str(target.metadata.get("delivery_id") or "") if target else ""
        delivery = delivery_by_id.get(delivery_id)
        if delivery is not None and edge.operation == OperationKind.EXPORT:
            enabled_by_default = delivery.enabled_by_default
        updated_edges.append(
            DependencyEdge(
                edge_id=edge.edge_id,
                from_node=edge.from_node,
                to_node=edge.to_node,
                operation=edge.operation,
                enabled_by_default=enabled_by_default,
                parameters=parameters,
            )
        )

    candidate.forward_deliveries = configured_deliveries
    candidate.dependency_graph = DependencyGraph(updated_nodes, updated_edges)
    candidate.dependency_graph_origin = EXPLICIT_GRAPH_ORIGIN
    return candidate
