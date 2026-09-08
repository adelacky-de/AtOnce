"""QGIS-free planning and execution identity for explicit graph slices."""

import hashlib
import json
from dataclasses import dataclass
from typing import Any, FrozenSet, Iterable, Mapping, Optional, Tuple

from ..models.dependency_graph import (
    DependencyGraph,
    NodeKind,
    OperationKind,
    PropagationPlan,
    plan_propagation,
)
from ..models.workflow import EXPLICIT_GRAPH_ORIGIN, FREEFORM_GRAPH_ORIGIN


class GraphExecutionError(ValueError):
    """Raised when a supported explicit graph cannot be executed safely."""


@dataclass(frozen=True)
class TransformStep:
    """One ordered explicit transform in a QGIS-free execution plan."""

    edge_id: str
    operation: OperationKind
    parent_node_id: str
    target_node_id: str
    parameters: Mapping[str, Any]


@dataclass(frozen=True)
class GraphExecutionPlan:
    propagation: PropagationPlan
    source_node_id: str
    source_lineage_id: str
    derived_node_id: Optional[str]
    operation: Optional[OperationKind]
    operation_parameters: Mapping[str, Any]
    delivery_ids: FrozenSet[str]
    refreshed_node_ids: FrozenSet[str]
    parent_source_node_ids: Tuple[str, ...] = ()
    parent_source_lineage_ids: Tuple[str, ...] = ()
    transform_steps: Tuple[TransformStep, ...] = ()


def _explicit_source_nodes(workflow, graph: DependencyGraph):
    sources = {item.stable_id for item in workflow.source_layers if item.stable_id}
    result = {}
    for node in graph.nodes:
        if node.kind != NodeKind.SOURCE:
            continue
        lineage_id = str(node.metadata.get("source_lineage_id") or "")
        if not lineage_id:
            raise GraphExecutionError(
                f"Explicit source node {node.node_id!r} is missing metadata.source_lineage_id."
            )
        if lineage_id not in sources:
            raise GraphExecutionError(
                f"Explicit source node {node.node_id!r} references unregistered source lineage "
                f"{lineage_id!r}."
            )
        if lineage_id in result:
            raise GraphExecutionError(
                f"Multiple explicit source nodes reference lineage {lineage_id!r}."
            )
        result[lineage_id] = node.node_id
    return result


def _validate_explicit_shape(graph: DependencyGraph):
    node_map = graph.node_map()
    source_count = sum(node.kind == NodeKind.SOURCE for node in graph.nodes)
    transform_edges = []
    delivery_edges = []
    for edge in graph.edges:
        target = node_map[edge.to_node]
        source = node_map[edge.from_node]
        if target.kind == NodeKind.DERIVED:
            transform_edges.append(edge)
            if source.kind not in {NodeKind.SOURCE, NodeKind.DERIVED}:
                raise GraphExecutionError(
                    "Explicit derived nodes may only have SOURCE or DERIVED parents; "
                    f"edge {edge.edge_id!r} is unsupported."
                )
            if edge.operation not in {OperationKind.FILTER, OperationKind.MERGE}:
                raise GraphExecutionError(
                    f"Explicit derived edges support only FILTER or MERGE; edge "
                    f"{edge.edge_id!r} uses {edge.operation.value.upper()}."
                )
        elif target.kind == NodeKind.DELIVERY:
            delivery_edges.append(edge)
            if source.kind != NodeKind.DERIVED:
                raise GraphExecutionError(
                    "Explicit forward deliveries must be children of the derived node; "
                    f"edge {edge.edge_id!r} is a direct SOURCE → DELIVERY edge."
                )
            if edge.operation != OperationKind.EXPORT:
                raise GraphExecutionError(
                    f"Explicit graphs support only EXPORT edges to forward deliveries; edge "
                    f"{edge.edge_id!r} "
                    f"uses {edge.operation.value.upper()}."
                )
        else:
            raise GraphExecutionError(f"Unsupported node kind on {edge.edge_id!r}.")

    if not transform_edges:
        raise GraphExecutionError(
            "Explicit execution requires one FILTER or MERGE edge into a derived node."
        )

    derived_targets = {edge.to_node for edge in transform_edges}
    operations = {edge.operation for edge in transform_edges}
    if len(derived_targets) == 1:
        target_node_id = next(iter(derived_targets))
        target = node_map[target_node_id]
        if any(node_map[edge.from_node].kind != NodeKind.SOURCE for edge in transform_edges):
            raise GraphExecutionError(
                "G7a/G7b derived nodes may not have a derived parent."
            )
        if len(operations) != 1:
            raise GraphExecutionError(
                "FILTER and MERGE cannot be mixed into the same explicit derived node."
            )
        operation = transform_edges[0].operation
        if operation == OperationKind.FILTER:
            if source_count != 1 or len(transform_edges) != 1:
                raise GraphExecutionError(
                    "G7a FILTER requires exactly one explicit SOURCE parent and one DERIVED target."
                )
        elif operation == OperationKind.MERGE:
            if source_count != 2 or len(transform_edges) != 2:
                raise GraphExecutionError(
                    "G7b MERGE requires exactly two explicit SOURCE parents and one DERIVED target. "
                    "only FILTER supports one explicit SOURCE parent."
                )
            if len({edge.from_node for edge in transform_edges}) != 2:
                raise GraphExecutionError("G7b MERGE requires two distinct SOURCE parents.")
        ordered_edges = tuple(transform_edges)
        mode = "single"
    elif len(derived_targets) == 2:
        if any(node_map[edge.from_node].kind == NodeKind.DERIVED for edge in transform_edges):
            # Preserve the actionable G7b/G7c boundary for malformed chains.
            if any(edge.operation == OperationKind.MERGE for edge in transform_edges):
                raise GraphExecutionError("G7b MERGE may not have a derived parent.")
        if source_count != 1 or len(transform_edges) != 2:
            raise GraphExecutionError(
                "G7c requires exactly one SOURCE and two FILTER transform edges."
            )
        if operations != {OperationKind.FILTER}:
            raise GraphExecutionError(
                "G7c supports only FILTER edges; MERGE cannot be mixed into the chain."
            )
        first_edges = [
            edge
            for edge in transform_edges
            if node_map[edge.from_node].kind == NodeKind.SOURCE
        ]
        second_edges = [
            edge
            for edge in transform_edges
            if node_map[edge.from_node].kind == NodeKind.DERIVED
        ]
        if len(first_edges) != 1 or len(second_edges) != 1:
            raise GraphExecutionError(
                "G7c requires one simple SOURCE → FILTER → DERIVED_1 → FILTER → DERIVED_2 path; "
                "otherwise exactly one transform-derived node is supported."
            )
        first = first_edges[0]
        second = second_edges[0]
        if second.from_node != first.to_node or second.to_node == first.to_node:
            raise GraphExecutionError(
                "G7c requires one simple SOURCE → FILTER → DERIVED_1 → FILTER → DERIVED_2 path."
            )
        if sum(node.kind == NodeKind.DERIVED for node in graph.nodes) != 2:
            raise GraphExecutionError("G7c supports exactly two derived transform nodes.")
        target_node_id = second.to_node
        target = node_map[target_node_id]
        ordered_edges = (first, second)
        mode = "chain"
    else:
        raise GraphExecutionError(
            "Explicit execution supports at most two derived transform nodes; branching and G7d are not supported."
        )

    for edge in ordered_edges:
        expression = str(edge.parameters.get("expression") or "").strip()
        if edge.operation == OperationKind.FILTER and not expression:
            raise GraphExecutionError(
                f"FILTER edge {edge.edge_id!r} requires a non-empty parameters.expression."
            )

    for child in delivery_edges:
        if child.from_node != target_node_id:
            raise GraphExecutionError(
                "Explicit deliveries may only originate from the final derived node; "
                f"edge {child.edge_id!r} is not supported."
            )
        delivery = node_map[child.to_node]
        if delivery.kind != NodeKind.DELIVERY:
            raise GraphExecutionError(
                "Explicit execution does not support chained delivery transforms."
            )
        if child.operation != OperationKind.EXPORT:
            raise GraphExecutionError(
                f"Explicit execution does not support operation {child.operation.value.upper()} "
                "after the derived node."
            )
        if not str(delivery.metadata.get("delivery_id") or ""):
            raise GraphExecutionError(
                f"Forward delivery node {delivery.node_id!r} is missing metadata.delivery_id."
            )
        if delivery.format not in {"geojson", "shapefile", "kml", "kmz", "gpkg", "geotiff"}:
            raise GraphExecutionError(
                f"Explicit execution does not support forward delivery format {delivery.format!r}."
            )
    return mode, ordered_edges, target_node_id


def plan_explicit_graph_execution(
    workflow,
    changed_source_lineage_ids: Iterable[str],
    selected_edge_ids: Optional[Iterable[str]] = None,
) -> GraphExecutionPlan:
    """Plan one explicit graph run without resolving or mutating QGIS objects."""

    if workflow.effective_dependency_graph_origin() != EXPLICIT_GRAPH_ORIGIN:
        raise GraphExecutionError(
            "Explicit graph execution requires workflow origin EXPLICIT_GRAPH_ORIGIN."
        )
    graph = workflow.effective_dependency_graph()
    errors = graph.validate()
    if errors:
        raise GraphExecutionError("Invalid dependency graph: " + " | ".join(errors))
    source_nodes = _explicit_source_nodes(workflow, graph)
    requested = {str(item) for item in changed_source_lineage_ids if str(item)}
    if not requested:
        raise GraphExecutionError("At least one changed source lineage is required.")
    unknown = requested - set(source_nodes)
    if unknown:
        raise GraphExecutionError(
            "Changed source lineage is not present in the explicit workflow graph: "
            + ", ".join(sorted(unknown))
        )

    mode, transform_edges, derived_node_id = _validate_explicit_shape(graph)
    operation = transform_edges[0].operation
    configured_delivery_ids = {
        item.delivery_id for item in workflow.forward_deliveries if item.delivery_id
    }
    graph_delivery_ids = {
        str(graph.node_map()[edge.to_node].metadata.get("delivery_id") or "")
        for edge in graph.downstream_edges(derived_node_id)
    }
    unknown_deliveries = graph_delivery_ids - configured_delivery_ids
    if unknown_deliveries:
        raise GraphExecutionError(
            "Explicit forward delivery node(s) are not configured in the workflow: "
            + ", ".join(sorted(unknown_deliveries))
        )
    source_edges = (
        [transform_edges[0]]
        if mode == "chain"
        else list(transform_edges)
    )
    parent_sources = sorted(
        (
            str(graph.node_map()[edge.from_node].metadata["source_lineage_id"]),
            edge.from_node,
        )
        for edge in source_edges
    )
    parent_source_lineage_ids = tuple(item[0] for item in parent_sources)
    parent_source_node_ids = tuple(item[1] for item in parent_sources)
    if mode == "single" and operation == OperationKind.FILTER and requested != set(parent_source_lineage_ids):
        raise GraphExecutionError(
            "G7a requires exactly the single FILTER parent source to be the changed source."
        )
    changed_node_ids = {source_nodes[lineage_id] for lineage_id in requested}
    propagation = plan_propagation(graph, changed_node_ids, selected_edge_ids)
    if mode == "single" and operation == OperationKind.MERGE:
        changed_parent_edges = {
            edge.edge_id
            for edge in transform_edges
            if edge.from_node in changed_node_ids
        }
        selected_changed_parent_edges = changed_parent_edges & propagation.selected_edge_ids
        if selected_changed_parent_edges and selected_changed_parent_edges != changed_parent_edges:
            raise GraphExecutionError(
                "The MERGE target has multiple changed parents. All changed parent edges must be "
                "selected for this rebuild because historical parent snapshots are not available."
            )
    if mode == "single" and operation == OperationKind.MERGE:
        steps = (
            TransformStep(
                edge_id=transform_edges[0].edge_id,
                operation=operation,
                parent_node_id=transform_edges[0].from_node,
                target_node_id=derived_node_id,
                parameters=dict(transform_edges[0].parameters),
            ),
        )
    else:
        steps = tuple(
            TransformStep(
                edge_id=edge.edge_id,
                operation=edge.operation,
                parent_node_id=edge.from_node,
                target_node_id=edge.to_node,
                parameters=dict(edge.parameters),
            )
            for edge in transform_edges
        )
    executable_steps = []
    if mode == "single" and operation == OperationKind.MERGE:
        if (
            derived_node_id in propagation.affected_node_ids
            and derived_node_id not in propagation.stale_by_choice_node_ids
        ):
            executable_steps.extend(steps)
    else:
        for step in steps:
            if step.edge_id not in propagation.selected_edge_ids:
                break
            if (
                step.target_node_id not in propagation.affected_node_ids
                or step.target_node_id in propagation.stale_by_choice_node_ids
            ):
                break
            executable_steps.append(step)
    delivery_ids = set()
    refreshed = set()
    refreshed.update(step.target_node_id for step in executable_steps)
    final_executable = bool(executable_steps) and executable_steps[-1].target_node_id == derived_node_id
    if final_executable:
        node_map = graph.node_map()
        for edge in graph.downstream_edges(derived_node_id):
            if edge.edge_id not in propagation.selected_edge_ids:
                continue
            if edge.to_node in propagation.stale_by_choice_node_ids:
                continue
            delivery = node_map[edge.to_node]
            delivery_ids.add(str(delivery.metadata["delivery_id"]))
            refreshed.add(edge.to_node)

    return GraphExecutionPlan(
        propagation=propagation,
        source_node_id=parent_source_node_ids[0],
        source_lineage_id=parent_source_lineage_ids[0],
        derived_node_id=derived_node_id,
        operation=operation,
        operation_parameters=dict(transform_edges[0].parameters),
        delivery_ids=frozenset(delivery_ids),
        refreshed_node_ids=frozenset(refreshed),
        parent_source_node_ids=parent_source_node_ids,
        parent_source_lineage_ids=parent_source_lineage_ids,
        transform_steps=steps,
    )


def _canonical_execution_value(value):
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_execution_value(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_execution_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def explicit_execution_config_fingerprint(workflow) -> str:
    """Return the stable execution identity for an explicit workflow.

    Presentation names and transient source QGIS bindings are deliberately
    excluded.  Graph IDs, transform parameters/defaults, immutable source
    lineage IDs, and configured forward delivery destinations are included.
    """

    if workflow.effective_dependency_graph_origin() not in {
        EXPLICIT_GRAPH_ORIGIN,
        FREEFORM_GRAPH_ORIGIN,
    }:
        return ""

    graph = workflow.effective_dependency_graph()
    delivery_by_id = {
        delivery.delivery_id: delivery for delivery in workflow.forward_deliveries
    }
    nodes = []
    for node in sorted(graph.nodes, key=lambda item: (item.kind.value, item.node_id)):
        item = {"node_id": node.node_id, "kind": node.kind.value}
        if node.kind == NodeKind.SOURCE:
            item["source_lineage_id"] = str(node.metadata.get("source_lineage_id") or "")
        elif node.kind == NodeKind.DELIVERY:
            delivery_id = str(node.metadata.get("delivery_id") or "")
            delivery = delivery_by_id.get(delivery_id)
            item.update(
                {
                    "delivery_id": delivery_id,
                    "format": delivery.format if delivery is not None else node.format,
                }
            )
        nodes.append(item)

    edges = [
        {
            "edge_id": edge.edge_id,
            "from_node": edge.from_node,
            "to_node": edge.to_node,
            "operation": edge.operation.value,
            "enabled_by_default": edge.enabled_by_default,
            "parameters": _canonical_execution_value(edge.parameters),
        }
        for edge in sorted(graph.edges, key=lambda item: item.edge_id)
    ]
    sources = sorted(
        {str(source.stable_id) for source in workflow.source_layers if source.stable_id}
    )
    deliveries = [
        {
            "delivery_id": delivery.delivery_id,
            "format": delivery.format,
            "path": delivery.path,
            "enabled_by_default": delivery.enabled_by_default,
        }
        for delivery in sorted(
            workflow.forward_deliveries, key=lambda item: item.delivery_id
        )
    ]
    canonical = {
        "sources": sources,
        "nodes": nodes,
        "edges": edges,
        "deliveries": deliveries,
    }
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# Short name kept as the public planner API requested by the explicit graph contract.
GraphExecutionPlanError = GraphExecutionError
plan_graph_execution = plan_explicit_graph_execution
