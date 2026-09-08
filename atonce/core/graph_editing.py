"""QGIS-free graph editing helpers for the free-form canvas boundary."""

from typing import Any, Optional

from ..models.dependency_graph import (
    DependencyEdge,
    DependencyGraph,
    NodeKind,
    OperationKind,
    connection_error,
)
from .operation_registry import DEFAULT_OPERATION_REGISTRY


class GraphEditError(ValueError):
    """Raised when a proposed canvas connection violates graph policy."""


def can_connect(from_node, to_node, operation: Optional[OperationKind] = None) -> bool:
    """Return whether two node types can be connected by a port."""

    return connection_error(from_node.kind, to_node.kind, operation) is None


def validate_connection(
    graph: DependencyGraph,
    from_node_id: str,
    to_node_id: str,
    operation: OperationKind,
    target_port: Optional[str] = None,
) -> None:
    """Validate one proposed connector before it is added to a graph."""

    node_map = graph.node_map()
    source = node_map.get(str(from_node_id))
    target = node_map.get(str(to_node_id))
    if source is None:
        raise GraphEditError(f"Connection source node {from_node_id!r} does not exist.")
    if target is None:
        raise GraphEditError(f"Connection target node {to_node_id!r} does not exist.")

    error = connection_error(source.kind, target.kind, operation)
    if error:
        raise GraphEditError(error)
    duplicate = [
        edge
        for edge in graph.edges
        if edge.from_node == source.node_id and edge.to_node == target.node_id
    ]
    if duplicate:
        raise GraphEditError("That connection already exists.")
    if target.kind == NodeKind.DELIVERY:
        incoming = [edge for edge in graph.edges if edge.to_node == target.node_id]
        if incoming:
            raise GraphEditError("Each output accepts exactly one operation result.")

    if target.kind == NodeKind.DERIVED:
        declared_kind = target.metadata.get("operation_kind")
        if declared_kind and OperationKind(operation) != OperationKind(declared_kind):
            raise GraphEditError(
                f"{target.name} accepts {OperationKind(declared_kind).value.upper()} "
                f"inputs, not {OperationKind(operation).value.upper()}."
            )
        operation_kind = declared_kind or operation.value
        definition = DEFAULT_OPERATION_REGISTRY.get(operation_kind)
        if definition is not None:
            port_id = str(target_port or "input")
            port = definition.input_port(port_id)
            if port is None:
                raise GraphEditError(
                    f"{definition.title} has no input port named {port_id!r}."
                )
            current = [
                edge
                for edge in graph.edges
                if edge.to_node == target.node_id
                and _target_port_for(edge.parameters) == port_id
            ]
            if port.max_count is not None and len(current) >= port.max_count:
                raise GraphEditError(
                    f"{definition.title} input {port.label!r} accepts at most "
                    f"{port.max_count} connection(s)."
                )


def _target_port_for(parameters: Optional[dict[str, Any]]) -> str:
    return str((parameters or {}).get("target_port") or "input")


def connect_nodes(
    graph: DependencyGraph,
    from_node_id: str,
    to_node_id: str,
    operation: OperationKind,
    *,
    edge_id: str,
    enabled_by_default: bool = True,
    parameters: Optional[dict[str, Any]] = None,
) -> DependencyGraph:
    """Return a copy of ``graph`` with one policy-checked edge added."""

    params = dict(parameters or {})
    validate_connection(
        graph,
        from_node_id,
        to_node_id,
        operation,
        target_port=params.get("target_port"),
    )
    target = graph.node_map()[str(to_node_id)]
    if target.kind == NodeKind.DELIVERY:
        enabled_by_default = bool(
            target.metadata.get("include_in_changes", enabled_by_default)
        )
    edge = DependencyEdge(
        edge_id=str(edge_id),
        from_node=str(from_node_id),
        to_node=str(to_node_id),
        operation=OperationKind(operation),
        enabled_by_default=bool(enabled_by_default),
        parameters=params,
    )
    candidate = DependencyGraph(list(graph.nodes), [*graph.edges, edge])
    errors = candidate.validate(require_complete=False)
    if errors:
        raise GraphEditError("Invalid graph connection: " + " | ".join(errors))
    return candidate


def disconnect_edge(graph: DependencyGraph, edge_id: str) -> DependencyGraph:
    """Return a graph with one connector removed, preserving all nodes."""

    wanted = str(edge_id)
    if not any(edge.edge_id == wanted for edge in graph.edges):
        raise GraphEditError(f"Connection {wanted!r} does not exist.")
    return DependencyGraph(
        list(graph.nodes),
        [edge for edge in graph.edges if edge.edge_id != wanted],
    )


def remove_node(graph: DependencyGraph, node_id: str) -> DependencyGraph:
    """Remove one block and its incident edges without cascading descendants."""

    wanted = str(node_id)
    if wanted not in graph.node_map():
        raise GraphEditError(f"Node {wanted!r} does not exist.")
    return DependencyGraph(
        [node for node in graph.nodes if node.node_id != wanted],
        [
            edge
            for edge in graph.edges
            if edge.from_node != wanted and edge.to_node != wanted
        ],
    )


def move_node(graph: DependencyGraph, node_id: str, x: float, y: float) -> DependencyGraph:
    """Update presentation coordinates without changing semantic identity."""

    from dataclasses import replace

    updated = []
    for node in graph.nodes:
        if node.node_id != str(node_id):
            updated.append(node)
            continue
        metadata = dict(node.metadata)
        metadata["canvas"] = {"x": float(x), "y": float(y)}
        updated.append(replace(node, metadata=metadata))
    if len(updated) == len(graph.nodes) and not any(node.node_id == str(node_id) for node in graph.nodes):
        raise GraphEditError(f"Node {node_id!r} does not exist.")
    return DependencyGraph(updated, list(graph.edges))
