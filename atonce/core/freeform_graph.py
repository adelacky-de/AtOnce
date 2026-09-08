"""QGIS-free construction and planning helpers for the free-form DAG."""

from dataclasses import dataclass, field, replace
from typing import Iterable, Mapping, Optional, Tuple
from uuid import uuid4

from ..models.dependency_graph import (
    DependencyEdge,
    DependencyGraph,
    DependencyNode,
    NodeKind,
    OperationKind,
    PropagationPlan,
    plan_propagation,
)
from .graph_editing import GraphEditError, connect_nodes
from .operation_registry import DEFAULT_OPERATION_REGISTRY


FREEFORM_GRAPH_VERSION = 1


class FreeformGraphError(ValueError):
    """Raised when an editable free-form graph cannot be planned safely."""


@dataclass(frozen=True)
class FreeformTransformStep:
    node_id: str
    operation: OperationKind
    input_edges: Tuple[DependencyEdge, ...]
    output_edge_ids: Tuple[str, ...]
    parameters: Mapping[str, object]
    input_edges_by_port: Mapping[str, Tuple[DependencyEdge, ...]] = field(
        default_factory=dict
    )


@dataclass(frozen=True)
class FreeformGraphExecutionPlan:
    graph: DependencyGraph
    propagation: PropagationPlan
    ordered_node_ids: Tuple[str, ...]
    transform_steps: Tuple[FreeformTransformStep, ...]
    output_edge_ids: Tuple[str, ...]


def _canvas_metadata(x=80, y=80):
    return {"canvas": {"x": float(x), "y": float(y)}}


def source_node(node_id: Optional[str] = None, *, name="Source", lineage_id="", x=80, y=80):
    metadata = _canvas_metadata(x, y)
    metadata.update({"workflow_role": "source", "source_lineage_id": str(lineage_id or "")})
    return DependencyNode(
        node_id=node_id or f"source:{uuid4()}",
        name=str(name or "Source"),
        kind=NodeKind.SOURCE,
        metadata=metadata,
    )


def operation_node(
    operation,
    node_id: Optional[str] = None,
    *,
    name: Optional[str] = None,
    parameters: Optional[Mapping[str, object]] = None,
    x=300,
    y=80,
):
    definition = DEFAULT_OPERATION_REGISTRY.get(operation)
    if definition is None:
        raise FreeformGraphError(f"Unsupported operation {operation!r}.")
    metadata = _canvas_metadata(x, y)
    metadata.update(
        {
            "workflow_role": "operation",
            "operation_kind": definition.kind.value,
            "operation_version": definition.version,
            "parameters": dict(parameters or {}),
        }
    )
    return DependencyNode(
        node_id=node_id or f"operation:{definition.kind.value}:{uuid4()}",
        name=str(name or definition.title),
        kind=NodeKind.DERIVED,
        metadata=metadata,
    )


def output_node(
    format_name="geojson",
    node_id: Optional[str] = None,
    *,
    name: Optional[str] = None,
    path="",
    delivery_id: Optional[str] = None,
    x=560,
    y=80,
):
    delivery_id = delivery_id or str(uuid4())
    metadata = _canvas_metadata(x, y)
    metadata.update(
        {
            "workflow_role": "output",
            "delivery_id": delivery_id,
            "path": str(path or ""),
            "format": str(format_name or ""),
            "include_in_changes": True,
        }
    )
    return DependencyNode(
        node_id=node_id or f"output:{delivery_id}",
        name=str(name or format_name or "Output"),
        kind=NodeKind.DELIVERY,
        format=str(format_name or ""),
        metadata=metadata,
    )


def empty_graph() -> DependencyGraph:
    return DependencyGraph()


def connect_graph_nodes(
    graph: DependencyGraph,
    from_node_id: str,
    to_node_id: str,
    *,
    target_port: str = "input",
    edge_id: Optional[str] = None,
    enabled_by_default: bool = True,
    parameters: Optional[Mapping[str, object]] = None,
) -> DependencyGraph:
    """Connect two visual blocks using their target operation contract."""

    node_map = graph.node_map()
    target = node_map.get(str(to_node_id))
    if target is None:
        raise GraphEditError(f"Connection target node {to_node_id!r} does not exist.")
    operation = (
        OperationKind.EXPORT
        if target.kind == NodeKind.DELIVERY
        else OperationKind(str(target.metadata.get("operation_kind") or OperationKind.FILTER.value))
    )
    params = dict(parameters or {})
    if target.kind != NodeKind.DELIVERY:
        params.setdefault("target_port", str(target_port or "input"))
    return connect_nodes(
        graph,
        from_node_id,
        to_node_id,
        operation,
        edge_id=edge_id or f"edge:{from_node_id}->{to_node_id}:{uuid4()}",
        enabled_by_default=enabled_by_default,
        parameters=params,
    )


def set_output_inclusion(
    graph: DependencyGraph,
    output_node_id: str,
    included: bool,
) -> DependencyGraph:
    """Persist one output's product state and keep its incoming edge in sync."""

    wanted = str(output_node_id)
    target = graph.node_map().get(wanted)
    if target is None or target.kind != NodeKind.DELIVERY:
        raise FreeformGraphError(f"Output node {wanted!r} does not exist.")
    metadata = dict(target.metadata)
    metadata["include_in_changes"] = bool(included)
    return DependencyGraph(
        [
            replace(node, metadata=metadata) if node.node_id == wanted else node
            for node in graph.nodes
        ],
        [
            replace(edge, enabled_by_default=bool(included))
            if edge.to_node == wanted
            else edge
            for edge in graph.edges
        ],
    )


def _delivery_is_raster(graph: DependencyGraph, delivery: DependencyNode) -> bool:
    """True when OUTPUT is raster by metadata, GeoTIFF format, or upstream SOURCE."""

    if str(delivery.metadata.get("data_type") or "") == "raster":
        return True
    # GeoTIFF is raster-only. KMZ is shared with vectors — detect via SOURCE walk.
    fmt = str(delivery.format or delivery.metadata.get("format") or "").lower()
    if fmt == "geotiff":
        return True
    node_map = graph.node_map()
    seen = set()
    frontier = [delivery.node_id]
    while frontier:
        current = frontier.pop(0)
        if current in seen:
            continue
        seen.add(current)
        node = node_map.get(current)
        if node is None:
            continue
        if node.kind == NodeKind.SOURCE:
            return str(node.metadata.get("data_type") or "vector") == "raster"
        for edge in graph.edges:
            if edge.to_node == current:
                frontier.append(edge.from_node)
    return False


def registered_selected_edges(graph: DependencyGraph):
    """Return Register edges; vector Outputs use include_in_changes as authority.

    Raster Outputs are always selected for Register/Run even when
    include_in_changes is False (they are not part of Changes).
    """

    node_map = graph.node_map()
    selected = set()
    for edge in graph.edges:
        target = node_map.get(edge.to_node)
        enabled = edge.enabled_by_default
        if target is not None and target.kind == NodeKind.DELIVERY:
            if _delivery_is_raster(graph, target):
                enabled = True
            else:
                enabled = bool(target.metadata.get("include_in_changes", enabled))
        if enabled:
            selected.add(edge.edge_id)
    return selected


def _operation_for_node(node: DependencyNode) -> OperationKind:
    raw = node.metadata.get("operation_kind")
    if not raw:
        raise FreeformGraphError(f"Operation node {node.node_id!r} has no operation kind.")
    try:
        return OperationKind(str(raw))
    except ValueError as exc:
        raise FreeformGraphError(
            f"Operation node {node.node_id!r} uses unsupported operation {raw!r}."
        ) from exc


def validate_freeform_graph(graph: DependencyGraph, *, require_complete=True) -> Tuple[str, ...]:
    """Validate common free-form rules and operation contracts."""

    errors = list(graph.validate(require_complete=require_complete))
    node_map = graph.node_map()
    if not graph.nodes:
        if require_complete:
            errors.append("Plan requires at least one source, operation, and output.")
        return tuple(errors)

    source_lineages = [
        str(node.metadata.get("source_lineage_id") or "")
        for node in graph.nodes
        if node.kind == NodeKind.SOURCE
    ]
    non_empty_lineages = [value for value in source_lineages if value]
    if len(non_empty_lineages) != len(set(non_empty_lineages)):
        errors.append("Free-form SOURCE blocks must use unique source lineage identities.")

    for node in graph.nodes:
        if (
            require_complete
            and node.kind == NodeKind.SOURCE
            and not str(node.metadata.get("source_lineage_id") or "")
        ):
            errors.append(f"Source node {node.node_id!r} is missing source_lineage_id.")
        if node.kind != NodeKind.DERIVED:
            continue
        try:
            operation = _operation_for_node(node)
        except FreeformGraphError as exc:
            errors.append(str(exc))
            continue
        if DEFAULT_OPERATION_REGISTRY.get(operation) is None:
            errors.append(f"No operation definition is registered for {operation.value!r}.")
        if require_complete and not graph.incoming_edges(node.node_id):
            errors.append(f"Operation node {node.name!r} has no connected input.")

    # Every output edge is terminal and every operation edge has a target role.
    for edge in graph.edges:
        target = node_map.get(edge.to_node)
        if target is None or target.kind != NodeKind.DERIVED:
            continue
        if not str(edge.parameters.get("target_port") or ""):
            errors.append(f"Edge {edge.edge_id!r} is missing a named target input port.")

    return tuple(dict.fromkeys(errors))


def plan_freeform_graph_execution(
    graph: DependencyGraph,
    changed_node_ids: Iterable[str],
    selected_edge_ids: Optional[Iterable[str]] = None,
) -> FreeformGraphExecutionPlan:
    """Build a deterministic, topology-driven plan without touching QGIS."""

    errors = validate_freeform_graph(graph, require_complete=True)
    if errors:
        raise FreeformGraphError("Invalid free-form workflow: " + " | ".join(errors))
    propagation = plan_propagation(graph, changed_node_ids, selected_edge_ids)
    ordered = tuple(graph.topological_order())
    node_map = graph.node_map()
    steps = []
    output_edge_ids = []
    for node_id in ordered:
        node = node_map[node_id]
        if node.kind == NodeKind.DERIVED:
            operation = _operation_for_node(node)
            definition = DEFAULT_OPERATION_REGISTRY.require(operation)
            incoming_by_port = {port.port_id: [] for port in definition.input_ports}
            unknown_port_edges = []
            for edge in graph.incoming_edges(node_id):
                port_id = str(edge.parameters.get("target_port") or "input")
                if port_id in incoming_by_port:
                    incoming_by_port[port_id].append(edge)
                else:
                    unknown_port_edges.append(edge)
            input_edges_by_port = {
                port_id: tuple(sorted(edges, key=lambda edge: edge.edge_id))
                for port_id, edges in incoming_by_port.items()
            }
            # Keep the compatibility tuple, but make its order semantic: the
            # registry's declared port order, then deterministic unknown edges.
            incoming = tuple(
                edge
                for port in definition.input_ports
                for edge in input_edges_by_port[port.port_id]
            ) + tuple(sorted(unknown_port_edges, key=lambda edge: edge.edge_id))
            outgoing = tuple(
                edge.edge_id
                for edge in sorted(graph.downstream_edges(node_id), key=lambda edge: edge.edge_id)
                if node_map[edge.to_node].kind == NodeKind.DELIVERY
            )
            steps.append(
                FreeformTransformStep(
                    node_id=node_id,
                    operation=operation,
                    input_edges=incoming,
                    input_edges_by_port=input_edges_by_port,
                    output_edge_ids=outgoing,
                    parameters={
                        **definition.parameter_defaults(),
                        **dict(node.metadata.get("parameters") or {}),
                    },
                )
            )
            output_edge_ids.extend(outgoing)
    return FreeformGraphExecutionPlan(
        graph=graph,
        propagation=propagation,
        ordered_node_ids=ordered,
        transform_steps=tuple(steps),
        output_edge_ids=tuple(output_edge_ids),
    )
