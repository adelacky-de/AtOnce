"""Construction helpers for a persisted free-form explicit workflow."""

from typing import Iterable, Optional
from uuid import uuid4

from ..models.dependency_graph import DependencyGraph, NodeKind
from ..models.workflow import (
    DeliveryRef,
    EXPLICIT_GRAPH_ORIGIN,
    FREEFORM_GRAPH_ORIGIN,
    LayerRef,
    SOURCE_ROLE,
    WorkflowDefinition,
)


def delivery_refs_from_graph(graph: DependencyGraph):
    refs = []
    for node in graph.nodes:
        if node.kind != NodeKind.DELIVERY:
            continue
        incoming = graph.incoming_edges(node.node_id)
        refs.append(
            DeliveryRef(
                delivery_id=str(node.metadata.get("delivery_id") or node.node_id),
                name=node.name,
                format=node.format or str(node.metadata.get("format") or ""),
                path=str(node.metadata.get("path") or ""),
                enabled_by_default=bool(
                    node.metadata.get(
                        "include_in_changes",
                        incoming[0].enabled_by_default if incoming else True,
                    )
                ),
            )
        )
    return refs


def build_freeform_workflow(
    *,
    name: str,
    sources: Iterable[LayerRef],
    graph: DependencyGraph,
    workflow_id: Optional[str] = None,
) -> WorkflowDefinition:
    """Wrap a graph draft in the normal project workflow persistence model."""

    configured_sources = [LayerRef.from_dict(item.to_dict()) for item in sources]
    if any(item.role != SOURCE_ROLE for item in configured_sources):
        raise ValueError("Free-form workflow sources must use SOURCE_ROLE.")
    return WorkflowDefinition(
        workflow_id=workflow_id or str(uuid4()),
        name=str(name or "Untitled workflow").strip(),
        layers=configured_sources,
        forward_deliveries=delivery_refs_from_graph(graph),
        dependency_graph=graph,
        dependency_graph_origin=FREEFORM_GRAPH_ORIGIN,
    )


def is_freeform_workflow(workflow) -> bool:
    return workflow is not None and workflow.effective_dependency_graph_origin() == FREEFORM_GRAPH_ORIGIN
