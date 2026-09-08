"""Generic dependency graph primitives for AtOnce.

This module is deliberately QGIS-free. The proven v0.1 A/B -> C -> outputs
workflow remains the first execution adapter, while these objects describe the
broader product model: arbitrary source/derived/delivery nodes connected by
explicit dependency edges with per-run propagation selection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Set


class NodeKind(str, Enum):
    SOURCE = "source"
    DERIVED = "derived"
    DELIVERY = "delivery"


class OperationKind(str, Enum):
    PASSTHROUGH = "passthrough"
    FILTER = "filter"
    JOIN = "join"
    MERGE = "merge"
    COMPARE_CHANGES = "compare_changes"
    CALCULATE_FIELD = "calculate_field"
    FIELD_MAPPING = "field_mapping"
    KEEP_FIELDS = "keep_fields"
    RENAME_FIELD = "rename_field"
    CHANGE_FIELD_TYPE = "change_field_type"
    REMOVE_DUPLICATES = "remove_duplicates"
    SORT = "sort"
    AGGREGATE = "aggregate"
    SELECT_BY_LOCATION = "select_by_location"
    SPATIAL_JOIN = "spatial_join"
    CLIP = "clip"
    BUFFER = "buffer"
    DISSOLVE = "dissolve"
    REPROJECT = "reproject"
    RASTER_REPROJECT = "raster_reproject"
    RASTER_CONVERT = "raster_convert"
    EXPORT = "export"
    CUSTOM = "custom"


# `DERIVED` is the persisted name for an operation/result block in the current
# graph schema. Keep that storage identity stable while making the free-form
# canvas connection contract explicit and reusable by graph editors.
ALLOWED_CONNECTIONS = {
    NodeKind.SOURCE: frozenset({NodeKind.DERIVED}),
    NodeKind.DERIVED: frozenset({NodeKind.DERIVED, NodeKind.DELIVERY}),
    NodeKind.DELIVERY: frozenset(),
}


def allowed_target_kinds(node_kind: NodeKind):
    """Return node kinds accepted by a node's output port."""

    kind = NodeKind(node_kind)
    return ALLOWED_CONNECTIONS[kind]


def allowed_source_kinds(node_kind: NodeKind):
    """Return node kinds accepted by a node's input port."""

    kind = NodeKind(node_kind)
    return frozenset(
        source_kind
        for source_kind, targets in ALLOWED_CONNECTIONS.items()
        if kind in targets
    )


def connection_error(
    from_kind: NodeKind,
    to_kind: NodeKind,
    operation: Optional[OperationKind] = None,
) -> Optional[str]:
    """Return an actionable error for one proposed graph connection.

    The type matrix is independent of execution support. Operation-specific
    planners may reject an otherwise valid operation later, but a source can
    never bypass the operation layer and an output can never become an
    upstream node.
    """

    source_kind = NodeKind(from_kind)
    target_kind = NodeKind(to_kind)
    if target_kind not in allowed_target_kinds(source_kind):
        if source_kind == NodeKind.SOURCE and target_kind == NodeKind.DELIVERY:
            return "Source layers must connect to an operation first."
        if source_kind == NodeKind.SOURCE:
            return "Source blocks can connect only to operation blocks."
        if source_kind == NodeKind.DELIVERY:
            return "Outputs are terminal and cannot start a connection."
        if target_kind == NodeKind.SOURCE:
            return "Operations cannot connect to source blocks."
        return "Outputs can only receive results from an operation."

    if operation is not None:
        operation_kind = OperationKind(operation)
        if target_kind == NodeKind.DELIVERY and operation_kind != OperationKind.EXPORT:
            return "Output connections must use EXPORT from an operation."
        if target_kind != NodeKind.DELIVERY and operation_kind == OperationKind.EXPORT:
            return "EXPORT is terminal and can only connect to an output."
    return None


@dataclass(frozen=True)
class DependencyNode:
    node_id: str
    name: str
    kind: NodeKind
    format: str = ""
    current_revision: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict, compare=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "kind": self.kind.value,
            "format": self.format,
            "current_revision": self.current_revision,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "DependencyNode":
        return cls(
            node_id=str(payload.get("node_id", "")),
            name=str(payload.get("name", "")),
            kind=NodeKind(str(payload.get("kind", NodeKind.DERIVED.value))),
            format=str(payload.get("format", "")),
            current_revision=(
                int(payload["current_revision"])
                if payload.get("current_revision") not in (None, "")
                else None
            ),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass(frozen=True)
class DependencyEdge:
    edge_id: str
    from_node: str
    to_node: str
    operation: OperationKind
    enabled_by_default: bool = True
    parameters: Dict[str, Any] = field(default_factory=dict, compare=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "from_node": self.from_node,
            "to_node": self.to_node,
            "operation": self.operation.value,
            "enabled_by_default": self.enabled_by_default,
            "parameters": dict(self.parameters),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "DependencyEdge":
        return cls(
            edge_id=str(payload.get("edge_id", "")),
            from_node=str(payload.get("from_node", "")),
            to_node=str(payload.get("to_node", "")),
            operation=OperationKind(str(payload.get("operation", OperationKind.CUSTOM.value))),
            enabled_by_default=bool(payload.get("enabled_by_default", True)),
            parameters=dict(payload.get("parameters") or {}),
        )


@dataclass
class DependencyGraph:
    nodes: List[DependencyNode] = field(default_factory=list)
    edges: List[DependencyEdge] = field(default_factory=list)

    def node_map(self) -> Dict[str, DependencyNode]:
        return {node.node_id: node for node in self.nodes}

    def validate(self, *, require_complete: bool = True) -> List[str]:
        """Return structural errors for a persisted or executable graph.

        Editing can call ``validate(require_complete=False)`` to retain
        incomplete blocks while the user is still authoring.  The default keeps
        the historical strict contract used by persistence and execution.
        """

        errors: List[str] = []
        node_ids = [node.node_id for node in self.nodes]
        edge_ids = [edge.edge_id for edge in self.edges]

        if any(not node_id for node_id in node_ids):
            errors.append("Every dependency node requires a non-empty node_id.")
        if len(set(node_ids)) != len(node_ids):
            errors.append("Dependency node IDs must be unique.")
        if any(not edge_id for edge_id in edge_ids):
            errors.append("Every dependency edge requires a non-empty edge_id.")
        if len(set(edge_ids)) != len(edge_ids):
            errors.append("Dependency edge IDs must be unique.")

        known = set(node_ids)
        node_map = self.node_map()
        incoming = {node_id: [] for node_id in node_ids}
        connection_keys = set()
        for edge in self.edges:
            if edge.from_node not in known:
                errors.append(f"Edge {edge.edge_id!r} references missing source node {edge.from_node!r}.")
            if edge.to_node not in known:
                errors.append(f"Edge {edge.edge_id!r} references missing target node {edge.to_node!r}.")
            if edge.from_node == edge.to_node:
                errors.append(f"Edge {edge.edge_id!r} cannot point a node to itself.")
            if edge.from_node in known and edge.to_node in known:
                connection_key = (
                    edge.from_node,
                    edge.to_node,
                    edge.operation.value,
                    str(edge.parameters.get("target_port") or "input"),
                )
                if connection_key in connection_keys:
                    errors.append(
                        f"Edge {edge.edge_id!r} duplicates an existing connection."
                    )
                connection_keys.add(connection_key)
                incoming[edge.to_node].append(edge)
                error = connection_error(
                    node_map[edge.from_node].kind,
                    node_map[edge.to_node].kind,
                    edge.operation,
                )
                if error:
                    errors.append(f"Edge {edge.edge_id!r}: {error}")
                target = node_map[edge.to_node]
                declared_operation = target.metadata.get("operation_kind")
                if target.kind == NodeKind.DERIVED and declared_operation:
                    try:
                        if edge.operation != OperationKind(declared_operation):
                            errors.append(
                                f"Edge {edge.edge_id!r} uses {edge.operation.value.upper()} "
                                f"for a {OperationKind(declared_operation).value.upper()} operation node."
                            )
                    except ValueError:
                        # The registry validation below reports the unknown
                        # operation name with the node-specific context.
                        pass

        for node in self.nodes:
            if node.kind != NodeKind.DELIVERY or node.node_id not in incoming:
                continue
            count = len(incoming[node.node_id])
            if count > 1:
                errors.append(
                    f"Output node {node.node_id!r} must have exactly one operation input; "
                    f"found {count}."
                )
            elif require_complete and count != 1:
                errors.append(
                    f"Output node {node.node_id!r} must have exactly one operation input; "
                    f"found {count}."
                )

        # The operation registry is optional here to keep this low-level model
        # independent of UI/runtime imports.  When operation metadata is present,
        # enforce named input ports and cardinality at the graph boundary.
        try:
            from ..core.operation_registry import DEFAULT_OPERATION_REGISTRY

            for node in self.nodes:
                if node.kind != NodeKind.DERIVED:
                    continue
                raw_kind = node.metadata.get("operation_kind")
                if not raw_kind:
                    continue  # legacy/G7 nodes derive operation from incoming edges
                definition = DEFAULT_OPERATION_REGISTRY.get(raw_kind)
                if definition is None:
                    errors.append(
                        f"Operation node {node.node_id!r} uses unsupported operation {raw_kind!r}."
                    )
                    continue
                node_edges = incoming.get(node.node_id, [])
                grouped = {}
                for edge in node_edges:
                    port_id = str(edge.parameters.get("target_port") or "input")
                    grouped.setdefault(port_id, []).append(edge)
                    if definition.input_port(port_id) is None:
                        errors.append(
                            f"Edge {edge.edge_id!r} targets unknown {definition.title} input port {port_id!r}."
                        )
                for port in definition.input_ports:
                    count = len(grouped.get(port.port_id, []))
                    if require_complete and count < port.min_count:
                        errors.append(
                            f"Operation node {node.node_id!r} requires input port "
                            f"{port.label!r}."
                        )
                    if port.max_count is not None and count > port.max_count:
                        errors.append(
                            f"Operation node {node.node_id!r} input port "
                            f"{port.label!r} accepts at most {port.max_count} connection(s)."
                        )
        except (ImportError, AttributeError):
            # Keep the data model importable in the smallest QGIS-free test
            # environments; full application validation has the registry.
            pass

        if not errors and self._contains_cycle():
            errors.append("Dependency graph must be acyclic.")
        return errors

    def validate_editing(self) -> List[str]:
        """Validate topology while allowing incomplete source/operation blocks."""

        return self.validate(require_complete=False)

    def incoming_edges(self, node_id: str) -> List[DependencyEdge]:
        return [edge for edge in self.edges if edge.to_node == node_id]

    def topological_order(self, *, require_complete: bool = True) -> List[str]:
        """Return deterministic dependency order, independent of canvas position."""

        errors = self.validate(require_complete=require_complete)
        if errors:
            raise ValueError("Invalid dependency graph: " + " | ".join(errors))
        outgoing: Dict[str, List[str]] = {node.node_id: [] for node in self.nodes}
        indegree: Dict[str, int] = {node.node_id: 0 for node in self.nodes}
        for edge in self.edges:
            if edge.from_node not in outgoing or edge.to_node not in indegree:
                continue
            outgoing[edge.from_node].append(edge.to_node)
            indegree[edge.to_node] += 1
        ready = sorted(node_id for node_id, count in indegree.items() if count == 0)
        ordered = []
        while ready:
            current = ready.pop(0)
            ordered.append(current)
            for target in sorted(outgoing[current]):
                indegree[target] -= 1
                if indegree[target] == 0:
                    ready.append(target)
                    ready.sort()
        if len(ordered) != len(indegree):
            raise ValueError("Dependency graph must be acyclic.")
        return ordered

    def _contains_cycle(self) -> bool:
        outgoing: Dict[str, List[str]] = {node.node_id: [] for node in self.nodes}
        indegree: Dict[str, int] = {node.node_id: 0 for node in self.nodes}
        for edge in self.edges:
            if edge.from_node not in outgoing or edge.to_node not in indegree:
                continue
            outgoing[edge.from_node].append(edge.to_node)
            indegree[edge.to_node] += 1

        queue = [node_id for node_id, degree in indegree.items() if degree == 0]
        seen = 0
        while queue:
            current = queue.pop()
            seen += 1
            for target in outgoing[current]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        return seen != len(indegree)

    def downstream_edges(self, node_id: str) -> List[DependencyEdge]:
        return [edge for edge in self.edges if edge.from_node == node_id]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "DependencyGraph":
        return cls(
            nodes=[DependencyNode.from_dict(item) for item in payload.get("nodes", [])],
            edges=[DependencyEdge.from_dict(item) for item in payload.get("edges", [])],
        )


@dataclass(frozen=True)
class PropagationPlan:
    """One explicit propagation run through a dependency graph.

    `selected_edge_ids` is run-scoped. Toggling a checkbox for one run must not
    silently modify `enabled_by_default` in the saved workflow configuration.

    A node may appear in both `affected_node_ids` and
    `stale_by_choice_node_ids`. Example: a merge target receives one selected
    upstream change while another changed parent edge is deliberately skipped.
    The target can refresh partially, but it is still stale relative to the
    omitted dependency.
    """

    changed_node_ids: Set[str]
    selected_edge_ids: Set[str]
    affected_node_ids: Set[str]
    skipped_edge_ids: Set[str]
    stale_by_choice_node_ids: Set[str]


def default_selected_edges(graph: DependencyGraph) -> Set[str]:
    return {edge.edge_id for edge in graph.edges if edge.enabled_by_default}


def plan_propagation(
    graph: DependencyGraph,
    changed_node_ids: Iterable[str],
    selected_edge_ids: Optional[Iterable[str]] = None,
) -> PropagationPlan:
    """Plan downstream impact using only explicitly selected dependency edges.

    A deselected edge does not erase dependency knowledge. If a changed/affected
    upstream node reaches a deselected edge, that edge is recorded as skipped and
    its target becomes `stale_by_choice`. Staleness then propagates through every
    descendant because those descendants ultimately depend on a deliberately
    stale parent, even when another selected path also refreshes them.
    """

    errors = graph.validate()
    if errors:
        raise ValueError("Invalid dependency graph: " + " | ".join(errors))

    node_ids = set(graph.node_map())
    changed = {str(item) for item in changed_node_ids}
    missing_changed = changed - node_ids
    if missing_changed:
        raise ValueError(
            "Changed node(s) are not present in dependency graph: "
            + ", ".join(sorted(missing_changed))
        )

    selected = (
        default_selected_edges(graph)
        if selected_edge_ids is None
        else {str(item) for item in selected_edge_ids}
    )
    known_edges = {edge.edge_id for edge in graph.edges}
    unknown_selected = selected - known_edges
    if unknown_selected:
        raise ValueError(
            "Selected edge(s) are not present in dependency graph: "
            + ", ".join(sorted(unknown_selected))
        )

    affected = set(changed)
    skipped: Set[str] = set()
    stale_roots: Set[str] = set()

    # First traverse only selected edges. Any deselected edge reached from a
    # changed/affected node becomes an explicit stale-by-choice root.
    queue = list(changed)
    visited = set(changed)
    while queue:
        current = queue.pop(0)
        for edge in graph.downstream_edges(current):
            if edge.edge_id in selected:
                affected.add(edge.to_node)
                if edge.to_node not in visited:
                    visited.add(edge.to_node)
                    queue.append(edge.to_node)
            else:
                skipped.add(edge.edge_id)
                stale_roots.add(edge.to_node)

    # Then propagate staleness through the full dependency graph. A descendant
    # remains stale even if it is also being refreshed via another selected path.
    stale = set(stale_roots)
    queue = list(stale_roots)
    while queue:
        current = queue.pop(0)
        for edge in graph.downstream_edges(current):
            if edge.to_node not in stale:
                stale.add(edge.to_node)
                queue.append(edge.to_node)

    return PropagationPlan(
        changed_node_ids=changed,
        selected_edge_ids=selected,
        affected_node_ids=affected,
        skipped_edge_ids=skipped,
        stale_by_choice_node_ids=stale,
    )
