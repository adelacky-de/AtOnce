"""Port compatibility policy shared by the free-form canvas connector surface."""

from typing import Optional

from ..core.graph_editing import can_connect
from ..models.dependency_graph import NodeKind, OperationKind, allowed_source_kinds, allowed_target_kinds


def output_port_targets(node_kind: NodeKind):
    """Kinds that a node's output port should visually accept."""

    return allowed_target_kinds(node_kind)


def input_port_sources(node_kind: NodeKind):
    """Kinds that a node's input port should visually accept."""

    return allowed_source_kinds(node_kind)


def accepts_connection(from_node, to_node, operation: Optional[OperationKind] = None) -> bool:
    """Return the UI drop decision before any graph-edit service call."""

    return can_connect(from_node, to_node, operation)
