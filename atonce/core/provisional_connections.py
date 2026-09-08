"""QGIS-free provisional wiring for drag-first operation authoring.

An unconfigured OPERATION needs to accept connections before its function is
chosen so structured editors can inspect upstream schemas. Provisional edges
are authoring-only: once a function is selected they are rebound to the
registry-declared semantic input ports before normal validation/execution.
"""

from dataclasses import replace
from uuid import uuid4

from .graph_editing import GraphEditError
from .operation_registry import DEFAULT_OPERATION_REGISTRY
from ..models.dependency_graph import (
    DependencyEdge,
    DependencyGraph,
    NodeKind,
    OperationKind,
    connection_error,
)


PROVISIONAL_EDGE_FLAG = "provisional_operation_input"


def connect_provisional_operation_input(
    graph: DependencyGraph,
    from_node_id: str,
    to_node_id: str,
) -> DependencyGraph:
    """Connect to an unconfigured operation through an authoring-only input.

    Do not route this through a concrete operation validator. Before a function
    is selected there is no valid FILTER/JOIN/etc. contract yet, so provisional
    edges intentionally use CUSTOM until ``bind_provisional_inputs`` assigns the
    selected operation and its semantic input ports.
    """

    node_map = graph.node_map()
    source = node_map.get(str(from_node_id))
    target = node_map.get(str(to_node_id))
    if source is None:
        raise GraphEditError(f"Connection source node {from_node_id!r} does not exist.")
    if target is None or target.kind != NodeKind.DERIVED:
        raise GraphEditError("A provisional input can only target an operation block.")
    if str(target.metadata.get("operation_kind") or ""):
        raise GraphEditError("The operation is already configured; use its named input port.")

    error = connection_error(source.kind, target.kind, OperationKind.CUSTOM)
    if error:
        raise GraphEditError(error)
    if any(
        edge.from_node == source.node_id and edge.to_node == target.node_id
        for edge in graph.edges
    ):
        raise GraphEditError("That connection already exists.")

    edge = DependencyEdge(
        edge_id=f"edge:{from_node_id}->{to_node_id}:{uuid4()}",
        from_node=str(from_node_id),
        to_node=str(to_node_id),
        operation=OperationKind.CUSTOM,
        enabled_by_default=True,
        parameters={
            PROVISIONAL_EDGE_FLAG: True,
            "target_port": "input",
        },
    )
    candidate = DependencyGraph(list(graph.nodes), [*graph.edges, edge])
    # Keep generic graph invariants (including DAG/cycle safety) while skipping
    # concrete operation-port cardinality until the function is known.
    errors = candidate.validate(require_complete=False)
    if errors:
        raise GraphEditError("Invalid provisional graph connection: " + " | ".join(errors))
    return candidate


def provisional_port_ids(definition, count):
    """Return deterministic semantic ports for ``count`` provisional inputs."""

    slots = []
    for port in definition.input_ports:
        if port.max_count is None:
            while len(slots) < count:
                slots.append(port.port_id)
            break
        slots.extend([port.port_id] * int(port.max_count))
        if len(slots) >= count:
            break
    return tuple(slots[:count])


def provisional_port_assignment(graph, operation_node_id, definition):
    """Preview provisional edge→port mapping without mutating the graph."""

    provisional = [
        edge
        for edge in graph.incoming_edges(str(operation_node_id))
        if bool((edge.parameters or {}).get(PROVISIONAL_EDGE_FLAG, False))
    ]
    provisional.sort(key=lambda edge: str(edge.edge_id))
    ports = provisional_port_ids(definition, len(provisional))
    return {
        edge.edge_id: ports[index]
        for index, edge in enumerate(provisional)
        if index < len(ports)
    }


def bind_provisional_inputs(
    graph: DependencyGraph,
    operation_node_id: str,
    operation_kind,
) -> DependencyGraph:
    """Rebind provisional incoming edges to registry-declared semantic ports."""

    definition = DEFAULT_OPERATION_REGISTRY.get(operation_kind)
    if definition is None:
        raise GraphEditError(f"Unsupported operation {operation_kind!r}.")

    incoming = list(graph.incoming_edges(str(operation_node_id)))
    provisional = [
        edge
        for edge in incoming
        if bool((edge.parameters or {}).get(PROVISIONAL_EDGE_FLAG, False))
    ]
    if not provisional:
        return graph

    provisional.sort(key=lambda edge: str(edge.edge_id))
    ports = provisional_port_ids(definition, len(provisional))
    if len(ports) < len(provisional):
        raise GraphEditError(
            f"{definition.title} accepts fewer inputs than are already connected. "
            "Disconnect extra provisional inputs before applying this function."
        )

    port_by_edge = {
        edge.edge_id: ports[index] for index, edge in enumerate(provisional)
    }
    rebound = []
    for edge in graph.edges:
        port_id = port_by_edge.get(edge.edge_id)
        if port_id is None:
            rebound.append(edge)
            continue
        parameters = dict(edge.parameters or {})
        parameters.pop(PROVISIONAL_EDGE_FLAG, None)
        parameters["target_port"] = port_id
        rebound.append(
            replace(
                edge,
                operation=OperationKind(definition.kind),
                parameters=parameters,
            )
        )
    return DependencyGraph(list(graph.nodes), rebound)
